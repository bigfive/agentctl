"""Schedule agentctl runs through configured stages, pools, and outcome routes."""
import json
import math
import os
import re
import shlex
import shutil
import time
import uuid
from pathlib import Path

from .ports import Agentctl, TERMINAL, TeamError, run
from .state import Store
from .messages import load_templates

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
VARIABLE = re.compile(r"{{\s*([a-z_]+)\s*}}")
VARIABLES = {'item_id', 'title', 'stage', 'attempt', 'worktree', 'branch',
             'team_command', 'config_path'}


def render(text, values):
    def replace(match):
        key = match[1]
        if key not in values:
            raise TeamError('unknown template variable: ' + key)
        return str(values[key])
    residue = VARIABLE.sub('', text)
    if '{{' in residue or '}}' in residue:
        raise TeamError('invalid template expression')
    return VARIABLE.sub(replace, text)


def positive(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise TeamError(name + ' must be a finite positive number')


def route_target(route):
    if isinstance(route, str) and route:
        return route
    if (not isinstance(route, dict) or set(route) != {'to', 'reset_count'} or
            not isinstance(route['to'], str) or not route['to'] or
            type(route['reset_count']) is not bool):
        raise TeamError('route must be a destination string or {to: STAGE, reset_count: BOOLEAN}')
    if route['reset_count'] and route['to'] in ('done', 'hold'):
        raise TeamError('reset_count requires a stage destination, not done or hold')
    return route['to']


def validate_routes(stages):
    # Outcomes are worker choices. Detect structurally trapped stages, not
    # whether a worker will eventually choose an available exit.
    escaping = {'done', 'hold'}
    while True:
        reachable = {name for name, stage in stages.items()
                     if any(route_target(target) in escaping for target in stage['outcomes'].values())}
        reachable |= {name for name, stage in stages.items() if stage.get('type', 'llm') == 'human'}
        reachable |= {name for name, stage in stages.items() if stage.get('overflow') in escaping}
        updated = escaping | reachable
        if updated == escaping:
            break
        escaping = updated
    trapped = set(stages) - escaping
    if trapped:
        raise TeamError('stages have no route to done or hold: ' + ', '.join(sorted(trapped)))


def validate_dependencies(tasks):
    visiting, visited = set(), set()
    def visit(item_id):
        if item_id in visiting:
            raise TeamError('dependency cycle involving ' + item_id)
        if item_id in visited:
            return
        visiting.add(item_id)
        for dependency in tasks[item_id]['deps']:
            if dependency not in tasks:
                raise TeamError('unknown dependency: ' + dependency)
            visit(dependency)
        visiting.remove(item_id)
        visited.add(item_id)
    for item_id in tasks:
        visit(item_id)


def validate_overflows(stages):
    # Once every stage in an overflow-only cycle reaches its limit, routing
    # would never execute another stage or reach a waiting state.
    for start in stages:
        seen, current = set(), start
        while current in stages and 'overflow' in stages[current]:
            if current in seen:
                raise TeamError('overflow routing cycle involving ' + current)
            seen.add(current)
            current = stages[current]['overflow']


def notification_config(value):
    if not isinstance(value, dict) or set(value) != {'target'}:
        raise TeamError('notifications requires exactly target')
    target = value['target']
    if not isinstance(target, str) or not re.fullmatch(r'(codex|claude|cursor|opencode):[^\s]+', target):
        raise TeamError('notification target must be HARNESS:NATIVE-SESSION-ID')


def config_load(path, notify=None):
    path = Path(path).resolve(strict=True)
    config = json.loads(path.read_text())
    if isinstance(config, dict) and 'notifications' in config:
        raise TeamError('notifications moved to start --notify HARNESS:NATIVE-SESSION-ID; remove notifications from config')
    optional = {'repo', 'base_branch', 'agentctl', 'worktrees', 'poll_seconds',
                'pools', 'entry', 'launch_message', 'human_wait_timeout_seconds',
                'notification_templates', 'watchdog'}
    if not isinstance(config, dict) or 'stages' not in config or set(config) - optional - {'stages'}:
        raise TeamError('config requires stages and only documented optional fields')
    if not isinstance(config['stages'], dict) or not config['stages']:
        raise TeamError('stages must be a nonempty object')
    config.setdefault('repo', str(path.parent))
    config.setdefault('poll_seconds', 2)
    config.setdefault('pools', {'default': 4})
    config.setdefault('entry', next(iter(config['stages'])))
    config.setdefault('launch_message', 'Read .agentctl-team/brief.md and follow its instructions.')
    if 'agentctl' not in config:
        binary = shutil.which('agentctl')
        if binary is None:
            raise TeamError('agentctl is unavailable on PATH')
        config['agentctl'] = binary
    if not isinstance(config['repo'], str) or not config['repo'].strip():
        raise TeamError('repo must be a nonempty path')
    config['repo'] = str((path.parent / Path(config['repo']).expanduser()).resolve())
    if 'worktrees' not in config:
        common = run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'], config['repo']).strip()
        config['worktrees'] = str(Path(common) / 'agentctl-team' / 'worktrees')
    if 'base_branch' not in config:
        branch = run(['git', 'branch', '--show-current'], config['repo']).strip()
        if not branch:
            raise TeamError('base_branch is required when the repository has a detached HEAD')
        config['base_branch'] = branch
    # Optional timeout defaults to the agreed 30 minutes; null waits indefinitely.
    config.setdefault('human_wait_timeout_seconds', 1800)
    if config['human_wait_timeout_seconds'] is not None:
        positive(config['human_wait_timeout_seconds'], 'human_wait_timeout_seconds')
    # These are documented optional defaults, not recovery from invalid input.
    config.setdefault('watchdog', {'interval_seconds': 5})
    watchdog = config['watchdog']
    if not isinstance(watchdog, dict) or set(watchdog) != {'interval_seconds'}:
        raise TeamError('watchdog requires exactly interval_seconds')
    positive(watchdog['interval_seconds'], 'watchdog.interval_seconds')
    config['_notification_templates'] = load_templates(
        config.get('notification_templates', {}), path.parent, render)
    config['_path'] = str(path)
    for key in ('repo', 'worktrees', 'agentctl'):
        if not isinstance(config[key], str) or not config[key].strip():
            raise TeamError(key + ' must be a nonempty path')
        target = Path(config[key]).expanduser()
        config[key] = str((path.parent / target).resolve())
    if run(['git', 'rev-parse', '--show-toplevel'], config['repo']).strip() != config['repo']:
        raise TeamError('repo must be the repository root')
    if not isinstance(config['base_branch'], str) or not config['base_branch']:
        raise TeamError('base_branch must be specified')
    run(['git', 'rev-parse', '--verify', config['base_branch'] + '^{commit}'], config['repo'])
    if not os.access(config['agentctl'], os.X_OK):
        raise TeamError('agentctl executable is unavailable')
    positive(config['poll_seconds'], 'poll_seconds')
    pools, stages = config['pools'], config['stages']
    if not isinstance(pools, dict) or not pools or not isinstance(stages, dict) or not stages:
        raise TeamError('pools and stages must be nonempty objects')
    for name, slots in pools.items():
        if not NAME.fullmatch(name) or type(slots) is not int or slots < 1:
            raise TeamError('invalid pool: ' + name)
    for name, stage in stages.items():
        if not NAME.fullmatch(name) or name in ('done', 'hold'):
            raise TeamError('invalid stage name: ' + name)
        if not isinstance(stage, dict):
            raise TeamError('stage must be an object: ' + name)
        kind = stage.get('type', 'llm')  # Documented compatibility default.
        if kind not in ('llm', 'human'):
            raise TeamError('stage type must be llm or human: ' + name)
        fields = {'outcomes', 'type', 'max', 'overflow', 'on_error'}
        required_stage = {'outcomes', 'type'} if kind == 'human' else {'worker', 'outcomes'}
        allowed = fields if kind == 'human' else fields | required_stage | {'files', 'prompt', 'pools', 'timeout_seconds'}
        if not required_stage <= set(stage) or set(stage) - allowed:
            raise TeamError('invalid stage fields: ' + name)
        if 'on_error' in stage and (kind != 'llm' or stage['on_error'] not in {*stages, 'hold'}):
            raise TeamError('on_error must name a stage or hold on an LLM stage: ' + name)
        if ('max' in stage) != ('overflow' in stage):
            raise TeamError('max and overflow must be configured together: ' + name)
        if 'max' in stage:
            if type(stage['max']) is not int or stage['max'] < 1:
                raise TeamError('max must be a positive integer: ' + name)
            if stage['overflow'] not in {*stages, 'done', 'hold'}:
                raise TeamError('unknown overflow destination: ' + name)
        outcomes = stage['outcomes']
        if not isinstance(outcomes, dict) or not outcomes:
            raise TeamError('outcomes must be nonempty')
        for outcome, destination in outcomes.items():
            if not NAME.fullmatch(outcome) or route_target(destination) not in {*stages, 'done', 'hold'}:
                raise TeamError('invalid outcome route in ' + name)
        if kind == 'human':
            continue
        stage.setdefault('pools', ['default'])
        stage.setdefault('timeout_seconds', 1800)
        positive(stage['timeout_seconds'], name + '.timeout_seconds')
        if (not isinstance(stage['pools'], list) or not stage['pools'] or
                len(set(stage['pools'])) != len(stage['pools']) or
                any(pool not in pools for pool in stage['pools'])):
            raise TeamError('invalid pools for stage ' + name)
        worker = stage['worker']
        if (not isinstance(worker, dict) or not {'harness', 'model'} <= set(worker) or set(worker) - {'harness', 'model', 'options'} or
                worker['harness'] not in ('codex', 'claude', 'cursor', 'opencode') or
                (worker['model'] is not None and (not isinstance(worker['model'], str) or not worker['model']))):
            raise TeamError('worker requires supported harness and model (null selects harness default)')
        if 'options' in worker:
            if not isinstance(worker['options'], dict):
                raise TeamError('worker.options must be an object')
            run([config['agentctl'], 'validate-options', worker['harness'],
                 '--options-json', json.dumps(worker['options'])])
        if ('files' in stage) == ('prompt' in stage):
            raise TeamError('LLM stage requires exactly one of prompt or files: ' + name)
        if 'prompt' in stage:
            if not isinstance(stage['prompt'], str) or not stage['prompt'].strip():
                raise TeamError('prompt must be nonempty: ' + name)
            render(stage['prompt'], {key: 'validation' for key in VARIABLES})
            continue
        if not isinstance(stage['files'], str) or not stage['files']:
            raise TeamError('files must name a directory')
        directory = (path.parent / stage['files']).resolve(strict=True)
        if not directory.is_dir():
            raise TeamError('files must name a directory')
        stage['files'] = str(directory)
        for required_file in ('brief.md', 'report.md', 'read-issue.sh', 'submit-report.sh'):
            if not (directory / required_file).is_file():
                raise TeamError('missing stage file: ' + str(directory / required_file))
        for source in directory.rglob('*'):
            if source.is_symlink():
                raise TeamError('stage files cannot contain symlinks')
            if source.is_file():
                render(source.read_text(), {key: 'validation' for key in VARIABLES})
        for script in ('read-issue.sh', 'submit-report.sh'):
            if not os.access(directory / script, os.X_OK):
                raise TeamError('stage script is not executable: ' + script)
    validate_routes(stages)
    validate_overflows(stages)
    if notify is not None:
        notification_config({'target': notify})
        config['notifications'] = {'target': notify}
    if not isinstance(config['entry'], str) or config['entry'] not in stages:
        raise TeamError('entry must name a stage')
    if not isinstance(config['launch_message'], str) or not config['launch_message'].strip():
        raise TeamError('launch_message must be nonempty')
    render(config['launch_message'], {key: 'validation' for key in VARIABLES})
    return config


class Pipeline:
    def __init__(self, config):
        self.config = config
        self.store = Store(config)
        # Existing delivery queues require an explicit migration, never reinterpret them.
        with self.store.transaction() as state:
            if state['version'] == 1:
                if state['tasks']:
                    raise TeamError('legacy delivery state exists; use a new repository or migrate it explicitly')
                state.update(version=2, control={'paused': False, 'stop': None})
            if state['version'] != 2:
                raise TeamError('unsupported pipeline state version')
        for item in self.store.load()['tasks'].values():
            if item.get('continuation'):
                raise TeamError('legacy same-session continuation pending for ' + item['id'] +
                                '; inspect state and resolve it before using fresh-session execution')
        self.store.worktrees = Path(config['worktrees'])
        self.agent = Agentctl(config['agentctl'])

    def validate_start(self):
        validate_routes(self.config['stages'])
        validate_dependencies(self.store.load()['tasks'])
        repo = self.config['repo']
        collision = run(['git', 'ls-files', '--', '.agentctl-team'], repo).strip()
        base_collision = run(['git', 'ls-tree', '-r', '--name-only',
                              self.config['base_branch'], '--', '.agentctl-team'], repo).strip()
        if collision or base_collision or (Path(repo) / '.agentctl-team').exists():
            raise TeamError('repository collides with reserved .agentctl-team directory')
        common = Path(run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'], repo).strip())
        exclusion = common / 'info' / 'exclude'
        exclusion.parent.mkdir(parents=True, exist_ok=True)
        existing = exclusion.read_text() if exclusion.exists() else ''
        if '/.agentctl-team/' not in existing.splitlines():
            with exclusion.open('a') as handle:
                handle.write('\n/.agentctl-team/\n')
        self.store.worktrees.mkdir(parents=True, exist_ok=True)
        # A running pipeline uses its recorded definition, not an edited replacement.
        for item in self.store.load()['tasks'].values():
            if item['state'] != 'done' and item['stage'] not in self.config['stages']:
                raise TeamError('queued item references a removed stage: ' + item['stage'])

    def add(self, item_id, title, body, priority=2, deps=()):
        if type(priority) is not int or not 0 <= priority <= 4:
            raise TeamError('priority must be an integer from 0 to 4')
        if len(set(deps)) != len(deps):
            raise TeamError('duplicate dependencies')
        if not NAME.fullmatch(item_id) or not title.strip() or not body.strip():
            raise TeamError('item requires valid id, nonempty title and body')
        with self.store.transaction() as state:
            if item_id in state['tasks']:
                raise TeamError('item already exists')
            if item_id in deps or any(dep not in state['tasks'] for dep in deps):
                raise TeamError('dependencies must identify existing other items')
            item = dict(id=item_id, title=title, body=body, priority=priority,
                        deps=list(deps), stage=self.config['entry'], state='queued',
                        branch='agentctl-team/' + item_id,
                        worktree=str(self.store.worktrees / item_id),
                        prepared=False, attempt=None, history=[], created=time.time(),
                        error=None)
            state['tasks'][item_id] = item
        return item

    def show(self, item_id):
        return self.store.task(self.store.load(), item_id)

    def edit(self, item_id, body=None, title=None, priority=None, deps=None):
        if body is None and title is None and priority is None and deps is None:
            raise TeamError('specify at least one field to edit')
        if body is not None and not body.strip():
            raise TeamError('body must not be empty')
        if title is not None and not title.strip():
            raise TeamError('title must not be empty')
        if priority is not None and (type(priority) is not int or not 0 <= priority <= 4):
            raise TeamError('priority must be an integer from 0 to 4')
        with self.store.transaction() as state:
            item = self.store.task(state, item_id)
            if deps is not None:
                if item['state'] not in ('queued', 'waiting-human', 'held', 'error'):
                    raise TeamError('dependencies cannot change while an item is active or done')
                if len(set(deps)) != len(deps):
                    raise TeamError('duplicate dependencies')
                item['deps'] = list(deps)
                validate_dependencies(state['tasks'])
            for field, value in (('body', body), ('title', title), ('priority', priority)):
                if value is not None:
                    item[field] = value
            return item

    def control(self, **updates):
        with self.store.transaction() as state:
            state['control'].update(updates)

    def retry(self, item_id):
        with self.store.transaction() as state:
            item = self.store.task(state, item_id)
            if item['state'] not in ('held', 'error'):
                raise TeamError('only held or failed executions can be retried; human stages require issue report')
            item.update(state='queued', error=None)

    @staticmethod
    def read_report(path):
        text = Path(path).read_text()
        match = re.fullmatch(r'---\r?\noutcome: ([A-Za-z0-9][A-Za-z0-9_-]*)\r?\n---\r?\n([\s\S]*)', text)
        if not match:
            raise TeamError('report requires front matter containing exactly outcome: NAME')
        return text, match[1]

    @staticmethod
    def record_report(attempt, text, outcome):
        previous = attempt['report']
        if previous:
            if previous['text'] != text:
                raise TeamError('attempt already has a different report')
            return previous['receipt']
        if outcome not in attempt['definition']['outcomes']:
            raise TeamError('outcome is not declared for this stage')
        receipt = 'report_' + uuid.uuid4().hex
        attempt['report'] = dict(outcome=outcome, text=text, receipt=receipt, at=time.time())
        return receipt

    def human_report(self, item_id, path):
        text, outcome = self.read_report(path)
        with self.store.transaction() as state:
            item = self.store.task(state, item_id)
            if item['state'] != 'waiting-human':
                raise TeamError('item is not waiting at a human stage: ' + item_id)
            attempt = item['attempt']
            receipt = self.record_report(attempt, text, outcome)
            self.route(state, item, attempt['definition']['outcomes'][outcome])
        self.store.event('human-report', item=item_id, attempt=attempt['id'], outcome=outcome)
        return receipt

    def report(self, token, path):
        text, outcome = self.read_report(path)
        with self.store.transaction() as state:
            found = [(item, item['attempt']) for item in state['tasks'].values()
                     if item['attempt'] and item['attempt']['id'] == token]
            if len(found) != 1:
                raise TeamError('unknown or stale attempt')
            item, attempt = found[0]
            if attempt['definition'].get('type', 'llm') != 'llm':
                raise TeamError('use issue report for a human stage')
            if not attempt['report'] and item['state'] not in ('launching', 'running'):
                raise TeamError('attempt is no longer active')
            receipt = self.record_report(attempt, text, outcome)
        self.store.event('submitted', item=item['id'], attempt=token, outcome=outcome)
        return receipt

    def prepare_worktree(self, item):
        path = Path(item['worktree'])
        if item['prepared']:
            actual = run(['git', 'branch', '--show-current'], path).strip()
            if actual != item['branch']:
                raise TeamError('unexpected branch in worktree ' + str(path) + ': expected ' +
                                item['branch'] + ', found ' + actual + '; inspect git status before retrying')
            return
        branches = run(['git', 'branch', '--list', item['branch']], self.config['repo']).strip()
        if path.exists() or branches:
            raise TeamError('worktree preparation is unrecorded for item ' + item['id'] +
                            ': path=' + str(path) + ', branch=' + item['branch'] +
                            ', repo=' + self.config['repo'] +
                            '. Inspect git worktree list and git status; preserve any work before manual cleanup or retry.')
        run(['git', 'worktree', 'add', '-b', item['branch'], str(path), self.config['base_branch']], self.config['repo'])
        with self.store.transaction() as state:
            self.store.task(state, item['id'])['prepared'] = True

    def make_attempt(self, item):
        stage = self.config['stages'][item['stage']]
        token = 'attempt_' + uuid.uuid4().hex
        archive = self.store.root / 'attempts' / token
        archive.mkdir(parents=True)
        command = shlex.quote(str(Path(__file__).parents[1] / 'bin' / 'agentctl-team'))
        values = dict(item_id=item['id'], title=item['title'], stage=item['stage'],
                      attempt=token, worktree=item['worktree'], branch=item['branch'],
                      team_command=command, config_path=shlex.quote(self.config['_path']))
        if 'prompt' in stage:
            from .inline import stage_files
            for name, (text, mode) in stage_files(stage).items():
                target = archive / 'files' / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(render(text, values))
                target.chmod(mode)
        else:
            for source in Path(stage['files']).rglob('*'):
                relative = source.relative_to(stage['files'])
                target = archive / 'files' / relative
                if source.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(render(source.read_text(), values))
                    target.chmod(source.stat().st_mode & 0o777)
        prompt = render(self.config['launch_message'], values)
        attempt = dict(id=token, stage=item['stage'], definition=json.loads(json.dumps(stage)),
                       prompt=prompt, archive=str(archive), job=None,
                       request_id='team:' + self.store.repo_id + ':' + token,
                       report=None, started=time.time())
        (archive / 'attempt.json').write_text(json.dumps(attempt, indent=2))
        with self.store.transaction() as state:
            current = self.store.task(state, item['id'])
            if current['attempt']:
                current['history'].append(current['attempt'])
            current.update(attempt=attempt, state='preparing')
        self.install_files(item, attempt)
        return attempt

    def install_files(self, item, attempt):
        destination = Path(item['worktree']) / '.agentctl-team'
        previous = destination.with_name('.agentctl-team.previous')
        if previous.exists():
            raise TeamError('unfinished preparation: ' + str(previous) + '; current files: ' +
                            str(destination) + '; archive: ' + attempt['archive'] +
                            '. Inspect both directories before manually restoring or removing either, then retry.')
        if destination.exists():
            # Preserve worker edits too, before replacing files for the next stage.
            shutil.copytree(destination, Path(attempt['archive']) / 'previous-files')
            destination.rename(previous)
        shutil.copytree(Path(attempt['archive']) / 'files', destination)
        if previous.exists():
            shutil.rmtree(previous)
        with self.store.transaction() as state:
            self.store.task(state, item['id'])['state'] = 'launching'

    def launch(self, item):
        attempt = item['attempt']
        worker = attempt['definition']['worker']
        result = self.agent.launch(worker['harness'], attempt['prompt'], item['worktree'],
                                   worker['model'], attempt['request_id'],
                                   options=worker.get('options'))
        with self.store.transaction() as state:
            current = self.store.task(state, item['id'])
            # A quick worker may have submitted while launch was in progress.
            current['attempt']['job'] = result['job']
            current['state'] = 'running'
        self.store.event('launched', item=item['id'], stage=item['stage'], job=result['job'])

    def attention(self, state, item, kind, detail):
        # Notifications are explicitly optional; absence disables delivery.
        if 'notifications' not in self.config:
            return
        command = shlex.quote(str(Path(__file__).parents[1] / 'bin' / 'agentctl-team'))
        prefix = command + ' --config ' + shlex.quote(self.config['_path'])
        attempts = item['history'] + ([item['attempt']] if item['attempt'] else [])
        reported = next((attempt for attempt in reversed(attempts) if attempt['report']), None)
        source_stage = reported['stage'] if kind == 'human' and reported else item['stage']
        overflow = None
        if item.get('transitions'):
            transition = item['transitions'][-1]
            if (transition['kind'] == 'overflow' and
                    (transition['destination'] == item['stage'] or (kind == 'held' and transition['destination'] == 'hold')) and
                    (not reported or transition['at'] >= reported['report']['at'])):
                overflow = transition
                source_stage = transition['stage']
        lines = ["[Message from: 'agentctl-team " + source_stage + "']", '']
        subject = '“' + item['title'] + '” (' + item['id'] + ')'
        if kind == 'human':
            lines += [subject + ' needs manager action before it can continue.', '']
            if item.get('transitions') and item['transitions'][-1]['kind'] == 'execution-error':
                lines += ['The previous execution failed:', item['transitions'][-1]['reason'], '']
            elif overflow:
                lines += [overflow['reason'], '']
            elif reported:
                # The report is already validated. Quote its prose without the
                # routing front matter; do not invent or paraphrase its question.
                prose = re.split(r'---\r?\n', reported['report']['text'], maxsplit=2)[2].strip()
                if prose:
                    lines += [source_stage + ' reported:', prose, '']
            outcomes = ', '.join(item['attempt']['definition']['outcomes'])
            lines += ['Please read the issue and submit a report for ' + item['stage'] +
                      ' using one of these outcomes: ' + outcomes + '.', '',
                      'Read the issue:', prefix + ' issue show ' + item['id'], '',
                      'Submit your report:', prefix + ' issue report ' + item['id'] + ' --file REPORT.md']
        elif kind == 'error':
            lines += [subject + ' could not continue because an execution error occurred.', '', detail, '',
                      'Read the issue:', prefix + ' issue show ' + item['id'], '',
                      'After resolving the cause, retry:', prefix + ' issue retry ' + item['id']]
        else:
            explanation = overflow['reason'] if overflow else detail
            if reported and not overflow:
                prose = re.split(r'---\r?\n', reported['report']['text'], maxsplit=2)[2].strip()
                if prose:
                    explanation = prose
            lines += [subject + ' is on hold.', '', explanation, '',
                      'Read the issue and previous reports:', prefix + ' issue show ' + item['id'], '',
                      'After resolving the cause, retry:', prefix + ' issue retry ' + item['id']]
        self.queue_notification(state, item['id'], item['stage'], kind, '\n'.join(lines), source_stage=source_stage)

    def queue_notification(self, state, item_id, stage, kind, body, source_stage=None, template_kind=None):
        command = shlex.quote(str(Path(__file__).parents[1] / 'bin' / 'agentctl-team'))
        prefix = command + ' --config ' + shlex.quote(self.config['_path'])
        item = state['tasks'].get(item_id) if item_id is not None else None
        definition = item['attempt']['definition'] if item and item.get('attempt') else None
        values = dict(context=body, source_stage=source_stage or stage or 'runner', item_id=item_id or '',
                      title=item['title'] if item else '', stage=stage or '',
                      outcomes=', '.join(definition['outcomes']) if definition else '',
                      issue_command=prefix + ' issue show ' + shlex.quote(item_id) if item_id else '',
                      report_command=prefix + ' issue report ' + shlex.quote(item_id) + ' --file REPORT.md' if item_id else '',
                      status_command=prefix + ' status --summary',
                      start_command=prefix + ' start --notify ' + shlex.quote(self.config['notifications']['target']),
                      reason=body if item is None else '', error=item.get('error') or '' if item else '')
        body = render(self.config['_notification_templates'][template_kind or kind], values)
        identifier = 'notice_' + uuid.uuid4().hex
        fence = '`' * max(3, max((len(match[0]) for match in re.finditer(r'`+', body)), default=0) + 1)
        state.setdefault('notifications', {})[identifier] = dict(
            id=identifier, item=item_id, stage=stage, kind=kind,
            target=self.config['notifications']['target'],
            message=fence + 'text\n' + body + '\n' + fence,
            state='pending', created=time.time(), job=None, error=None)
        return identifier

    def begin_run(self):
        identifier = 'run_' + uuid.uuid4().hex
        with self.store.transaction() as state:
            # Holding the scheduler lock proves no earlier scheduler still owns it.
            for old in state.setdefault('runs', {}).values():
                if old['state'] == 'running':
                    old.update(state='superseded', finished=time.time())
            state['runs'][identifier] = dict(id=identifier, state='running', started=time.time(),
                pid=os.getpid(), heartbeat=time.time(),
                notify_target=self.config.get('notifications', {}).get('target'))
        return identifier

    def runner_stopped(self, identifier, reason, error=None):
        descriptions = {
            'completed': 'all backlog items are complete',
            'waiting': 'the remaining items are held or have unmet dependencies',
            'human-wait-timeout': 'the human-wait timeout elapsed while human input was still pending',
            'execution-error': 'one or more items encountered execution errors',
            'stop-requested': 'a stop was requested',
            'drained': 'the requested drain completed',
            'cancelled': 'the requested cancellation completed',
            'once': 'the requested diagnostic tick completed',
            'signal': 'it received a termination signal',
            'fatal-error': 'an error prevented it from continuing',
        }
        notice = None
        with self.store.transaction() as state:
            record = state['runs'][identifier]
            if record['state'] == 'stopped':
                return  # Never enqueue a second alert for the same run.
            record.update(state='stopped', reason=reason, error=error, finished=time.time())
            if 'notifications' in self.config:
                counts = {}
                for item in state['tasks'].values():
                    counts[item['state']] = counts.get(item['state'], 0) + 1
                lines = ["[Message from: 'agentctl-team runner']", '',
                         'The runner has stopped because ' + descriptions[reason] + '.', '']
                if reason == 'human-wait-timeout':
                    lines += ['Human-wait timeout: ' + str(self.config['human_wait_timeout_seconds']) + ' seconds.', '']
                if error:
                    lines += [error, '']
                if counts:
                    lines += ['Items: ' + ', '.join(str(count) + ' ' + status for status, count in sorted(counts.items())) + '.', '']
                active = len(self.active(state))
                if active:
                    lines += [str(active) + ' worker execution(s) remain active. Their work has not been cancelled.', '']
                if reason != 'completed':
                    command = shlex.quote(str(Path(__file__).parents[1] / 'bin' / 'agentctl-team'))
                    prefix = command + ' --config ' + shlex.quote(self.config['_path'])
                    lines += ['Read the backlog:', prefix + ' status --summary']
                    if reason in ('waiting', 'human-wait-timeout', 'execution-error', 'fatal-error'):
                        lines += ['', 'To continue after resolving the cause (if still appropriate):', prefix + ' start --notify ' + shlex.quote(self.config['notifications']['target'])]
                template_kind = ('runner-completed' if reason == 'completed' else
                                 'runner-error' if reason in ('execution-error', 'fatal-error') else
                                 'human-wait-timeout' if reason == 'human-wait-timeout' else 'runner-stopped')
                notice = self.queue_notification(state, None, None, 'runner-stopped', '\n'.join(lines), template_kind=template_kind)
                record['notification'] = notice
        self.store.event('runner-stopped', run=identifier, reason=reason, error=error)
        if notice:
            # An earlier failed alert must not prevent attempting this distinct
            # stop alert. Its failure remains recorded and is still raised.
            self.notify_pending({notice})

    def acknowledge_notification(self, identifier, note):
        if not note.strip():
            raise TeamError('notification acknowledgement requires a note')
        with self.store.lock('notifications', blocking=False):
            with self.store.transaction() as state:
                notifications = state.get('notifications', {})
                if identifier not in notifications:
                    raise TeamError('unknown notification: ' + identifier)
                record = notifications[identifier]
                if record['state'] not in ('failed', 'uncertain', 'sending'):
                    raise TeamError('only failed or uncertain notifications need acknowledgement')
                record.update(state='acknowledged', note=note, acknowledged=time.time())

    def operational_error(self, item, error):
        message = (str(error) + '\nItem: ' + item['id'] + '\nStage: ' + item['stage'] +
                   '\nWorktree: ' + item['worktree'] + '\nBranch: ' + item['branch'])
        attempt = item['attempt']
        if attempt:
            message += '\nAttempt: ' + attempt['id'] + '\nJob: ' + str(attempt.get('job'))
        with self.store.transaction() as state:
            current = self.store.task(state, item['id'])
            if current.get('observation_error') != message:
                current['observation_error'] = message
                self.attention(state, current, 'error', message)
        self.notify_pending()
        return message

    def notify_pending(self, identifiers=None):
        # Persist intent before calling the transport. Never silently resend after
        # a lost response: the recipient might already have received the message.
        with self.store.lock('notifications'):
            for record in sorted(self.store.load().get('notifications', {}).values(), key=lambda notice: notice['created']):
                identifier = record['id']
                if identifiers is not None and identifier not in identifiers:
                    continue
                if record['state'] in ('failed', 'uncertain', 'sending'):
                    if record['state'] == 'sending':
                        with self.store.transaction() as state:
                            state['notifications'][identifier].update(state='uncertain',
                                error='runner stopped during delivery; inspect recipient and agentctl records')
                    raise TeamError('notification ' + identifier + ' is ' +
                                    ('uncertain' if record['state'] == 'sending' else record['state']) +
                                    ': ' + str(record['error']) +
                                    '; inspect delivery, then use notification acknowledge')
                if record['state'] != 'pending':
                    continue
                with self.store.transaction() as state:
                    state['notifications'][identifier]['state'] = 'sending'
                try:
                    result = self.agent.notify(record['target'], record['message'], identifier)
                    if result.get('status') != 'succeeded':
                        raise TeamError('notification transport did not confirm acceptance: ' + str(result))
                except (TeamError, OSError, ValueError) as exc:
                    with self.store.transaction() as state:
                        state['notifications'][identifier].update(state='failed', error=str(exc))
                    raise TeamError('notification ' + identifier + ' failed: ' + str(exc) +
                                    '; delivery may be ambiguous; inspect before acknowledgement') from exc
                with self.store.transaction() as state:
                    state['notifications'][identifier].update(state='sent', job=result['job'], sent=time.time(),
                        transport=result.get('transport_delivery'))
                self.store.event('notified', notification=identifier, item=record['item'], job=result['job'])

    def stage_attempts(self, item, stage):
        return sum(attempt['stage'] == stage for attempt in item['history'] +
                   ([item['attempt']] if item['attempt'] else []))

    def executions(self, item, stage):
        # Old ledgers without reset boundaries retain their lifetime counts.
        boundary = item.get('count_resets', {}).get(stage, 0)
        return self.stage_attempts(item, stage) - boundary

    def route(self, state, item, route):
        destination = route_target(route)
        if isinstance(route, dict) and route['reset_count']:
            before = self.executions(item, destination)
            boundary = self.stage_attempts(item, destination)
            item.setdefault('count_resets', {})[destination] = boundary
            item.setdefault('transitions', []).append(dict(
                kind='count-reset', stage=item['stage'], destination=destination,
                reason='Reset execution count for ' + destination + ' from ' + str(before) + ' to zero.',
                previous_count=before, boundary=boundary, at=time.time()))
        if destination == 'hold':
            item['state'] = 'held'
            self.attention(state, item, 'held', 'Workflow routed this item to hold.')
        elif destination == 'done':
            item['state'] = 'done'
        else:
            item.update(stage=destination, state='queued')

    def open_human(self, item):
        stage = self.config['stages'][item['stage']]
        with self.store.transaction() as state:
            current = self.store.task(state, item['id'])
            if current['attempt']:
                current['history'].append(current['attempt'])
            current['attempt'] = dict(id='attempt_' + uuid.uuid4().hex, stage=item['stage'],
                                      definition=json.loads(json.dumps(stage)), job=None,
                                      report=None, started=time.time())
            current['state'] = 'waiting-human'
            self.attention(state, current, 'human', 'Human report required.')
        self.store.event('human-wait', item=item['id'], stage=item['stage'])

    def execution_error(self, state, item, detail, definition):
        attempt = item['attempt']
        if attempt:
            attempt['error'] = detail
        item.setdefault('errors', []).append(dict(stage=item['stage'],
            attempt=attempt['id'] if attempt else None, detail=detail, at=time.time()))
        if 'on_error' in definition:
            destination = definition['on_error']
            item.setdefault('transitions', []).append(dict(kind='execution-error', stage=item['stage'],
                destination=destination, reason=detail, at=time.time()))
            item['error'] = None
            self.route(state, item, destination)
        else:
            item.update(state='error', error=detail)
            self.attention(state, item, 'error', detail)

    def fail(self, item_id, message):
        with self.store.transaction() as state:
            item = self.store.task(state, item_id)
            attempt = item['attempt']
            detail = (message + '\nItem: ' + item_id + '\nStage: ' + item['stage'] +
                      '\nWorktree: ' + item['worktree'] + '\nBranch: ' + item['branch'])
            if attempt:
                detail += '\nAttempt: ' + attempt['id']
                if attempt.get('job'):
                    detail += '\nJob: ' + attempt['job']
                if attempt.get('archive'):
                    detail += '\nArchive: ' + attempt['archive']
            definition = self.config['stages'][item['stage']]
            self.execution_error(state, item, detail, definition)
        self.store.event('error', item=item_id, error=message)

    def observe(self, item):
        attempt = item['attempt']
        record = self.agent.status(attempt['job'])
        status = record['status']
        if status not in TERMINAL | {'pending', 'running'}:
            raise TeamError('unsupported worker status: ' + str(status))
        if status not in TERMINAL:
            if time.time() - attempt['started'] > attempt['definition']['timeout_seconds']:
                self.agent.cancel(attempt['job'])
                with self.store.transaction() as state:
                    self.store.task(state, item['id']).update(state='cancelling', error='attempt timed out')
            return
        with self.store.transaction() as state:
            current = self.store.task(state, item['id'])
            attempt = current['attempt']
            report = attempt['report']
            if current['state'] == 'cancelling' or status != 'succeeded' or not report:
                message = 'worker ' + status + (' without a report' if not report else '')
                if record.get('error'):
                    message += ': ' + str(record['error'])
                detail = (current['error'] or message) + '\nItem: ' + item['id'] + \
                         '\nWorktree: ' + item['worktree'] + '\nJob: ' + attempt['job']
                self.execution_error(state, current, detail, attempt['definition'])
                return
            destination = attempt['definition']['outcomes'][report['outcome']]
            self.route(state, current, destination)
            self.store.event('routed', item=item['id'], outcome=report['outcome'], destination=destination)

    def active(self, state):
        return [item for item in state['tasks'].values()
                if item['state'] in ('preparing', 'launching', 'running', 'cancelling')]

    def tick(self):
        for item in list(self.store.load()['tasks'].values()):
            try:
                if item['state'] == 'preparing':
                    # Interrupted file installation is ambiguous; do not overwrite worker files.
                    self.fail(item['id'], 'interrupted file preparation; inspect the recorded archive and worktree before retrying')
                elif item['state'] == 'launching':
                    self.launch(item)  # Stable request ID prevents duplicate launches.
                elif item['state'] in ('running', 'cancelling'):
                    self.observe(item)
            except (TeamError, OSError, ValueError, KeyError) as exc:
                # Unknown running-job state must retain its capacity until reconciled.
                if item['state'] in ('launching', 'running', 'cancelling'):
                    message = self.operational_error(item, exc)
                    raise TeamError('cannot reconcile active item: ' + message) from exc
                self.fail(item['id'], str(exc))
        self.notify_pending()
        state = self.store.load()
        if state['control']['paused'] or state['control']['stop']:
            return
        used = {pool: 0 for pool in self.config['pools']}
        for item in self.active(state):
            for pool in item['attempt']['definition']['pools']:
                if pool not in used:
                    raise TeamError('active attempt references a removed pool')
                used[pool] += 1
        # Favor items that have already progressed, then priority and arrival.
        waiting = sorted((item for item in state['tasks'].values() if item['state'] == 'queued'),
                         key=lambda item: (-len(item['history']), item['priority'], item['created']))
        for item in waiting:
            if any(state['tasks'][dep]['state'] != 'done' for dep in item['deps']):
                continue
            stage = self.config['stages'][item['stage']]
            if 'max' in stage and self.executions(item, item['stage']) >= stage['max']:
                with self.store.transaction() as current_state:
                    current = self.store.task(current_state, item['id'])
                    reason = ('Stage ' + item['stage'] + ' reached max=' + str(stage['max']) +
                              '; routing to ' + stage['overflow'] + ' before another execution.')
                    current.setdefault('transitions', []).append(dict(kind='overflow', stage=item['stage'],
                        destination=stage['overflow'], reason=reason, at=time.time()))
                    self.route(current_state, current, stage['overflow'])
                self.store.event('overflow', item=item['id'], stage=item['stage'], destination=stage['overflow'])
                continue
            if stage.get('type', 'llm') == 'human':
                self.open_human(item)
                continue
            if any(used[pool] >= self.config['pools'][pool] for pool in stage['pools']):
                continue
            try:
                self.prepare_worktree(item)
                attempt = self.make_attempt(item)
                for pool in stage['pools']:
                    used[pool] += 1
                item['attempt'] = attempt
            except (TeamError, OSError, ValueError) as exc:
                self.fail(item['id'], str(exc))
                continue
            # Failure here may mean the response was lost after launch. Keep ownership
            # and stop; recovery must reuse the recorded request ID.
            try:
                self.launch(item)
            except (TeamError, OSError, ValueError) as exc:
                raise TeamError(self.operational_error(item, exc)) from exc
        self.notify_pending()

    def idle(self):
        state = self.store.load()
        return not self.active(state) and not any(
            item['state'] == 'queued' and all(state['tasks'][dep]['state'] == 'done' for dep in item['deps'])
            for item in state['tasks'].values())
