"""Exercise notification routing against an actual framed Unix socket peer."""
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from agentctl.errors import DeliveryError
from agentctl.notify.codex import CodexNotify

THREAD = '11111111-2222-4333-8444-555555555555'


class Peer:
    def __init__(self, path, mode):
        self.mode = mode
        self.methods = []
        self.error = None
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(1)
        self.server.settimeout(5)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def read(self, conn, size):
        data = b''
        while len(data) < size:
            part = conn.recv(size - len(data))
            if not part:
                return None
            data += part
        return data

    def run(self):
        try:
            with self.server.accept()[0] as conn:
                conn.settimeout(5)
                while True:
                    header = self.read(conn, 4)
                    if header is None:
                        return
                    req = json.loads(self.read(conn, struct.unpack('<I', header)[0]))
                    method = req['method']
                    self.methods.append(method)
                    response = {'type': 'response', 'requestId': req['requestId'],
                                'resultType': 'success', 'method': method,
                                'handledByClientId': 'owner', 'result': {}}
                    if method == 'initialize':
                        response['result'] = {'clientId': 'probe'}
                    elif method == 'thread-owner-discovery':
                        response['result'] = {'supportsUntrustedAppInput': True}
                    elif method == 'thread-follower-steer-turn':
                        if self.mode == 'disconnect':
                            return  # Simulate accepted request with lost response.
                        if self.mode == 'idle':
                            response.update(resultType='error', error='Cannot steer conversation %s because its active turn already ended' % THREAD)
                        elif self.mode == 'reject':
                            response.update(resultType='error', error='request-version-mismatch')
                        elif self.mode == 'malformed':
                            response['result'] = {'unexpected': True}
                        else:
                            response['result'] = {'result': {'turnId': 'active-turn'}}
                    raw = json.dumps(response).encode()
                    # Fragment the frame header: clients must read complete frames.
                    wire = struct.pack('<I', len(raw)) + raw
                    conn.sendall(wire[:2])
                    conn.sendall(wire[2:])
        except Exception as exc:
            self.error = exc
        finally:
            self.server.close()

    def finish(self):
        self.thread.join(6)
        if self.thread.is_alive():
            raise AssertionError('IPC peer did not finish')
        if self.error:
            raise self.error


class CodexNotifications(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='steer-', dir='/tmp')
        self.root = Path(self.temp.name)
        self.queue_log = self.root / 'queue.json'
        binary = self.root / 'codex'
        binary.write_text('#!/usr/bin/env python3\nimport json,os,sys\nopen(os.environ["TEST_QUEUE_LOG"],"w").write(json.dumps(sys.argv[1:]))\nprint("queued")\nsys.exit(int(os.environ.get("TEST_QUEUE_EXIT","0")))\n')
        binary.chmod(0o755)
        self.env = patch.dict(os.environ, {'AGENTCTL_CODEX_BIN': str(binary),
            'AGENTCTL_CODEX_IPC_SOCKET': str(self.root / 'ipc.sock'),
            'TEST_QUEUE_LOG': str(self.queue_log), 'TEST_QUEUE_EXIT': '0'})
        self.env.start()
        self.peer = None

    def tearDown(self):
        try:
            if self.peer:
                self.peer.finish()
        finally:
            self.env.stop()
            self.temp.cleanup()

    def deliver(self, mode):
        self.peer = Peer(os.environ['AGENTCTL_CODEX_IPC_SOCKET'], mode)
        return CodexNotify().deliver(THREAD, 'notice', 'stable-notice')

    def assert_queued(self, result):
        self.assertEqual(result['transport'], 'codex queue')
        self.assertEqual(json.loads(self.queue_log.read_text()),
                         ['queue', '--thread', THREAD, '--message', 'notice'])

    def test_active_session_receives_steer_without_queue(self):
        result = self.deliver('active')
        self.assertEqual(result['turn_id'], 'active-turn')
        self.assertFalse(self.queue_log.exists())

    def test_idle_session_queues_without_warning(self):
        result = self.deliver('idle')
        self.assert_queued(result)
        self.assertNotIn('steer_warning', result)

    def test_protocol_rejection_logs_and_queues(self):
        with self.assertLogs('agentctl.notify.codex', level='WARNING'):
            result = self.deliver('reject')
        self.assert_queued(result)
        self.assertIn('request-version-mismatch', result['steer_warning'])

    def test_invalid_success_response_logs_and_queues(self):
        with self.assertLogs('agentctl.notify.codex', level='WARNING'):
            result = self.deliver('malformed')
        self.assert_queued(result)
        self.assertIn('uncertain', result['steer_warning'])

    def test_lost_reply_queues_with_persistent_diagnostic(self):
        with self.assertLogs('agentctl.notify.codex', level='WARNING'):
            result = self.deliver('disconnect')
        self.assert_queued(result)
        self.assertIn('closed', result['steer_warning'])

    def test_missing_socket_logs_and_queues(self):
        with self.assertLogs('agentctl.notify.codex', level='WARNING'):
            result = CodexNotify().deliver(THREAD, 'notice', 'id')
        self.assert_queued(result)
        self.assertIn('FileNotFoundError', result['steer_warning'])

    def test_queue_failure_remains_error(self):
        os.environ['TEST_QUEUE_EXIT'] = '1'
        with self.assertLogs('agentctl.notify.codex', level='WARNING'):
            with self.assertRaises(DeliveryError):
                CodexNotify().deliver(THREAD, 'notice', 'id')

    def test_exact_name_retains_queue_support(self):
        result = CodexNotify().deliver('manager', 'notice', 'id')
        self.assertEqual(result['transport'], 'codex queue')
        self.assertEqual(json.loads(self.queue_log.read_text())[2], 'manager')
