"""Retired installation APIs and selective, non-destructive legacy cleanup."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from agentctl import setup

class SetupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.paths = dict(settings_path=str(self.root/'claude.json'), cursor_hooks_path=str(self.root/'cursor.json'), codex_hooks_path=str(self.root/'codex.json'))

    def test_installation_apis_never_create_files(self):
        results = setup.setup_all(**self.paths)
        self.assertFalse(results['claude']['ok'])
        self.assertFalse(results['codex']['ok'])
        self.assertFalse(results['cursor']['ok'])
        self.assertEqual(list(self.root.iterdir()), [])
        for fn, kw in [(setup.setup_claude, {'settings_path':self.paths['settings_path']}),
                       (setup.setup_cursor, {'hooks_path':self.paths['cursor_hooks_path']}),
                       (setup.setup_codex, {'config_path':self.paths['codex_hooks_path']})]:
            changed, info = fn(**kw)
            self.assertFalse(changed)
            self.assertFalse(info['ok'])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cleanup_preserves_other_settings_and_mixed_hook_groups(self):
        path = self.root/'claude.json'
        path.write_text(json.dumps({'model':'sonnet','hooks':{'Stop':[{'matcher':'*','hooks':[
            {'type':'command','command':'existing'}, {'type':'command','command':'agentctl _hook complete --harness claude'}]}]}}))
        changed, info = setup.uninstall_claude(str(path))
        self.assertTrue(changed)
        value = json.loads(path.read_text())
        self.assertEqual(value['model'],'sonnet')
        self.assertEqual(value['hooks']['Stop'][0]['hooks'],[{'type':'command','command':'existing'}])
        before = path.read_bytes()
        self.assertFalse(setup.uninstall_claude(str(path))[0])
        self.assertEqual(path.read_bytes(),before)

    def test_selective_cleanup_preserves_other_harnesses(self):
        for harness in ['codex','claude','cursor']:
            (self.root/(harness+'.json')).write_text(json.dumps({'hooks':setup._definitions(harness)}))
        before = (self.root/'cursor.json').read_bytes()
        result = setup.uninstall_all(harnesses=['codex'],**self.paths)
        self.assertEqual(list(result),['codex'])
        self.assertFalse(any(json.loads((self.root/'codex.json').read_text())['hooks'].values()))
        self.assertEqual((self.root/'cursor.json').read_bytes(),before)

    def test_legacy_cursor_marker_and_user_history_hooks(self):
        path=self.root/'cursor.json'
        path.write_text(json.dumps({'version':1,'hooks':{'stop':[{'command':'agentctl _hook complete','_agentctl_owned':'v1'},
                                                               {'command':'/custom/history.sh'}]}}))
        setup.uninstall_cursor(str(path))
        self.assertEqual(json.loads(path.read_text())['hooks']['stop'],[{'command':'/custom/history.sh'}])

    def test_malformed_configuration_is_preserved(self):
        path=self.root/'cursor.json';path.write_text('{broken')
        changed, info=setup.uninstall_cursor(str(path))
        self.assertFalse(info['ok'])
        self.assertFalse(changed)
        self.assertEqual(path.read_text(),'{broken')

    def test_dry_run_does_not_mutate(self):
        path=self.root/'codex.json';path.write_text(json.dumps({'hooks':setup._definitions('codex')}))
        before=path.read_bytes()
        changed,info=setup.uninstall_codex(str(path),dry_run=True)
        self.assertTrue(changed)
        self.assertEqual(path.read_bytes(),before)

    def test_cleanup_preserves_file_permissions(self):
        path=self.root/'codex.json';path.write_text(json.dumps({'hooks':setup._definitions('codex')}));path.chmod(0o600)
        setup.uninstall_codex(str(path))
        self.assertEqual(path.stat().st_mode & 0o777,0o600)

    def test_inspect_reports_polling_and_retired_installation(self):
        info=setup.inspect_claude(str(self.root/'missing.json'))
        self.assertFalse(info['installed'])
        self.assertEqual(info['capabilities']['completion_detection'],'transcript polling in the daemon')
