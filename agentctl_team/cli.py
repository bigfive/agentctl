"""Public pipeline and prepared-worker commands."""
import argparse
import json
import signal
import sys
import time
from pathlib import Path

from .pipeline import Pipeline, config_load
from .ports import TeamError


def parser():
    p = argparse.ArgumentParser(prog='agentctl-team')
    p.add_argument('--config', required=True, help='pipeline JSON definition')
    commands = p.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='create editable starter config and stage files without launching workers')
    init.add_argument('--repo', default='.', help='Git root; defaults to current directory')
    init.add_argument('--model', default='gpt-6-luna', help='Codex model; defaults to gpt-6-luna')
    start = commands.add_parser('start')
    start.add_argument('--daemon', action='store_true')
    start.add_argument('--once', action='store_true')
    delivery = start.add_mutually_exclusive_group(required=True)
    delivery.add_argument('--notify', help='required notification destination: HARNESS:NATIVE-SESSION-ID')
    delivery.add_argument('--no-notifications', action='store_true', help='explicitly disable new notifications for this invocation')
    commands.add_parser('doctor')
    status = commands.add_parser('status')
    status.add_argument('--summary', action='store_true', help='runner health, manager actions, active jobs, and notification delivery')
    commands.add_parser('pause')
    commands.add_parser('resume')
    stop = commands.add_parser('stop')
    stop_mode = stop.add_mutually_exclusive_group()
    stop_mode.add_argument('--drain', action='store_true')
    stop_mode.add_argument('--cancel-active', action='store_true')
    report = commands.add_parser('report')
    report.add_argument('--attempt', required=True)
    report.add_argument('--file', required=True)
    issue = commands.add_parser('issue').add_subparsers(dest='action', required=True)
    add = issue.add_parser('add')
    add.add_argument('--id', required=True)
    add.add_argument('--title', required=True)
    add.add_argument('--body-file', required=True)
    add.add_argument('--priority', type=int, default=2, help='optional; default 2, lower first')
    add.add_argument('--dep', action='append', default=[], help='optional; no dependencies if absent')
    listing = issue.add_parser('list')
    listing.add_argument('--json', action='store_true', help='full item records as JSON')
    for action in ('show', 'edit', 'retry', 'report', 'interrupt'):
        sub = issue.add_parser(action)
        sub.add_argument('id')
        if action == 'interrupt':
            sub.add_argument('correction')
        if action == 'edit':
            sub.add_argument('--body-file')
            sub.add_argument('--title')
            sub.add_argument('--priority', type=int)
            dependencies = sub.add_mutually_exclusive_group()
            dependencies.add_argument('--dep', action='append', help='replace dependencies; repeat for each item')
            dependencies.add_argument('--clear-deps', action='store_true')
        if action == 'report':
            sub.add_argument('--file', required=True)
    notification = commands.add_parser('notification').add_subparsers(dest='notification_action', required=True)
    notification.add_parser('list')
    acknowledge = notification.add_parser('acknowledge')
    acknowledge.add_argument('id')
    acknowledge.add_argument('--note', required=True)
    return p


def print_issue_table(items):
    if not items:
        print('No issues.')
        return
    items = sorted(items, key=lambda item: (item['priority'], item['created'], item['id']))
    rows = [('ID', 'PRI', 'STATUS', 'STAGE', 'TITLE')]
    rows.extend((item['id'], 'P' + str(item['priority']), item['state'],
                 item['stage'], ' '.join(item['title'].split())) for item in items)
    widths = [max(len(row[column]) for row in rows) for column in range(4)]
    for index, row in enumerate(rows):
        print('  '.join(value.ljust(width) for value, width in zip(row[:4], widths))
              + '  ' + row[4])
        if index == 0:
            print('  '.join('─' * width for width in widths) + '  ' + '─' * 5)
    print('\n%d issue%s' % (len(items), '' if len(items) == 1 else 's'))


def schedule(pipeline, once=False, daemon=False):
    stopping = None

    def stop(number, *_):
        nonlocal stopping
        stopping = signal.Signals(number).name

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    with pipeline.store.lock('scheduler', blocking=False):
        run_id = pipeline.begin_run()
        reason, failure = None, None
        human_wait_started = None
        watchdog = None
        try:
            pipeline.validate_start()
            pipeline.control(stop=None)
            if 'notifications' in pipeline.config and not once:
                from .watchdog import arm
                watchdog = arm(pipeline, run_id)
            print('agentctl-team: runner started.', file=sys.stderr, flush=True)
            if daemon:
                print('This command stays running. Use a background process or service manager; '
                      'do not wait on it in an LLM manager turn.', file=sys.stderr, flush=True)
            if 'notifications' in pipeline.config:
                print('Notifications may steer an active Codex turn or queue until it settles. '
                      'Complete available manager actions, confirm work resumed, then end your turn '
                      'when waiting for workers or a necessary user decision. '
                      'Launching the runner does not complete the user request. '
                      'Use status --summary to inspect current state.', file=sys.stderr, flush=True)
            while not stopping:
                with pipeline.store.transaction() as current:
                    current['runs'][run_id]['heartbeat'] = time.time()
                pipeline.tick()
                state = pipeline.store.load()
                control = state['control']['stop']
                if control == 'cancel':
                    for item in pipeline.active(state):
                        with pipeline.store.transaction() as current:
                            active = current['tasks'][item['id']]
                            if active['state'] == 'interrupting':
                                raise TeamError('resolve the pending interruption before cancelling active work')
                            if active['state'] in ('running', 'cancelling', 'launching') and active['attempt']['job']:
                                pipeline.agent.cancel(active['attempt']['job'])
                                active.update(state='cancelling', error='cancelled by operator')
                if once:
                    reason = 'once'
                    break
                if control == 'exit':
                    reason = 'stop-requested'
                    break
                if control in ('drain', 'cancel') and not pipeline.active(pipeline.store.load()):
                    reason = 'drained' if control == 'drain' else 'cancelled'
                    break
                idle = pipeline.idle()
                tasks = list(pipeline.store.load()['tasks'].values())
                awaiting_human = idle and any(item['state'] == 'waiting-human' for item in tasks)
                has_error = any(item['state'] == 'error' for item in tasks)
                if awaiting_human and not has_error:
                    if human_wait_started is None:
                        human_wait_started = time.monotonic()
                    timeout = pipeline.config['human_wait_timeout_seconds']
                    if timeout is not None and time.monotonic() - human_wait_started >= timeout:
                        reason = 'human-wait-timeout'
                        break
                else:
                    human_wait_started = None
                    if not daemon and idle:
                        reason = ('execution-error' if has_error
                                  else 'completed' if all(item['state'] == 'done' for item in tasks)
                                  else 'waiting')
                        break
                time.sleep(pipeline.config['poll_seconds'])
            if stopping:
                reason = 'signal'
                failure = 'Received ' + stopping + '.'
        except Exception as exc:
            reason, failure = 'fatal-error', str(exc)
            raise
        finally:
            try:
                pipeline.runner_stopped(run_id, reason, failure)
            except Exception as notification_error:
                if failure:
                    raise TeamError(failure + '\nAlso failed to deliver runner-stopped notification: ' +
                                    str(notification_error)) from notification_error
                raise
            finally:
                if watchdog is not None:
                    if watchdog.poll() is None:
                        watchdog.terminate()
                    watchdog.wait(timeout=5)
    return pipeline.store.load()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'init':
            from .initialize import initialize
            print(json.dumps(initialize(args.config, args.repo, args.model), indent=2))
            return 0
        pipeline = Pipeline(config_load(args.config, notify=getattr(args, 'notify', None)))
        command = args.command
        result = None
        if command == 'start':
            result = schedule(pipeline, args.once, args.daemon)
        elif command == 'doctor':
            with pipeline.store.lock('scheduler', blocking=False):
                pipeline.validate_start()
            result = {'valid': True}
        elif command == 'status':
            if args.summary:
                from .inspection import summary
                print(summary(pipeline))
            else:
                result = pipeline.store.load()
        elif command in ('pause', 'resume'):
            pipeline.control(paused=command == 'pause')
        elif command == 'stop':
            if args.drain and args.cancel_active:
                raise TeamError('choose drain or cancel-active')
            pipeline.control(stop='cancel' if args.cancel_active else 'drain' if args.drain else 'exit')
        elif command == 'report':
            result = {'receipt': pipeline.report(args.attempt, args.file)}
        elif command == 'issue':
            if args.action == 'add':
                result = pipeline.add(args.id, args.title, Path(args.body_file).read_text(), args.priority, args.dep)
            elif args.action == 'list':
                items = list(pipeline.store.load()['tasks'].values())
                if args.json:
                    result = items
                else:
                    print_issue_table(items)
            elif args.action == 'show':
                result = pipeline.show(args.id)
            elif args.action == 'edit':
                result = pipeline.edit(args.id,
                    body=Path(args.body_file).read_text() if args.body_file is not None else None,
                    title=args.title, priority=args.priority,
                    deps=[] if args.clear_deps else args.dep)
            elif args.action == 'interrupt':
                result = pipeline.interrupt(args.id, args.correction)
            elif args.action == 'retry':
                pipeline.retry(args.id)
            elif args.action == 'report':
                result = {'receipt': pipeline.human_report(args.id, args.file)}
        elif command == 'notification':
            if args.notification_action == 'list':
                result = list(pipeline.store.load().get('notifications', {}).values())
            else:
                pipeline.acknowledge_notification(args.id, args.note)
        if result is not None:
            print(json.dumps(result, indent=2))
        if command == 'start' and any(item['state'] == 'error' for item in result['tasks'].values()):
            return 1
        return 0
    except (TeamError, OSError, ValueError, KeyError, TypeError) as exc:
        print('agentctl-team: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
