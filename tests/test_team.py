"""Behavioral pipeline tests using real Git and a controllable agentctl CLI."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from agentctl_team.pipeline import Pipeline, config_load
from agentctl_team.ports import TeamError

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / 'tests/fakes/fake_team_agentctl.py'


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pipeline-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Pipeline Test')
        self.git('config', 'user.email', 'pipeline@example.invalid')
        (self.repo / 'README').write_text('initial\n')
        self.git('add', '.')
        self.git('commit', '-m', 'Initial')
        self.fake = self.root / 'jobs.json'
        previous = os.environ.get('FAKE_AGENTCTL_JOBS')
        os.environ['FAKE_AGENTCTL_JOBS'] = str(self.fake)
        self.addCleanup(lambda: os.environ.pop('FAKE_AGENTCTL_JOBS', None) if previous is None
                        else os.environ.__setitem__('FAKE_AGENTCTL_JOBS', previous))
        files = self.root / 'templates'
        files.mkdir()
        (files / 'brief.md').write_text('Stage {{ stage }} for {{ item_id }}')
        (files / 'report.md').write_text('---\noutcome: <replace-with-outcome>\n---\n\nNotes\n')
        for name in ('read-issue.sh', 'submit-report.sh'):
            (files / name).write_text('#!/bin/sh\nexit 0\n')
            (files / name).chmod(0o755)
        self.definition = dict(repo=str(self.repo), base_branch='main', agentctl=str(FAKE),
                               worktrees=str(self.root / 'worktrees'), poll_seconds=.01,
                               pools={'agents': 2, 'serial': 1}, entry='write',
                               launch_message='Read .agentctl-team/brief.md', stages={})
        for stage, pools, outcomes in (
                ('write', ['agents'], {'ready': 'review', 'blocked': 'hold'}),
                ('review', ['agents'], {'passed': 'integrate', 'changed': 'review', 'blocked': 'hold'}),
                ('integrate', ['agents', 'serial'], {'complete': 'done'})):
            self.definition['stages'][stage] = dict(pools=pools, timeout_seconds=60,
                worker={'harness': 'codex', 'model': None}, files=str(files), outcomes=outcomes)
        self.config = self.root / 'pipeline.json'
        self.write_config()
        self.p = Pipeline(self.load_config())
        self.p.validate_start()

    def load_config(self):
        return config_load(self.config, notify=getattr(self, 'notify_target', None))

    def git(self, *args, cwd=None):
        return subprocess.check_output(['git', *args], cwd=cwd or self.repo,
                                       text=True, stderr=subprocess.DEVNULL).strip()

    def write_config(self):
        self.config.write_text(json.dumps(self.definition))

    def jobs(self):
        return json.loads(self.fake.read_text())['jobs'] if self.fake.exists() else []

    def finish(self, item_id, outcome, status='succeeded'):
        item = self.p.show(item_id)
        report = Path(item['worktree']) / '.agentctl-team/report.md'
        report.write_text('---\noutcome: ' + outcome + '\n---\n\nFindings\n')
        receipt = self.p.report(item['attempt']['id'], report)
        state = json.loads(self.fake.read_text())
        for job in state['jobs']:
            if job['job'] == item['attempt']['job']:
                job['status'] = status
        self.fake.write_text(json.dumps(state))
        return receipt

    def test_same_worktree_uncommitted_changes_and_no_implicit_merge(self):
        self.p.add('one', 'Poem', 'Write a poem')
        main = self.git('rev-parse', 'main')
        self.p.tick()
        first = self.p.show('one')
        worktree = Path(first['worktree'])
        (worktree / 'poem.txt').write_text('A poem without a commit\n')
        self.finish('one', 'ready')
        self.p.tick()
        review = self.p.show('one')
        self.assertEqual('review', review['stage'])
        self.assertEqual(first['worktree'], review['worktree'])
        self.assertEqual('A poem without a commit\n', (worktree / 'poem.txt').read_text())
        self.assertNotEqual(first['attempt']['job'], review['attempt']['job'])
        self.assertIsNone(self.jobs()[-1]['parent'])
        self.finish('one', 'passed')
        self.p.tick()
        self.finish('one', 'complete')
        self.p.tick()
        self.assertEqual('done', self.p.show('one')['state'])
        self.assertEqual(main, self.git('rev-parse', 'main'))
        self.assertTrue(worktree.exists())
        self.assertEqual('', self.git('ls-files', '--others', '--exclude-standard', '.agentctl-team', cwd=worktree))

    def test_submission_waits_for_exit_and_is_idempotent(self):
        self.p.add('one', 'One', 'Body')
        self.p.tick()
        item = self.p.show('one')
        report = Path(item['worktree']) / '.agentctl-team/report.md'
        report.write_text('---\noutcome: ready\n---\n\nDone\n')
        first = self.p.report(item['attempt']['id'], report)
        self.assertEqual(first, self.p.report(item['attempt']['id'], report))
        self.p.tick()
        self.assertEqual('write', self.p.show('one')['stage'])
        self.assertEqual(1, len(self.jobs()))
        report.write_text('---\noutcome: blocked\n---\n\nQuestion\n')
        with self.assertRaises(TeamError):
            self.p.report(item['attempt']['id'], report)

    def test_shared_capacity_and_serial_integration(self):
        for item_id in ('one', 'two', 'three'):
            self.p.add(item_id, item_id, 'Body')
        self.p.tick()
        self.assertEqual(2, len(self.jobs()))
        for item_id in ('one', 'two'):
            self.finish(item_id, 'ready')
        self.p.tick()
        for item_id in ('one', 'two'):
            self.finish(item_id, 'passed')
        self.p.tick()
        active = self.p.active(self.p.store.load())
        self.assertEqual(2, len(active))
        self.assertEqual(1, sum(item['stage'] == 'integrate' for item in active))
        self.assertEqual('queued', self.p.show('two')['state'])
        self.finish('one', 'complete')
        self.p.tick()
        self.assertEqual('running', self.p.show('two')['state'])

    def test_human_report_routes_to_fresh_session_with_history(self):
        self.definition['stages']['write']['outcomes']['blocked'] = 'manager'
        self.definition['stages']['manager'] = {'type': 'human', 'outcomes': {'clarified': 'write'}}
        self.write_config()
        self.p = Pipeline(self.load_config())
        self.p.add('one', 'One', 'Body')
        self.p.tick()
        first_job = self.p.show('one')['attempt']['job']
        self.finish('one', 'blocked')
        self.p.tick()
        self.assertEqual('waiting-human', self.p.show('one')['state'])
        self.assertEqual([], self.p.active(self.p.store.load()))
        report = self.root / 'manager.md'
        report.write_text('---\noutcome: clarified\n---\n\nUse four lines\n')
        subprocess.run([str(ROOT / 'bin/agentctl-team'), '--config', str(self.config),
                        'issue', 'report', 'one', '--file', str(report)], check=True, capture_output=True)
        self.p.tick()
        item = self.p.show('one')
        self.assertEqual('write', item['stage'])
        self.assertNotEqual(first_job, item['attempt']['job'])
        self.assertIsNone(self.jobs()[-1]['parent'])
        self.assertEqual('clarified', item['history'][-1]['report']['outcome'])
        self.assertIn('Use four lines', item['history'][-1]['report']['text'])
        with self.assertRaises(TeamError):
            self.p.human_report('one', report)

    def test_invalid_report_and_timeout_hold_without_advancing(self):
        self.p.add('one', 'One', 'Body')
        self.p.tick()
        item = self.p.show('one')
        with self.assertRaises(TeamError):
            self.p.report(item['attempt']['id'], Path(item['worktree']) / '.agentctl-team/report.md')
        with self.p.store.transaction() as state:
            state['tasks']['one']['attempt']['started'] = time.time() - 100
        self.p.tick()
        self.assertEqual('cancelling', self.p.show('one')['state'])
        self.p.tick()
        self.assertEqual('error', self.p.show('one')['state'])
        self.assertEqual('write', self.p.show('one')['stage'])
        self.assertTrue(Path(item['worktree']).exists())

    def test_restart_recovers_launch_without_duplicate_job(self):
        self.p.add('one', 'One', 'Body')
        self.p.tick()
        with self.p.store.transaction() as state:
            state['tasks']['one']['state'] = 'launching'
            state['tasks']['one']['attempt']['job'] = None
        restarted = Pipeline(self.load_config())
        restarted.tick()
        self.assertEqual(1, len(self.jobs()))
        self.assertEqual('running', restarted.show('one')['state'])

    def test_live_issue_edit_and_dependencies(self):
        self.p.add('one', 'One', 'Before')
        self.p.add('two', 'Two', 'Body', deps=['one'])
        self.p.tick()
        self.p.edit('one', 'After')
        self.assertEqual('After', self.p.show('one')['body'])
        self.assertEqual('queued', self.p.show('two')['state'])
        self.assertEqual(1, len(self.jobs()))

    def test_bad_configuration_and_repository_collision_fail_before_launch(self):
        self.definition['stages']['write']['outcomes']['ready'] = 'missing'
        self.write_config()
        with self.assertRaises(TeamError):
            self.load_config()
        (self.repo / '.agentctl-team').mkdir()
        with self.assertRaises(TeamError):
            self.p.validate_start()
        self.assertEqual([], self.jobs())

    def test_template_edit_only_affects_future_attempt(self):
        self.p.add('one', 'One', 'Body')
        self.p.tick()
        first = self.p.show('one')['attempt']
        brief = Path(first['archive']) / 'files/brief.md'
        original = brief.read_text()
        (self.root / 'templates/brief.md').write_text('New instructions')
        self.assertEqual(original, brief.read_text())
        self.finish('one', 'ready')
        self.p.tick()
        self.assertEqual('New instructions', (Path(self.p.show('one')['worktree']) / '.agentctl-team/brief.md').read_text())

    def test_prepared_scripts_read_live_issue_and_submit_through_public_cli(self):
        files = self.root / 'templates'
        (files / 'read-issue.sh').write_text(
            '#!/bin/sh\nset -eu\n{{ team_command }} --config {{ config_path }} issue show {{ item_id }}\n')
        (files / 'submit-report.sh').write_text(
            '#!/bin/sh\nset -eu\n{{ team_command }} --config {{ config_path }} report '
            '--attempt {{ attempt }} --file .agentctl-team/report.md\n')
        self.p.add('one', 'One', 'Before')
        self.p.tick()
        item = self.p.show('one')
        self.p.edit('one', 'Updated while worker is running')
        result = subprocess.run(['.agentctl-team/read-issue.sh'], cwd=item['worktree'],
                                capture_output=True, text=True, check=True)
        self.assertEqual('Updated while worker is running', json.loads(result.stdout)['body'])
        report = Path(item['worktree']) / '.agentctl-team/report.md'
        report.write_text('---\noutcome: ready\n---\n\nFinished\n')
        submitted = subprocess.run(['.agentctl-team/submit-report.sh'], cwd=item['worktree'],
                                   capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(submitted.stdout)['receipt'])
        self.assertEqual('ready', self.p.show('one')['attempt']['report']['outcome'])

    def test_failed_worker_with_valid_report_does_not_route(self):
        self.p.add('one', 'One', 'Body')
        self.p.tick()
        self.finish('one', 'ready', status='failed')
        self.p.tick()
        self.assertEqual('error', self.p.show('one')['state'])
        self.assertEqual('write', self.p.show('one')['stage'])
        self.assertEqual(1, len(self.jobs()))

    def test_fresh_review_loop_preserves_changes_and_rejects_stale_attempt(self):
        self.p.add('one', 'One', 'Body')
        self.p.tick()
        self.finish('one', 'ready')
        self.p.tick()
        first = self.p.show('one')
        (Path(first['worktree']) / 'poem.txt').write_text('reviewer fix')
        self.finish('one', 'changed')
        self.p.tick()
        second = self.p.show('one')
        self.assertEqual('review', second['stage'])
        self.assertNotEqual(first['attempt']['job'], second['attempt']['job'])
        self.assertIsNone(self.jobs()[-1]['parent'])
        self.assertEqual('reviewer fix', (Path(second['worktree']) / 'poem.txt').read_text())
        report = Path(second['worktree']) / '.agentctl-team/report.md'
        report.write_text('---\noutcome: passed\n---\n\nDone\n')
        with self.assertRaises(TeamError):
            self.p.report(first['attempt']['id'], report)

    def test_restart_keeps_recorded_routing_for_running_attempt(self):
        self.p.add('one', 'One', 'Body')
        self.p.tick()
        self.definition['stages']['write']['outcomes']['ready'] = 'done'
        self.write_config()
        self.finish('one', 'ready')
        restarted = Pipeline(self.load_config())
        restarted.tick()
        self.assertEqual('review', restarted.show('one')['stage'])

    def test_scheduler_lock_prevents_second_owner(self):
        self.p.add('one', 'One', 'Body')
        with self.p.store.lock('scheduler', blocking=False):
            result = subprocess.run([str(ROOT / 'bin/agentctl-team'), '--config',
                                     str(self.config), 'start', '--once'],
                                    capture_output=True, text=True)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], self.jobs())

    def test_unexpected_worker_status_stops_without_releasing_ownership(self):
        self.p.add('one', 'One', 'Body')
        self.p.tick()
        state = json.loads(self.fake.read_text())
        state['jobs'][0]['status'] = 'unexpected'
        self.fake.write_text(json.dumps(state))
        with self.assertRaises(TeamError):
            self.p.tick()
        self.assertEqual('running', self.p.show('one')['state'])


    def test_edit_backlog_fields_and_reject_dependency_cycles_atomically(self):
        self.p.add('one', 'Original', 'Body')
        self.p.add('two', 'Dependent', 'Body', deps=['one'])
        updated = self.p.edit('one', title='Revised', priority=0)
        self.assertEqual(('Revised', 0), (updated['title'], updated['priority']))
        with self.assertRaises(TeamError):
            self.p.edit('one', title='Must not save', deps=['two'])
        self.assertEqual('Revised', self.p.show('one')['title'])
        self.assertEqual([], self.p.show('one')['deps'])
        self.p.edit('two', deps=[])
        self.p.edit('one', deps=['two'])
        self.p.tick()
        self.assertEqual('queued', self.p.show('one')['state'])
        self.assertEqual('running', self.p.show('two')['state'])
        with self.assertRaises(TeamError):
            self.p.edit('two', deps=['one'])

    def test_cli_edit_replaces_and_clears_dependencies(self):
        self.p.add('one', 'One', 'Body')
        self.p.add('two', 'Two', 'Body')
        command = [str(ROOT / 'bin/agentctl-team'), '--config', str(self.config), 'issue', 'edit', 'two']
        subprocess.run(command + ['--title', 'New title', '--priority', '1', '--dep', 'one'],
                       check=True, capture_output=True)
        self.assertEqual(['one'], self.p.show('two')['deps'])
        self.assertEqual('New title', self.p.show('two')['title'])
        subprocess.run(command + ['--clear-deps'], check=True, capture_output=True)
        self.assertEqual([], self.p.show('two')['deps'])

    def test_start_rejects_trapped_cycle_but_accepts_review_loop_with_exit(self):
        self.definition['stages']['write']['outcomes'] = {'ready': 'review'}
        self.definition['stages']['review']['outcomes'] = {'changed': 'write'}
        self.write_config()
        result = subprocess.run([str(ROOT / 'bin/agentctl-team'), '--config', str(self.config),
                                 'start', '--once'], capture_output=True, text=True)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], self.jobs())
        self.definition['stages']['review']['outcomes']['passed'] = 'integrate'
        self.write_config()
        self.load_config()

    def test_worker_options_reach_public_launch_command(self):
        # Validate against the real public CLI; fake only the subsequent job transport.
        from agentctl_team.ports import Agentctl
        backend = Agentctl(str(FAKE))
        backend.launch('codex', 'Task', str(self.repo), 'gpt-6-luna', 'options-test',
                       options={'reasoning_effort': 'low'})
        self.assertEqual({'reasoning_effort': 'low'}, self.jobs()[0]['options'])
        self.definition['agentctl'] = str(ROOT / 'bin/agentctl')
        self.definition['stages']['write']['worker']['options'] = {'reasoning_effort': 'low'}
        self.write_config()
        self.load_config()
        self.definition['stages']['write']['worker']['options'] = {'reasoning_effort': 'invalid'}
        self.write_config()
        with self.assertRaises(TeamError):
            self.load_config()


    def test_stage_limit_routes_to_human_before_extra_execution(self):
        self.definition['stages']['review'].update(max=1, overflow='manager')
        self.definition['stages']['manager'] = {'type': 'human', 'outcomes': {'finish': 'done'}}
        self.write_config()
        self.p = Pipeline(self.load_config())
        self.p.add('one', 'One', 'Body')
        self.p.tick()
        self.finish('one', 'ready')
        self.p.tick()
        self.finish('one', 'changed')
        self.p.tick()
        self.p.tick()
        item = self.p.show('one')
        self.assertEqual('waiting-human', item['state'])
        self.assertEqual('manager', item['stage'])
        self.assertEqual(2, len(self.jobs()))
        self.assertEqual('overflow', item['transitions'][-1]['kind'])
        self.assertIn('max=1', item['transitions'][-1]['reason'])

    def test_human_entry_does_not_create_worktree_or_take_slots(self):
        self.definition['entry'] = 'manager'
        self.definition['stages']['manager'] = {'type': 'human', 'outcomes': {'ready': 'write'}}
        self.write_config()
        self.p = Pipeline(self.load_config())
        for item_id in ('one', 'two', 'three'):
            self.p.add(item_id, item_id, 'Body')
        self.p.tick()
        self.assertTrue(all(item['state'] == 'waiting-human' for item in self.p.store.load()['tasks'].values()))
        self.assertEqual([], self.jobs())
        self.assertFalse(Path(self.p.show('one')['worktree']).exists())
        self.assertTrue(self.p.idle())

    def test_notification_sent_once_across_restart(self):
        self.definition['entry'] = 'manager'
        self.notify_target = 'codex:test-manager'
        self.definition['stages']['manager'] = {'type': 'human', 'outcomes': {'ready': 'done'}}
        self.write_config()
        self.p = Pipeline(self.load_config())
        self.p.add('one', 'One', 'Body')
        self.p.tick()
        self.assertEqual(1, len(json.loads(self.fake.read_text())['notifies']))
        restarted = Pipeline(self.load_config())
        restarted.tick()
        notices = restarted.store.load()['notifications']
        self.assertEqual(['sent'], [record['state'] for record in notices.values()])
        self.assertEqual(1, len(json.loads(self.fake.read_text())['notifies']))

    def test_ambiguous_notification_is_not_retried(self):
        self.definition['entry'] = 'manager'
        self.notify_target = 'codex:test-manager'
        self.definition['stages']['manager'] = {'type': 'human', 'outcomes': {'ready': 'done'}}
        self.write_config()
        self.p = Pipeline(self.load_config())
        self.p.add('one', 'One', 'Body')
        self.p.open_human(self.p.show('one'))
        with self.p.store.transaction() as state:
            notice = next(iter(state['notifications'].values()))
            identifier = notice['id']
            notice['state'] = 'sending'  # Crash after transport accepted but before recording.
        with self.assertRaises(TeamError):
            self.p.notify_pending()
        self.assertEqual([], self.jobs())
        self.assertEqual('uncertain', self.p.store.load()['notifications'][identifier]['state'])
        self.p.acknowledge_notification(identifier, 'Inspected recipient and transport records')
        self.p.notify_pending()
        self.assertFalse(self.fake.exists())

    def test_invalid_limits_and_overflow_cycles_are_rejected(self):
        self.definition['stages']['review'].update(max=0, overflow='hold')
        self.write_config()
        with self.assertRaises(TeamError):
            self.load_config()
        self.definition['stages']['review'].update(max=1, overflow='review')
        self.write_config()
        with self.assertRaises(TeamError):
            self.load_config()

    def test_crash_error_names_worktree_branch_and_item(self):
        self.p.add('one', 'One', 'Body')
        item = self.p.show('one')
        Path(item['worktree']).mkdir(parents=True)
        self.p.tick()
        failed = self.p.show('one')
        self.assertEqual('error', failed['state'])
        for detail in (item['worktree'], item['branch'], 'one', 'git worktree list'):
            self.assertIn(detail, failed['error'])
        self.assertTrue(Path(item['worktree']).exists())


    def test_notification_failure_is_visible_and_never_retried_automatically(self):
        self.definition['entry'] = 'manager'
        self.notify_target = 'codex:test-manager'
        self.definition['stages']['manager'] = {'type': 'human', 'outcomes': {'ready': 'done'}}
        self.write_config()
        self.p = Pipeline(self.load_config())
        self.p.add('one', 'One', 'Body')
        os.environ['FAKE_NOTIFY_FAIL'] = '1'
        self.addCleanup(lambda: os.environ.pop('FAKE_NOTIFY_FAIL', None))
        with self.assertRaises(TeamError):
            self.p.tick()
        self.assertEqual('waiting-human', self.p.show('one')['state'])
        notice = next(iter(self.p.store.load()['notifications'].values()))
        self.assertEqual('failed', notice['state'])
        self.assertIn('injected notification transport failure', notice['error'])
        os.environ.pop('FAKE_NOTIFY_FAIL')
        with self.assertRaises(TeamError):
            self.p.tick()
        self.assertEqual(1, len(json.loads(self.fake.read_text())['notifies']))
        self.p.acknowledge_notification(notice['id'], 'Manually resolved transport failure')
        self.p.tick()
        self.assertEqual(1, len(json.loads(self.fake.read_text())['notifies']))

    def test_failed_execution_counts_toward_limit_and_notifies_with_context(self):
        self.definition['stages']['write'].update(max=1, overflow='manager')
        self.definition['stages']['manager'] = {'type': 'human', 'outcomes': {'finish': 'done'}}
        self.notify_target = 'codex:test-manager'
        self.write_config()
        self.p = Pipeline(self.load_config())
        self.p.add('one', 'One', 'Body')
        self.p.tick()
        job = self.p.show('one')['attempt']['job']
        self.finish('one', 'ready', status='failed')
        self.p.tick()
        item = self.p.show('one')
        notice = next(iter(self.p.store.load()['notifications'].values()))
        self.assertEqual('error', notice['kind'])
        self.assertIn(job, notice['message'])
        self.assertIn(item['worktree'], notice['message'])
        self.p.retry('one')
        self.p.tick()
        self.p.tick()
        self.assertEqual('waiting-human', self.p.show('one')['state'])
        self.assertEqual(1, len(self.jobs()))


    def start_cli(self, *args):
        return subprocess.run([str(ROOT / 'bin/agentctl-team'), '--config', str(self.config),
                               'start', '--notify', self.notify_target, *args], capture_output=True, text=True, timeout=10)

    def enable_notifications(self):
        self.notify_target = 'codex:test-manager'
        self.write_config()
        self.p = Pipeline(self.load_config())

    def test_runner_completion_alert_is_durable_and_not_duplicated(self):
        self.enable_notifications()
        result = self.start_cli()
        self.assertEqual(0, result.returncode, result.stderr)
        state = json.loads(result.stdout)
        run = next(iter(state['runs'].values()))
        self.assertEqual('completed', run['reason'])
        notice = state['notifications'][run['notification']]
        self.assertEqual('runner-stopped', notice['kind'])
        self.assertEqual('sent', notice['state'])
        self.p.runner_stopped(run['id'], 'completed')
        self.assertEqual(1, len(json.loads(self.fake.read_text())['notifies']))

    def test_runner_waiting_exit_notifies_after_human_alert(self):
        self.definition['human_wait_timeout_seconds'] = .05
        self.definition['entry'] = 'manager'
        self.definition['stages']['manager'] = {'type': 'human', 'outcomes': {'ready': 'done'}}
        self.enable_notifications()
        result = self.start_cli()
        self.assertEqual(0, result.returncode, result.stderr)
        state = json.loads(result.stdout)
        self.assertEqual('completed', next(iter(state['runs'].values()))['reason'])
        self.p.add('one', 'One', 'Body')
        result = self.start_cli()
        self.assertEqual(0, result.returncode, result.stderr)
        state = json.loads(result.stdout)
        latest = max(state['runs'].values(), key=lambda run: run['started'])
        self.assertEqual('human-wait-timeout', latest['reason'])
        notice = state['notifications'][latest['notification']]
        self.assertIn('timeout elapsed', notice['message'])
        self.assertEqual('waiting-human', state['tasks']['one']['state'])
        notices = sorted(state['notifications'].values(), key=lambda notice: notice['created'])
        self.assertEqual(['runner-stopped', 'human', 'runner-stopped'], [notice['kind'] for notice in notices])

    def test_runner_validation_failure_notifies_with_original_error(self):
        self.enable_notifications()
        (self.repo / '.agentctl-team').mkdir()
        result = self.start_cli()
        self.assertNotEqual(0, result.returncode)
        state = self.p.store.load()
        run = next(iter(state['runs'].values()))
        self.assertEqual('fatal-error', run['reason'])
        self.assertIn('.agentctl-team', run['error'])
        notice = state['notifications'][run['notification']]
        self.assertEqual('sent', notice['state'])
        self.assertIn(run['error'], notice['message'])
        self.assertEqual([], self.jobs())

    def test_failed_stop_notification_is_recorded_and_exit_is_nonzero(self):
        self.enable_notifications()
        os.environ['FAKE_NOTIFY_FAIL'] = '1'
        self.addCleanup(lambda: os.environ.pop('FAKE_NOTIFY_FAIL', None))
        result = self.start_cli()
        self.assertNotEqual(0, result.returncode)
        state = self.p.store.load()
        run = next(iter(state['runs'].values()))
        self.assertEqual('completed', run['reason'])
        self.assertEqual('failed', state['notifications'][run['notification']]['state'])
        self.assertIn('injected notification transport failure', result.stderr)

    def exercise_runner_stop(self, mode):
        self.enable_notifications()
        self.p.add('one', 'One', 'Body')
        process = subprocess.Popen([str(ROOT / 'bin/agentctl-team'), '--config', str(self.config),
                                    'start', '--daemon', '--notify', self.notify_target], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while self.p.show('one')['state'] != 'running':
                if process.poll() is not None or time.monotonic() > deadline:
                    self.fail('runner did not launch the assigned job')
                time.sleep(.02)
            if mode == 'signal':
                process.terminate()
            else:
                self.p.control(stop=mode)
                if mode == 'drain':
                    self.finish('one', 'ready')
            out, err = process.communicate(timeout=5)
            self.assertEqual(0, process.returncode, err)
            return json.loads(out)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_requested_stop_alert_preserves_active_worker(self):
        state = self.exercise_runner_stop('exit')
        self.assertEqual('stop-requested', next(iter(state['runs'].values()))['reason'])
        self.assertEqual('running', state['tasks']['one']['state'])
        self.assertEqual('running', self.jobs()[0]['status'])

    def test_drained_exit_alert_does_not_launch_next_stage(self):
        state = self.exercise_runner_stop('drain')
        self.assertEqual('drained', next(iter(state['runs'].values()))['reason'])
        self.assertEqual('queued', state['tasks']['one']['state'])
        self.assertEqual(1, len(self.jobs()))

    def test_sigterm_alert_preserves_active_worker(self):
        state = self.exercise_runner_stop('signal')
        run = next(iter(state['runs'].values()))
        self.assertEqual('signal', run['reason'])
        self.assertIn('SIGTERM', run['error'])
        self.assertEqual('running', self.jobs()[0]['status'])


    def test_human_report_continues_same_runner_without_restart(self):
        self.definition['entry'] = 'manager'
        self.definition['human_wait_timeout_seconds'] = 2
        self.definition['stages']['manager'] = {'type': 'human', 'outcomes': {'finish': 'done'}}
        self.enable_notifications()
        self.p.add('one', 'One', 'Body')
        process = subprocess.Popen([str(ROOT / 'bin/agentctl-team'), '--config', str(self.config),
                                    'start', '--notify', self.notify_target], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while self.p.show('one')['state'] != 'waiting-human':
                if process.poll() is not None or time.monotonic() > deadline:
                    self.fail('runner did not enter human stage')
                time.sleep(.02)
            time.sleep(.1)
            self.assertIsNone(process.poll())
            report = self.root / 'human-report.md'
            report.write_text('---\noutcome: finish\n---\n\nComplete\n')
            self.p.human_report('one', report)
            out, err = process.communicate(timeout=5)
            self.assertEqual(0, process.returncode, err)
            state = json.loads(out)
            self.assertEqual('done', state['tasks']['one']['state'])
            self.assertEqual(1, len(state['runs']))
            self.assertEqual('completed', next(iter(state['runs'].values()))['reason'])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_human_wait_timeout_default_infinite_and_invalid_values(self):
        self.assertEqual(1800, self.load_config()['human_wait_timeout_seconds'])
        self.definition['human_wait_timeout_seconds'] = None
        self.write_config()
        self.assertIsNone(self.load_config()['human_wait_timeout_seconds'])
        for value in (0, -1, '', False):
            self.definition['human_wait_timeout_seconds'] = value
            self.write_config()
            with self.assertRaises(TeamError):
                self.load_config()


    def test_missing_report_routes_to_human_and_preserves_error(self):
        self.definition['stages']['write']['on_error'] = 'manager'
        self.definition['stages']['manager'] = {'type': 'human', 'outcomes': {'retry': 'write'}}
        self.write_config()
        self.p = Pipeline(self.load_config())
        self.p.add('one', 'One', 'Body')
        self.p.tick()
        jobs = json.loads(self.fake.read_text())
        jobs['jobs'][0]['status'] = 'succeeded'
        self.fake.write_text(json.dumps(jobs))
        self.p.tick()
        item = self.p.show('one')
        self.assertEqual('waiting-human', item['state'])
        self.assertEqual('manager', item['stage'])
        self.assertIn('without a report', item['history'][-1]['error'])
        self.assertEqual('execution-error', item['transitions'][-1]['kind'])
        self.assertIsNone(item['history'][-1]['report'])


    def test_notification_destination_is_per_start_not_config(self):
        self.notify_target = 'codex:first-manager'
        self.write_config()
        self.assertNotIn('notifications', config_load(self.config))
        self.assertEqual('codex:first-manager', self.load_config()['notifications']['target'])
        self.assertNotIn('notifications', config_load(self.config))
        self.definition['notifications'] = {'target': 'codex:stale-manager'}
        self.write_config()
        with self.assertRaises(TeamError):
            config_load(self.config)


    def test_init_generates_valid_editable_workflow_without_workers_and_refuses_overwrite(self):
        config = self.root / 'starter.json'
        command = [str(ROOT / 'bin/agentctl-team'), '--config', str(config), 'init', '--repo', str(self.repo)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        generated = config_load(config)
        self.assertNotIn('notifications', generated)
        self.assertEqual('gpt-6-luna', generated['stages']['work']['worker']['model'])
        self.assertEqual('human_review', generated['stages']['review']['on_error'])
        self.assertTrue(Path(generated['stages']['integration']['files'], 'check-runtime-files.sh').is_file())
        before = config.read_bytes()
        repeated = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(0, repeated.returncode)
        self.assertEqual(before, config.read_bytes())
        self.assertEqual([], self.jobs())



if __name__ == '__main__':
    unittest.main()
