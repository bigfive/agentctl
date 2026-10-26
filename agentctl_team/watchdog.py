"""Independent runner-exit monitor. Never recovers jobs or modifies worktrees."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .pipeline import Pipeline
from .ports import TeamError, LockHeldError


def arm(pipeline, run_id):
    snapshot = pipeline.store.root / (run_id + '-watchdog.json')
    snapshot.write_text(json.dumps(pipeline.config))
    log_path = pipeline.store.root / (run_id + '-watchdog.log')
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parents[1])
    env['PYTHONPATH'] = root + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    with log_path.open('ab') as log:
        child = subprocess.Popen([sys.executable, '-m', 'agentctl_team.watchdog', str(snapshot), run_id],
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True, close_fds=True, env=env)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        record = pipeline.store.load()['runs'][run_id]
        if record.get('watchdog', {}).get('pid') == child.pid:
            return child
        if child.poll() is not None:
            raise TeamError('watchdog failed to start; inspect ' + str(log_path))
        time.sleep(.02)
    child.terminate()
    raise TeamError('watchdog startup was not confirmed; inspect ' + str(log_path))


def observe(pipeline, run_id):
    """Return False once this run no longer needs monitoring."""
    record = pipeline.store.load()['runs'][run_id]
    if record['state'] != 'running':
        return False
    if pipeline.store.lock_held('scheduler'):
        return True
    # Lock acquisition rules out a restart racing with the loss decision.
    try:
        with pipeline.store.lock('scheduler', blocking=False):
            with pipeline.store.transaction() as state:
                record = state['runs'][run_id]
                if record['state'] != 'running':
                    return False
                record.update(state='lost', reason='unexpected-exit', finished=time.time(),
                              error='Scheduler lock was released without a recorded stop.')
                body = ("[Message from: 'agentctl-team watchdog']\n\n"
                        'The runner disappeared without recording a normal stop.\n'
                        'Run: ' + run_id + '\n'
                        'Existing worker executions have not been cancelled. Their current state needs inspection.')
                notice = pipeline.queue_notification(state, None, None, 'runner-lost', body, source_stage='watchdog')
                record['notification'] = notice
            pipeline.store.event('runner-lost', run=run_id)
            # Deliver outside the state lock. Delivery ambiguity remains in outbox.
            pipeline.notify_pending({notice})
            return False
    except LockHeldError:
        return True  # A restart won the scheduler lock race; recheck its state.


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    pipeline = Pipeline(config)
    run_id = sys.argv[2]
    # A per-run lock proves monitor identity without trusting a recycled PID.
    with pipeline.store.lock('watchdog-' + run_id, blocking=False):
        with pipeline.store.transaction() as state:
            state['runs'][run_id]['watchdog'] = {'pid': os.getpid(), 'started': time.time()}
        while observe(pipeline, run_id):
            time.sleep(config['watchdog']['interval_seconds'])


if __name__ == '__main__':
    main()
