"""Boundary contracts between native hooks, CLI output, and daemon transport."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from agentctl import cli, ipc, setup, sessions
from agentctl.errors import DaemonUnavailableError, UsageError

class CLIIntegrationTests(unittest.TestCase):
    def run_command(self, argv, stdin=''):
        out=io.StringIO();err=io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err),mock.patch('sys.stdin',io.StringIO(stdin)):
            args=cli._build_parser().parse_args(argv)
            code=args.handler(args)
        return code,out.getvalue(),err.getvalue()

    def test_hook_commands_are_retired_without_dispatch(self):
        for action in ('start', 'begin', 'complete', 'receive'):
            with mock.patch('agentctl.ipc.call') as call, self.assertRaises(UsageError):
                self.run_command(['_hook',action,'--harness','claude'])
            call.assert_not_called()

    def test_hook_ipc_is_retired(self):
        daemon=mock.Mock()
        result=ipc.dispatch(daemon,{'op':'hook_event','target':'codex:id','event':'complete'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['exit_code'],67)
        daemon.notify_hook_event.assert_not_called()

    def test_inbox_ipc_returns_every_claimed_message(self):
        with mock.patch('agentctl.sessions.registry_update'),mock.patch('agentctl.sessions.inbox_claim_all',return_value=[{'message':'first'},{'message':'second'}]):
            result=ipc.dispatch(object(),{'op':'inbox_claim','address':'claude:id'})
        self.assertEqual(result['data']['message'],'first\n\nsecond')
        self.assertEqual(len(result['data']['messages']),2)

    def test_old_daemon_never_receives_new_mutation(self):
        with mock.patch('agentctl.ipc.ensure_daemon'),mock.patch('agentctl.ipc._request',return_value={'ok':True,'data':{'pid':123}}) as request:
            with self.assertRaises(DaemonUnavailableError):
                ipc.call('start',harness='claude',prompt='paid work')
        self.assertEqual(request.call_count,1)
        self.assertEqual(request.call_args.args[0],{'op':'ping'})

    def test_setup_failure_returns_nonzero(self):
        with mock.patch('agentctl.setup.uninstall_all',return_value={'cursor':{'ok':False,'error':'invalid config'}}):
            code,out,_=self.run_command(['setup','--uninstall','--harness','cursor','--json'])
        self.assertEqual(code,64)
        self.assertFalse(json.loads(out)['cursor']['ok'])

    def test_conflicting_setup_actions_are_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            cli._build_parser().parse_args(['setup','--inspect','--uninstall'])

    def test_read_outputs_full_message(self):
        text='x'*5000
        with mock.patch('agentctl.discover.read_history',return_value=[{'role':'assistant','content':text}]):
            code,out,_=self.run_command(['sessions','history','codex:id','--offset','2'])
        self.assertEqual(code,0);self.assertIn(text,out)

    def test_hook_installation_is_rejected_without_writing_configs(self):
        with mock.patch('agentctl.setup.setup_all') as install, self.assertRaises(UsageError):
            self.run_command(['setup','--harness','codex','--json'])
        install.assert_not_called()

    def test_group_defaults_list_without_crashing(self):
        with mock.patch('agentctl.discover.discover',return_value=[]):
            code,out,_=self.run_command(['sessions'])
        self.assertEqual(code,0);self.assertIn('no sessions',out)
        with mock.patch('agentctl.ipc.call',return_value=[]):
            code,out,_=self.run_command(['sub'])
        self.assertEqual(code,0);self.assertIn('no subscriptions',out)
