"""Process boundaries: git and agentctl's public CLI only."""
import json
import subprocess


class TeamError(Exception):
    pass


class LockHeldError(TeamError):
    pass


def run(argv, cwd=None):
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise TeamError('%s exited %s: %s' % (argv[0], result.returncode, detail))
    return result.stdout


def json_run(argv, cwd=None):
    output = run(argv, cwd)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise TeamError('invalid JSON from %s: %s' % (argv[0], exc)) from exc


TERMINAL = {'succeeded', 'failed', 'cancelled'}


class Agentctl:
    def __init__(self, binary):
        self.binary = binary

    def call(self, *args):
        return json_run([self.binary, *args, '--json'])

    def status(self, job):
        return self.call('status', job)

    def launch(self, harness, prompt, cwd, model, request_id, options=None):
        # The scheduler owns worker completion and manager notifications.
        args = ['start', harness, prompt, '--cwd', cwd, '--no-notify']
        if model is not None:
            args += ['--model', model]
        if options is not None:
            args += ['--options-json', json.dumps(options)]
        record = self.call(*args, '--request-id', request_id)
        if not isinstance(record, dict) or not record.get('job'):
            raise TeamError('agentctl did not return a job')
        return record

    def cancel(self, job):
        return self.call('cancel', job)

    def interrupt(self, job, prompt, request_id):
        return self.call('interrupt', job, prompt, '--request-id', request_id)

    def notify(self, target, message, identifier):
        return self.call('notify', target, '--message', message, '--id', identifier)
