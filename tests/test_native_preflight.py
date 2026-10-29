"""Read-only target probing cannot enqueue a paid prompt."""
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from agentctl.notify.codex import CodexNotify
from agentctl.errors import InvalidTargetError

FAKE=str(Path(__file__).parent/'fakes/fake-codex')
THREAD='12345678-1234-1234-1234-123456789abc'
class NativePreflightTests(unittest.TestCase):
    def test_codex_probe_reads_without_queueing(self):
        with tempfile.TemporaryDirectory() as tmp:
            log=Path(tmp)/'queue.jsonl'
            with mock.patch.dict(os.environ,{'AGENTCTL_CODEX_BIN':FAKE,'FAKE_CODEX_QUEUE_LOG':str(log)}):
                result=CodexNotify().preflight(THREAD)
            self.assertEqual(result['thread']['id'],THREAD)
            self.assertFalse(log.exists())

    def test_codex_missing_thread_does_not_enqueue(self):
        with tempfile.TemporaryDirectory() as tmp:
            log=Path(tmp)/'queue.jsonl'
            with mock.patch.dict(os.environ,{'AGENTCTL_CODEX_BIN':FAKE,'FAKE_CODEX_QUEUE_LOG':str(log),'FAKE_CODEX_PREFLIGHT_FAIL':'1'}):
                with self.assertRaises(InvalidTargetError):
                    CodexNotify().preflight(THREAD)
            self.assertFalse(log.exists())

    def test_queue_route_works_without_proxy_and_does_not_enqueue(self):
        with tempfile.TemporaryDirectory() as tmp:
            ipc = Path(tmp) / 'ipc'
            ipc.mkdir()
            log = Path(tmp) / 'queue.jsonl'
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(ipc / 'ipc.sock'))
                listener.listen(1)
                with mock.patch.dict(os.environ, {
                    'AGENTCTL_CODEX_BIN': FAKE, 'FAKE_CODEX_PROXY_FAIL': '1',
                    'FAKE_CODEX_QUEUE_LOG': str(log), 'CODEX_HOME': tmp,
                }), mock.patch('agentctl.discover._codex_sessions', return_value=[
                    {'native_id': THREAD, 'title': 'test'}
                ]):
                    result = CodexNotify().preflight(THREAD)
                self.assertTrue(result['verified'])
                self.assertEqual(result['transport'], 'codex queue')
                self.assertFalse(log.exists())

    def test_missing_proxy_and_queue_routes_remain_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {
                'AGENTCTL_CODEX_BIN': FAKE, 'FAKE_CODEX_PROXY_FAIL': '1',
                'CODEX_HOME': tmp,
            }), mock.patch('agentctl.discover._codex_sessions', return_value=[
                {'native_id': THREAD}
            ]):
                result = CodexNotify().preflight(THREAD)
            self.assertFalse(result['verified'])
            self.assertIn('control socket does not exist', result['note'])

    def test_live_queue_listener_does_not_verify_unknown_target(self):
        with mock.patch('agentctl.discover._codex_sessions', return_value=[]):
            with self.assertRaisesRegex(ValueError, 'target is not present'):
                CodexNotify()._preflight_queue(THREAD)
