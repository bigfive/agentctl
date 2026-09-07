"""Command line surface.

    agentctl start <harness> <prompt> [--cwd DIR] [--model M]
    agentctl asyncwait <job> --notify <harness>:<session-id>
    agentctl wait <job> [--rewake] [--timeout SECONDS]
    agentctl status <job>
    agentctl read <job> [--stream | --stderr]
    agentctl followup <job> <prompt>
    agentctl cancel <job>
    agentctl list
    agentctl daemon status|start|stop|run

``start`` prints the job handle on stdout and nothing else, so a shell can
capture it with ``a=$(agentctl start ...)``. Every other command prints a
human-readable block, or JSON with ``--json``.
"""

import argparse
import json
import os
import sys
import time

from . import __version__, config, envelope, ipc, notify, store
from .errors import AgentctlError, ExitCode, UsageError

POLL_SECONDS = 0.5


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return ExitCode.USAGE
    try:
        return args.handler(args)
    except AgentctlError as exc:
        _fail(exc)
        return exc.exit_code
    except KeyboardInterrupt:
        sys.stderr.write("agentctl: interrupted\n")
        return 130


def _fail(exc):
    sys.stderr.write("agentctl: %s\n" % exc.message)
    if exc.hint:
        sys.stderr.write("hint: %s\n" % exc.hint)


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="agentctl",
        description="Start work in another agent harness, watch it, and "
                    "notify the parent session when it finishes.")
    parser.add_argument("--version", action="version",
                        version="agentctl %s" % __version__)
    sub = parser.add_subparsers(dest="command")

    start = sub.add_parser("start", help="start a child session")
    start.add_argument("harness", choices=sorted(config.HARNESSES))
    start.add_argument("prompt")
    start.add_argument("--cwd", default=None,
                       help="working directory for the child (default: here)")
    start.add_argument("--model", default=None)
    start.add_argument("--json", action="store_true")
    start.set_defaults(handler=cmd_start)

    asyncwait = sub.add_parser(
        "asyncwait",
        help="register a durable wait and return immediately")
    asyncwait.add_argument("job")
    asyncwait.add_argument("--notify", required=True,
                           metavar="HARNESS:SESSION-ID",
                           help="where to deliver the completion, "
                                "e.g. codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3")
    asyncwait.add_argument("--json", action="store_true")
    asyncwait.set_defaults(handler=cmd_asyncwait)

    wait = sub.add_parser("wait", help="block until a job is terminal")
    wait.add_argument("job", nargs="+")
    wait.add_argument("--rewake", action="store_true",
                      help="write the envelope to stderr and exit 2, for use "
                           "as a Claude asyncRewake hook")
    wait.add_argument("--timeout", type=float, default=None,
                      help="seconds to wait before giving up")
    wait.set_defaults(handler=cmd_wait)

    status = sub.add_parser("status", help="show a job snapshot")
    status.add_argument("job")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=cmd_status)

    read = sub.add_parser("read", help="read a job's result or raw output")
    read.add_argument("job")
    group = read.add_mutually_exclusive_group()
    group.add_argument("--stream", action="store_true",
                       help="print the child's raw event stream")
    group.add_argument("--stderr", action="store_true",
                       help="print the child's stderr")
    read.set_defaults(handler=cmd_read)

    followup = sub.add_parser(
        "followup", help="send another turn into a job's native session")
    followup.add_argument("job")
    followup.add_argument("prompt")
    followup.add_argument("--json", action="store_true")
    followup.set_defaults(handler=cmd_followup)

    cancel = sub.add_parser("cancel", help="cancel a running job")
    cancel.add_argument("job")
    cancel.add_argument("--json", action="store_true")
    cancel.set_defaults(handler=cmd_cancel)

    listing = sub.add_parser("list", help="list known jobs")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(handler=cmd_list)

    daemon = sub.add_parser("daemon", help="inspect or control the daemon")
    daemon.add_argument("action",
                        choices=["status", "start", "stop", "run"])
    daemon.add_argument("--json", action="store_true")
    daemon.set_defaults(handler=cmd_daemon)

    return parser


# ---- commands ----------------------------------------------------------

def cmd_start(args):
    record = ipc.call("start", harness=args.harness, prompt=args.prompt,
                      cwd=os.path.abspath(args.cwd or os.getcwd()),
                      model=args.model)
    if args.json:
        print(json.dumps(record, indent=2, sort_keys=True))
    else:
        # Bare handle, so a=$(agentctl start ...) captures exactly this.
        print(record["job"])
    if record["status"] == store.FAILED:
        sys.stderr.write("agentctl: %s\n" % (record.get("error") or "failed"))
        return ExitCode.CHILD_FAILED
    return ExitCode.OK


def cmd_asyncwait(args):
    wait = ipc.call("asyncwait", job=args.job, notify=args.notify)
    if args.json:
        print(json.dumps(wait, indent=2, sort_keys=True))
        return ExitCode.OK
    print("wait: %s" % wait["wait"])
    print("job: %s" % args.job)
    print("notify: %s:%s" % (wait["notify_harness"], wait["notify_id"]))
    print("delivery: %s" % wait["delivery"]["state"])
    if wait["delivery"]["state"] == store.DELIVERY_FAILED:
        sys.stderr.write("agentctl: delivery failed: %s\n"
                         % wait["delivery"].get("error"))
        return ExitCode.DELIVERY_FAILED
    return ExitCode.OK


def cmd_wait(args):
    ipc.ensure_daemon()
    deadline = None if args.timeout is None else time.time() + args.timeout
    records = []
    for job_id in args.job:
        record = _block_until_terminal(job_id, deadline)
        records.append(record)

    messages = [envelope.build(record, store.read_result(record["job"]))
                for record in records]
    text = "\n\n".join(messages)

    if args.rewake:
        # Claude's asyncRewake contract: the hook's stderr becomes the system
        # reminder, and exit code 2 is what wakes the session.
        sys.stderr.write(text + "\n")
        return ExitCode.REWAKE

    print(text)
    if any(record["status"] == store.CANCELLED for record in records):
        return ExitCode.CHILD_CANCELLED
    if any(record["status"] == store.FAILED for record in records):
        return ExitCode.CHILD_FAILED
    return ExitCode.OK


def _block_until_terminal(job_id, deadline):
    record = store.read(job_id)
    while not store.is_terminal(record):
        if deadline is not None and time.time() > deadline:
            raise AgentctlError(
                "timed out waiting for %s (status %s)"
                % (job_id, record["status"]),
                hint="the job is still tracked; check 'agentctl status %s'"
                     % job_id)
        time.sleep(POLL_SECONDS)
        record = store.read(job_id)
    return record


def cmd_status(args):
    record = ipc.call("status", job=args.job)
    if args.json:
        print(json.dumps(record, indent=2, sort_keys=True))
        return ExitCode.OK
    print("job: %s" % record["job"])
    print("harness: %s" % record["harness"])
    print("child: %s" % envelope.child_handle(record))
    print("status: %s" % record["status"])
    if record.get("followup_of"):
        print("followup_of: %s" % record["followup_of"])
    print("cwd: %s" % record["cwd"])
    print("started: %s" % _stamp(record.get("started_at")))
    print("finished: %s" % _stamp(record.get("finished_at")))
    if record.get("error"):
        print("error: %s" % record["error"])
    for wait in record.get("waits", []):
        delivery = wait["delivery"]
        line = "wait %s -> %s:%s [%s]" % (
            wait["wait"], wait["notify_harness"], wait["notify_id"],
            delivery["state"])
        if delivery.get("notification_id"):
            line += " %s" % delivery["notification_id"]
        print(line)
        if delivery.get("error"):
            print("  delivery error: %s" % delivery["error"])
    return ExitCode.OK


def cmd_read(args):
    record = ipc.call("status", job=args.job)
    if args.stream:
        sys.stdout.write(_slurp(store.stream_path(record["job"])))
        return ExitCode.OK
    if args.stderr:
        sys.stdout.write(_slurp(store.stderr_path(record["job"])))
        return ExitCode.OK

    result = store.read_result(record["job"])
    if result.strip():
        sys.stdout.write(result if result.endswith("\n") else result + "\n")
    elif record.get("error"):
        sys.stdout.write("%s\n" % record["error"])
    else:
        sys.stdout.write("no result recorded for %s (status %s)\n"
                         % (record["job"], record["status"]))
    if record["status"] == store.FAILED:
        return ExitCode.CHILD_FAILED
    if record["status"] == store.CANCELLED:
        return ExitCode.CHILD_CANCELLED
    return ExitCode.OK


def cmd_followup(args):
    child = ipc.call("followup", job=args.job, prompt=args.prompt)
    if args.json:
        print(json.dumps(child, indent=2, sort_keys=True))
    else:
        print(child["job"])
    return ExitCode.OK


def cmd_cancel(args):
    record = ipc.call("cancel", job=args.job)
    if args.json:
        print(json.dumps(record, indent=2, sort_keys=True))
    else:
        print("job: %s" % record["job"])
        print("status: %s" % record["status"])
    return ExitCode.OK


def cmd_list(args):
    records = ipc.call("list")
    if args.json:
        print(json.dumps(records, indent=2, sort_keys=True))
        return ExitCode.OK
    if not records:
        print("no jobs")
        return ExitCode.OK
    for record in records:
        waits = len(record.get("waits", []))
        print("%-18s %-9s %-10s %-4s %s" % (
            record["job"], record["harness"], record["status"],
            "w=%d" % waits, envelope.child_handle(record)))
    return ExitCode.OK


def cmd_daemon(args):
    if args.action == "run":
        from .daemon import Daemon
        ipc.serve(Daemon())
        return ExitCode.OK
    if args.action == "start":
        started = ipc.ensure_daemon()
        print("daemon %s (pid %s)"
              % ("started" if started else "already running", ipc.daemon_pid()))
        return ExitCode.OK
    if args.action == "stop":
        if not ipc.is_running():
            print("daemon not running")
            return ExitCode.OK
        ipc.call("shutdown")
        print("daemon stopping")
        return ExitCode.OK

    running = ipc.is_running()
    info = {
        "running": running,
        "pid": ipc.daemon_pid() if running else None,
        "socket": config.socket_path(),
        "home": config.home(),
        "log": config.daemon_log_path(),
        "opencode_url": config.opencode_base_url(),
    }
    if args.json:
        print(json.dumps(info, indent=2, sort_keys=True))
    else:
        for key in ("running", "pid", "socket", "home", "log", "opencode_url"):
            print("%s: %s" % (key, info[key]))
    return ExitCode.OK if running else ExitCode.DAEMON_UNAVAILABLE


# ---- helpers -----------------------------------------------------------

def _stamp(value):
    if not value:
        return "-"
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(value))


def _slurp(path):
    try:
        with open(path) as handle:
            return handle.read()
    except IOError:
        return ""
