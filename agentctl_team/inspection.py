"""Bounded manager view built from current locks, ledger, and job observations."""
import re
import time
from collections import Counter


def summary(pipeline):
    state = pipeline.store.load()
    runs = sorted(state.get('runs', {}).values(), key=lambda run: run['started'])
    latest = runs[-1] if runs else None
    held = pipeline.store.lock_held('scheduler')
    if held:
        health = ('alive' if latest and latest['state'] == 'running' else
                  'stopping (scheduler lock held)' if latest and latest['state'] == 'stopped' else
                  'scheduler lock held; run record unavailable')
    elif latest and latest['state'] in ('running', 'lost'):
        health = 'unexpectedly missing'
    else:
        health = 'stopped' if latest else 'not started'
    lines = ['Runner: ' + health]
    if latest:
        lines += ['Run: ' + latest['id']]
        if latest.get('reason'):
            lines += ['Reason: ' + latest['reason']]
        watcher = latest.get('watchdog')
        if watcher:
            lines += ['Watchdog: ' + ('alive' if pipeline.store.lock_held('watchdog-' + latest['id']) else 'not running') +
                      ' (PID ' + str(watcher['pid']) + ')']
        elif latest['state'] == 'running':
            lines += ['Watchdog: not armed (diagnostic tick, notifications disabled, or startup incomplete)']
    tasks = sorted(state['tasks'].values(), key=lambda item: (item['priority'], item['created']))
    counts = Counter(item['state'] for item in tasks)
    stages = Counter(item['stage'] for item in tasks if item['state'] != 'done')
    lines += ['Items: ' + (', '.join(str(n) + ' ' + k for k, n in sorted(counts.items())) or 'none'),
              'Stages (unfinished): ' + (', '.join(k + '=' + str(n) for k, n in sorted(stages.items())) or 'none'),
              '', 'Manager actions:']
    attention = [item for item in tasks if item['state'] in ('waiting-human', 'error', 'held') or item.get('observation_error')]
    for item in attention:
        attempts = item['history'] + ([item['attempt']] if item['attempt'] else [])
        evidence = [(attempt['report']['at'], re.split(r'---\r?\n', attempt['report']['text'], maxsplit=2)[2].strip())
                    for attempt in attempts if attempt.get('report')]
        evidence += [(error['at'], error['detail']) for error in item.get('errors', [])]
        evidence += [(transition['at'], transition['reason']) for transition in item.get('transitions', [])
                     if transition['kind'] == 'overflow']
        latest_evidence = max(evidence, key=lambda entry: entry[0])[1] if evidence else 'Read issue history and transitions.'
        detail = item.get('observation_error') or item.get('error') or latest_evidence
        lines += [item['id'] + ' [' + item['stage'] + ', ' + item['state'] + '] ' + item['title'],
                  '  ' + ' '.join(detail.split())[:700]]
    if not attention:
        lines += ['  none']
    blocked = [(item['id'], [dep for dep in item['deps'] if state['tasks'][dep]['state'] != 'done'])
               for item in tasks if item['state'] == 'queued']
    for identifier, deps in blocked:
        if deps:
            lines += [identifier + ': waiting for ' + ', '.join(deps)]
    lines += ['', 'Active jobs:']
    active = pipeline.active(state)
    for item in active:
        attempt = item['attempt']
        job = attempt.get('job')
        observation = 'launch not confirmed'
        if job:
            try:
                observation = pipeline.agent.status(job)['status']
            except Exception as exc:
                observation = 'status unavailable: ' + str(exc)
        elapsed = max(0, int(time.time() - attempt['started']))
        lines += [item['id'] + ' [' + item['stage'] + '] ' + str(job) +
                  ': ' + observation + ', ' + str(elapsed) + 's elapsed']
    if not active:
        lines += ['  none']
    notices = sorted(state.get('notifications', {}).values(), key=lambda notice: notice['created'])
    unresolved = [notice for notice in notices if notice['state'] not in ('sent', 'acknowledged')]
    recent = notices[-5:]
    visible = {notice['id']: notice for notice in unresolved + recent}
    lines += ['', 'Notifications (recent and unresolved):']
    for notice in sorted(visible.values(), key=lambda notice: notice['created']):
        transport = notice.get('transport')
        label = transport.get('transport') if isinstance(transport, dict) else 'transport detail unavailable'
        lines += [notice['id'] + ': ' + notice['state'] + ', ' + label]
        if isinstance(transport, dict) and transport.get('steer_warning'):
            lines += ['  Queue fallback: ' + transport['steer_warning'][:500]]
        if notice.get('error'):
            lines += ['  ' + notice['error'][:500]]
    if not visible:
        lines += ['  none']
    return '\n'.join(lines)
