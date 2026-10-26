"""One JSON queue, one lock, one append-only event log. No database."""
import contextlib
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path

from .ports import TeamError, LockHeldError, run

class Store:
    def __init__(self, config):
        common = run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                     config['repo']).strip()
        self.config = config
        self.root = Path(common) / 'agentctl-team'
        self.root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.path = self.root / 'state.json'
        self.events_path = self.root / 'events.jsonl'
        self.repo_id = hashlib.sha256(str(config['repo']).encode()).hexdigest()[:12]
        self.worktrees = Path(config['worktrees'])
        if not self.path.exists():
            with self.lock('state'):
                if not self.path.exists():
                    self._write({'version': 2, 'tasks': {}, 'control': self._control()})

    @staticmethod
    def _control():
        return {'paused': False, 'stop': None}

    def load(self):
        try:
            with self.path.open(encoding='utf-8') as handle:
                state = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise TeamError('cannot read queue state: %s' % exc) from exc
        if not isinstance(state, dict) or not isinstance(state.get('tasks'), dict):
            raise TeamError('queue state is malformed')
        return state

    def _write(self, state):
        tmp = self.path.with_name(self.path.name + '.tmp')
        with tmp.open('w', encoding='utf-8') as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    @contextlib.contextmanager
    def lock(self, name, blocking=True):
        with (self.root / (name + '.lock')).open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError as exc:
                raise LockHeldError('%s lock is held by another process' % name) from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def lock_held(self, name):
        with (self.root / (name + '.lock')).open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False

    @contextlib.contextmanager
    def transaction(self):
        with self.lock('state'):
            state = self.load()
            yield state
            self._write(state)

    def event(self, kind, **fields):
        record = {'at': time.time(), 'kind': kind}
        record.update(fields)
        line = json.dumps(record, sort_keys=True) + '\n'
        fd = os.open(self.events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)

    def task(self, state, task_id):
        task = state['tasks'].get(task_id)
        if task is None:
            raise TeamError('unknown task: ' + task_id)
        return task

