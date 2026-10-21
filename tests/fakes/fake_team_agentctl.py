#!/usr/bin/env python3
"""Small in-process agentctl substitute for scheduler tests."""
import json
import os
import sys
import time
import uuid
from pathlib import Path

PATH = Path(os.environ['FAKE_AGENTCTL_JOBS'])
ARGS = sys.argv[1:]


def load():
    if PATH.exists():
        return json.loads(PATH.read_text())
    return {'jobs': [], 'notifies': []}


def save(state):
    PATH.write_text(json.dumps(state, indent=2))


def option(name, default=None):
    return ARGS[ARGS.index(name) + 1] if name in ARGS else default


def emit(value):
    print(json.dumps(value))


def job_record(state, harness, prompt, cwd, request_id, parent=None):
    existing = next((job for job in state['jobs'] if job.get('request_id') == request_id), None)
    if existing:
        return existing
    parent_job = next((job for job in state['jobs'] if job['job'] == parent), None) if parent else None
    identifier = 'job_' + uuid.uuid4().hex[:12]
    stream = PATH.parent / ('stream-' + identifier + '.jsonl')
    stream.write_text('')
    job = {
        'job': identifier,
        'status': 'running',
        'harness': harness,
        'prompt': prompt,
        'options': json.loads(option('--options-json', '{}')),
        'cwd': cwd,
        'request_id': request_id,
        'native_session_id': parent_job['native_session_id'] if parent_job else 'ses-' + uuid.uuid4().hex,
        'parent': parent,
        'followup_of': parent,
        'created_at': time.time(),
        'started_at': time.time(),
        'finished_at': None,
        'stream': str(stream),
    }
    state['jobs'].append(job)
    return job


def main():
    state = load()
    command = ARGS[0]
    if command == 'start':
        job = job_record(state, ARGS[1], ARGS[2], option('--cwd'), option('--request-id'))
        save(state)
        emit(job)
    elif command == 'followup':
        parent = ARGS[1]
        job = job_record(state, 'codex', ARGS[2],
                         next((j['cwd'] for j in state['jobs'] if j['job'] == parent), None),
                         option('--request-id'), parent)
        save(state)
        emit(job)
    elif command == 'status':
        job = next((j for j in state['jobs'] if j['job'] == ARGS[1]), None)
        if job is None:
            sys.exit(1)
        emit(job)
    elif command == 'list':
        emit(state['jobs'])
    elif command == 'cancel':
        job = next((j for j in state['jobs'] if j['job'] == ARGS[1]), None)
        if job is None:
            sys.exit(1)
        if job['status'] == 'running':
            job['status'] = 'cancelled'
            job['finished_at'] = time.time()
        save(state)
        emit(job)
    elif command == 'notify':
        record = {'target': ARGS[1], 'message': option('--message'), 'id': option('--id'),
                  'job': 'job_notify_' + uuid.uuid4().hex[:8], 'status': 'succeeded'}
        state['notifies'].append(record)
        save(state)
        if os.environ.get('FAKE_NOTIFY_FAIL'):
            print('injected notification transport failure', file=sys.stderr)
            sys.exit(1)
        emit(record)
    else:
        print('unsupported fake agentctl command: ' + command, file=sys.stderr)
        sys.exit(1)


main()
