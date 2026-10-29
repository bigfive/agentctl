"""Command line surface.

    agentctl start <harness> <prompt> [--cwd DIR] [--model M]
                             [--from ADDR] [--no-notify] [--require-callback]
    agentctl send <harness:native-id> <prompt> [--from ADDR]
                             [--no-notify] [--require-callback] [--model M]
    agentctl sessions list   [--harness H] [--filter TEXT] [--limit N] [--json]
    agentctl sessions history <harness:native-id> [--limit N] [--json]
    agentctl sub <harness:native-id> [--notify ADDR] [--timeout SECS] [--json]
    agentctl sub read <sub-id> [--json]
    agentctl listeners [<harness:native-id>] [--json]
    agentctl setup (--inspect | --uninstall) [--harness H] [--dry-run] [--json]
    agentctl asyncwait <job> --notify <harness>:<session-id> [--timeout SECONDS]
    agentctl wait <job> [--rewake] [--timeout SECONDS]
    agentctl status <job>
    agentctl read <job> [--stream | --stderr]
    agentctl followup <job> <prompt>
    agentctl cancel <job>
    agentctl list
    agentctl daemon status|start|stop|run

``start`` prints the job handle on stdout and nothing else, so a shell can
capture it with ``a=$(agentctl start ...)``. ``send`` prints the subscription
id on stdout. Every other command prints a human-readable block, or JSON with
``--json``.
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
                    "notify the parent session when it finishes or times out.")
    parser.add_argument("--version", action="version",
                        version="agentctl %s" % __version__)
    sub = parser.add_subparsers(dest="command")

    start = sub.add_parser("start", help="start a child session")
    start.add_argument("harness", choices=sorted(config.HARNESSES))
    start.add_argument("prompt")
    start.add_argument("--cwd", default=None,
                       help="working directory for the child (default: here)")
    start.add_argument("--model", default=None)
    start.add_argument("--from", dest="from_addr", default=None,
                       metavar="HARNESS:SESSION-ID",
                       help="caller address for auto-subscription; inferred "
                            "from environment if omitted")
    start.add_argument("--no-notify", action="store_true",
                       help="do not auto-subscribe to completion")
    start.add_argument("--require-callback", action="store_true",
                       help="fail before dispatch if caller cannot receive a "
                            "callback")
    start.add_argument("--json", action="store_true")
    start.set_defaults(handler=cmd_start)

    send = sub.add_parser(
        "send",
        help="send a prompt to an existing session")
    send.add_argument("address", metavar="HARNESS:SESSION-ID",
                      help="target session, e.g. codex:01a07a1e-...")
    send.add_argument("prompt")
    send.add_argument("--from", dest="from_addr", default=None,
                      metavar="HARNESS:SESSION-ID",
                      help="caller address for auto-subscription")
    send.add_argument("--no-notify", action="store_true",
                      help="do not auto-subscribe to completion")
    send.add_argument("--require-callback", action="store_true",
                      help="fail before dispatch if caller cannot receive a "
                           "callback")
    send.add_argument("--model", default=None)
    send.add_argument("--json", action="store_true")
    send.set_defaults(handler=cmd_send)

    sessions_cmd = sub.add_parser(
        "sessions", help="discover and read sessions across harnesses")
    sessions_sub = sessions_cmd.add_subparsers(dest="sessions_action")

    sess_list = sessions_sub.add_parser(
        "list", help="list sessions across all harnesses")
    sess_list.add_argument("--harness", metavar="H",
                           help="filter to one harness")
    sess_list.add_argument("--filter", dest="filter_query", default=None,
                           metavar="TEXT",
                           help="filter sessions by address, title, or cwd")
    sess_list.add_argument("--limit", type=int, default=50,
                           help="max sessions to show (default 50)")
    sess_list.add_argument("--json", action="store_true")
    sessions_cmd.set_defaults(handler=cmd_sessions, json=False)

    sess_hist = sessions_sub.add_parser(
        "history", help="show a session's conversation history")
    sess_hist.add_argument("address", metavar="HARNESS:SESSION-ID")
    sess_hist.add_argument("--limit", type=int, default=20,
                           help="messages to show (default 20)")
    sess_hist.add_argument("--offset", type=int, default=0,
                           help="skip this many recent messages")
    sess_hist.add_argument("--raw", action="store_true",
                           help="show native records, including tools")
    sess_hist.add_argument("--json", action="store_true")

    sess_search = sessions_sub.add_parser("search", help="search saved conversation contents across harnesses")
    sess_search.add_argument("query")
    sess_search.add_argument("--mode", choices=("literal", "full-text"), default="literal")
    sess_search.add_argument("--harness", choices=config.HARNESSES)
    sess_search.add_argument("--limit", type=int, default=20)
    sess_search.add_argument("--include-tools", action="store_true", help="also index and search native tool outputs")
    sess_search.add_argument("--json", action="store_true")
    sess_status = sessions_sub.add_parser("search-status", help="show search index freshness and coverage")
    sess_status.add_argument("--json", action="store_true")
    sess_reindex = sessions_sub.add_parser("reindex", help="rebuild the search index from native sessions")
    sess_reindex.add_argument("--include-tools", action="store_true")
    sess_reindex.add_argument("--json", action="store_true")

    sub_cmd = sub.add_parser(
        "sub", help="manage completion subscriptions")
    sub_cmd_sub = sub_cmd.add_subparsers(dest="sub_action")

    sub_create = sub_cmd_sub.add_parser(
        "create",
        help="subscribe to completion of an external session turn")
    sub_create.add_argument("address", metavar="HARNESS:SESSION-ID",
                            help="target session to watch")
    sub_create.add_argument("--notify", dest="notify_addr",
                            metavar="HARNESS:SESSION-ID",
                            default=None,
                            help="where to deliver the callback; inferred "
                                 "from environment if omitted")
    sub_create.add_argument("--timeout", type=float, default=None)
    sub_create.add_argument("--json", action="store_true")

    sub_read = sub_cmd_sub.add_parser("read", help="read a subscription record")
    sub_read.add_argument("sub", metavar="SUB-ID")
    sub_read.add_argument("--json", action="store_true")

    sub_list = sub_cmd_sub.add_parser("list", help="list subscriptions")
    sub_list.add_argument("address", metavar="HARNESS:SESSION-ID", nargs="?",
                          help="filter to a specific target session")
    sub_list.add_argument("--json", action="store_true")

    sub_cmd.set_defaults(handler=cmd_sub, json=False)

    listeners = sub.add_parser(
        "listeners",
        help="inspect who is subscribed to a session's completion")
    listeners.add_argument("address", metavar="HARNESS:SESSION-ID", nargs="?",
                           help="session to inspect (default: self from env)")
    listeners.add_argument("--json", action="store_true")
    listeners.set_defaults(handler=cmd_listeners)

    setup_cmd = sub.add_parser(
        "setup",
        help="inspect or remove legacy agentctl hooks")
    setup_cmd.add_argument("--harness", metavar="H", default=None,
                           help="configure a specific harness only")
    setup_actions = setup_cmd.add_mutually_exclusive_group()
    setup_actions.add_argument("--inspect", action="store_true",
                           help="show installed setup without changing anything")
    setup_actions.add_argument("--uninstall", action="store_true",
                           help="remove agentctl-owned hook entries")
    setup_cmd.add_argument("--dry-run", action="store_true",
                           help="show what would be changed without writing")
    setup_cmd.add_argument("--json", action="store_true")
    setup_cmd.set_defaults(handler=cmd_setup)

    resume_cmd = sub.add_parser(
        "resume",
        help="resume an externally-created session with a new prompt")
    resume_cmd.add_argument("address", metavar="HARNESS:SESSION-ID",
                            help="target session, e.g. codex:01a07a1e-...")
    resume_cmd.add_argument("prompt")
    resume_cmd.add_argument("--cwd", default=None)
    resume_cmd.add_argument("--model", default=None)
    resume_cmd.add_argument("--from", dest="from_addr", default=None,
                            metavar="HARNESS:SESSION-ID")
    resume_cmd.add_argument("--no-notify", action="store_true")
    resume_cmd.add_argument("--require-callback", action="store_true")
    resume_cmd.add_argument("--json", action="store_true")
    resume_cmd.set_defaults(handler=cmd_resume)

    hook_cmd = sub.add_parser(
        "_hook", help=argparse.SUPPRESS)
    hook_cmd.add_argument("event", choices=["start", "begin", "complete", "receive"])
    hook_cmd.add_argument("--session", default=None)
    hook_cmd.add_argument("--harness", default=None)
    hook_cmd.set_defaults(handler=cmd_hook)

    asyncwait = sub.add_parser(
        "asyncwait",
        help="register a durable wait and return immediately")
    asyncwait.add_argument("job")
    asyncwait.add_argument("--notify", required=True,
                           metavar="HARNESS:SESSION-ID",
                           help="where to deliver the notification, "
                                "e.g. codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3")
    asyncwait.add_argument("--timeout", type=float, default=None,
                           help="seconds to wait before notifying anyway")
    asyncwait.add_argument("--json", action="store_true")
    asyncwait.set_defaults(handler=cmd_asyncwait)

    wait = sub.add_parser("wait", help="block until a job is terminal")
    wait.add_argument("job", nargs="+")
    wait.add_argument("--rewake", action="store_true",
                      help="write the envelope to stderr and exit 2 "
                           "(legacy foreground format)")
    wait.add_argument("--timeout", type=float, default=None,
                      help="seconds to wait before giving up")
    wait.add_argument("--json", action="store_true")
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
    from .sender import resolve_caller, preflight_callback

    if getattr(args, "no_notify", False) and getattr(args, "require_callback", False):
        raise UsageError("--require-callback and --no-notify are contradictory: "
                         "--no-notify skips subscription, --require-callback requires it")

    # Resolve caller for auto-subscription; preflight before any side effects.
    from_harness, from_id = (None, None)
    if not getattr(args, "no_notify", False):
        from_harness, from_id = resolve_caller(
            getattr(args, "from_addr", None))
        if from_harness:
            preflight_callback(from_harness, from_id,
                               require_callback=getattr(args,
                                                        "require_callback",
                                                        False))
        elif getattr(args, "require_callback", False):
            raise UsageError(
                "require-callback: caller identity is unknown",
                hint="set --from harness:session-id or supply the native "
                     "session identity environment variable")

    # Pass subscriber into start_job so the daemon creates the subscription
    # BEFORE launching the child, closing the fast-completion race.
    record = ipc.call(
        "start",
        harness=args.harness,
        prompt=args.prompt,
        cwd=os.path.abspath(args.cwd or os.getcwd()),
        model=args.model,
        subscriber_harness=from_harness if from_harness else None,
        subscriber_id=from_id if from_id else None,
        require_callback=args.require_callback,
    )

    sub_record = record.pop("_subscription", None)

    if args.json:
        out = dict(record)
        if sub_record:
            out["subscription"] = sub_record
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        # Bare handle, so a=$(agentctl start ...) captures exactly this.
        print(record["job"])
        if sub_record:
            sys.stderr.write("sub: %s\n" % sub_record["sub"])
    if record["status"] == store.FAILED:
        sys.stderr.write("agentctl: %s\n" % (record.get("error") or "failed"))
        return ExitCode.CHILD_FAILED
    return ExitCode.OK


def cmd_send(args):
    """Ask the daemon to dispatch and track a request to an existing session."""
    from .sender import resolve_caller
    if args.no_notify and args.require_callback:
        raise UsageError("--require-callback and --no-notify are contradictory")
    harness, native_id = _parse_address_arg(args.address)
    caller = (None, None) if args.no_notify else resolve_caller(args.from_addr)
    record = ipc.call("send_session", harness=harness, session_id=native_id,
                      prompt=args.prompt, model=args.model,
                      subscriber_harness=caller[0], subscriber_id=caller[1],
                      require_callback=args.require_callback)
    cb = record.get("callback", {})
    if record.get("from_address") and cb.get("state") in (
            "unsupported", "not_configured", "unreachable", "unverified", "busy"):
        description = "callback route unverified; delivery will be attempted" if cb["state"] == "unverified" else "callbacks not available"
        sys.stderr.write("agentctl: %s (%s): %s; result readable with 'agentctl sub read %s'\n"
                         % (description, cb["state"], cb.get("error") or "route unavailable", record["sub"]))
    if args.json:
        print(json.dumps(record, indent=2, sort_keys=True))
    else:
        print(record.get("sub") or record.get("job") or record.get("request"))
        if record.get("_subscription"):
            sys.stderr.write("sub: %s\n" % record["_subscription"]["sub"])
        if record.get("dispatch", {}).get("unknown"):
            sys.stderr.write("agentctl: acceptance unknown; no automatic resend\n")
    return ExitCode.DELIVERY_FAILED if record.get("dispatch", {}).get("rejected") else ExitCode.OK


def cmd_resume(args):
    """Resume an existing externally-created session with a new prompt.

    Validates the session exists via discovery before launching.
    Unknown sessions are never silently created as fresh ones.
    """
    from .sender import resolve_caller, preflight_callback
    from . import discover as _discover

    harness, native_id = _parse_address_arg(args.address)

    # Reject contradictory flags
    if getattr(args, "no_notify", False) and getattr(args, "require_callback", False):
        raise UsageError("--require-callback and --no-notify are contradictory")

    # Validate existence: unknown target must never create a fresh session
    all_sessions = _discover.discover(harnesses=[harness])
    existing = next((s for s in all_sessions
                     if s.get("native_id") == native_id), None)
    if existing is None:
        from .errors import UnknownHandleError
        raise UnknownHandleError(
            "no %s session with id %s found; cannot resume an unknown target"
            % (harness, native_id),
            hint="use 'agentctl sessions list --harness %s' to discover "
                 "available sessions, or use 'agentctl start %s' to create "
                 "a new one" % (harness, harness))

    if existing.get("capability", {}).get("resume") is False:
        from .errors import UnsupportedOperationError
        raise UnsupportedOperationError(existing["capability"].get("resume_reason") or "this session kind cannot be resumed")
    # Resolve caller for auto-subscription
    from_harness, from_id = (None, None)
    if not getattr(args, "no_notify", False):
        from_harness, from_id = resolve_caller(getattr(args, "from_addr", None))
        if from_harness:
            preflight_callback(from_harness, from_id,
                               require_callback=getattr(args, "require_callback", False))
        elif getattr(args, "require_callback", False):
            raise UsageError(
                "require-callback: caller identity is unknown",
                hint="set --from harness:session-id")

    record = ipc.call(
        "resume_external",
        harness=harness,
        session_id=native_id,
        prompt=args.prompt,
        cwd=os.path.abspath(args.cwd or existing.get("cwd") or os.getcwd()),
        model=getattr(args, "model", None),
        subscriber_harness=from_harness,
        subscriber_id=from_id,
        require_callback=args.require_callback,
    )

    sub_record = record.pop("_subscription", None)
    if args.json:
        out = dict(record)
        if sub_record:
            out["subscription"] = sub_record
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print(record["job"])
        if sub_record:
            sys.stderr.write("sub: %s\n" % sub_record["sub"])
    if record["status"] == store.FAILED:
        sys.stderr.write("agentctl: %s\n" % (record.get("error") or "failed"))
        return ExitCode.CHILD_FAILED
    return ExitCode.OK


def cmd_sessions(args):
    from . import discover as _discover

    action = getattr(args, "sessions_action", None) or "list"

    if action in ("search", "search-status", "reindex"):
        if action == "search":
            result = ipc.call("session_search", query=args.query, mode=args.mode,
                harness=args.harness, limit=args.limit, include_tools=args.include_tools)
        elif action == "reindex":
            result = ipc.call("session_reindex", include_tools=args.include_tools)
        else:
            result = ipc.call("search_status")
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            coverage = result.get("coverage", result)
            if action == "search":
                print("%s matching sessions" % result["total_sessions"])
                for hit in result["results"]:
                    print("\n%s  %s\n[%s; %s] %s" % (hit["address"], hit.get("title") or "", hit["role"], hit["state"], hit["excerpt"]))
                    if hit.get("read_command"):
                        print(hit["read_command"])
            elif action == "reindex":
                print("reindexed %s sessions" % result["changed_sessions"])
            print("\nIndex refreshed: %s; updating: %s; incomplete coverage: %s" % (
                _stamp(coverage.get("last_refresh")), coverage["refreshing"], coverage["incomplete"]))
            for source in coverage["sources"]:
                print("%s: %s%s" % (source["harness"], source["counts"],
                                      "; error: " + source["error"] if source["error"] else ""))
            if coverage.get("error"):
                print("index error: %s" % coverage["error"])
        return ExitCode.OK

    if action == "history":
        harness, native_id = _parse_address_arg(args.address)
        limit = getattr(args, "limit", 20)
        if limit < 0 or getattr(args, "offset", 0) < 0:
            raise UsageError("history limit and offset must be non-negative")
        if getattr(args, "raw", False):
            messages = _discover.read_raw_history(harness, native_id)
        else:
            messages = _discover.read_history(harness, native_id, limit=limit,
                                              offset=getattr(args, "offset", 0))
        if args.json or getattr(args, "raw", False):
            print(json.dumps(messages, indent=2))
        else:
            if not messages:
                print("no history found for %s:%s" % (harness, native_id))
                return ExitCode.OK
            for msg in messages:
                role = msg.get("role", "?")
                content = msg.get("content", "")
                print("[%s] %s" % (role, content))
        return ExitCode.OK

    # default: list
    harnesses = None
    if getattr(args, "harness", None):
        harnesses = [args.harness]
    sessions_list = _discover.discover(harnesses)
    query = getattr(args, "filter_query", None)
    if query:
        sessions_list = _discover.filter_sessions(sessions_list, query)
    limit = getattr(args, "limit", 50)
    if limit < 0:
        raise UsageError("session list limit must be non-negative")
    sessions_list = sessions_list[:limit]

    if args.json:
        print(json.dumps(sessions_list, indent=2, sort_keys=True))
    else:
        if not sessions_list:
            print("no sessions found")
            return ExitCode.OK
        for s in sessions_list:
            kind = s.get("kind", s.get("harness", "?"))
            address = s.get("address", "?")
            title = s.get("title") or ""
            cap = s.get("capability", {})
            send_ok = cap.get("send", True)
            flag = "" if send_ok else " [send-unsupported]"
            mtime = s.get("modified_at")
            ts = time.strftime("%Y-%m-%dT%H:%M", time.localtime(mtime)) \
                if mtime else "-"
            print("%-14s %-50s %s%s" % (kind, address, ts, flag))
            if title:
                print("  %s" % title[:76])
    return ExitCode.OK


def cmd_sub(args):
    from . import sessions as _sessions

    action = getattr(args, "sub_action", None)

    if action == "read":
        record = ipc.call("sub_read", sub=args.sub)
        if args.json:
            print(json.dumps(record, indent=2, sort_keys=True))
        else:
            _print_sub(record)
        return ExitCode.OK

    if action == "list" or action is None:
        target = getattr(args, "address", None)
        records = ipc.call("sub_list", target=target)
        if args.json:
            print(json.dumps(records, indent=2, sort_keys=True))
        else:
            if not records:
                print("no subscriptions")
                return ExitCode.OK
            for r in records:
                _print_sub_line(r)
        return ExitCode.OK

    if action == "create":
        from .sender import resolve_caller, preflight_callback
        harness, native_id = _parse_address_arg(args.address)
        notify_addr = getattr(args, "notify_addr", None)
        from_harness, from_id = resolve_caller(notify_addr)
        if not from_harness:
            sys.stderr.write("agentctl: note: caller identity unknown; "
                             "subscription created without callback target\n")
        record = ipc.call(
            "sub_create",
            target_harness=harness,
            target_id=native_id,
            from_harness=from_harness,
            from_id=from_id,
            timeout=getattr(args, "timeout", None),
        )
        if args.json:
            print(json.dumps(record, indent=2, sort_keys=True))
        else:
            print("sub: %s" % record["sub"])
            print("target: %s" % record["target_address"])
            if record.get("from_address"):
                print("callback: %s" % record["from_address"])
            else:
                print("callback: none (use 'agentctl sub read %s' to poll)"
                      % record["sub"])
        return ExitCode.OK

    raise UsageError("unknown sub action: %r" % action)


def cmd_listeners(args):
    from .sender import resolve_caller

    addr = getattr(args, "address", None)
    if not addr:
        h, sid = resolve_caller()
        if h and sid:
            addr = "%s:%s" % (h, sid)
        else:
            raise UsageError(
                "session address required when caller identity is not in "
                "the environment",
                hint="pass a HARNESS:SESSION-ID, or set AGENTCTL_FROM")

    records = ipc.call("listeners", target=addr)
    if args.json:
        print(json.dumps(records, indent=2, sort_keys=True))
    else:
        if not records:
            print("no listeners for %s" % addr)
            return ExitCode.OK
        print("listeners for %s:" % addr)
        for r in records:
            _print_sub_line(r)
    return ExitCode.OK


def cmd_setup(args):
    from . import setup as _setup
    selected = [args.harness] if args.harness else None
    if args.inspect:
        results = _setup.inspect_all(harnesses=selected)
    elif args.uninstall:
        results = _setup.uninstall_all(dry_run=args.dry_run, harnesses=selected)
    else:
        raise UsageError("native hook installation has been removed; transcript completion polling runs in the daemon",
                         hint="use 'agentctl setup --inspect' or 'agentctl setup --uninstall' for legacy hook cleanup")
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        for harness, result in sorted(results.items()):
            print("\n-- %s --" % harness)
            _print_dict(result)
    return ExitCode.OK if all(r.get("ok", False) for r in results.values()) else ExitCode.USAGE


def cmd_hook(args):
    raise UsageError("agentctl native hooks have been retired; the daemon polls transcript completion markers")


def cmd_asyncwait(args):
    wait = ipc.call("asyncwait", job=args.job, notify=args.notify,
                    timeout=args.timeout)
    if args.json:
        print(json.dumps(wait, indent=2, sort_keys=True))
        return ExitCode.OK
    print("wait: %s" % wait["wait"])
    print("job: %s" % args.job)
    print("notify: %s:%s" % (wait["notify_harness"], wait["notify_id"]))
    if wait.get("timeout") is not None:
        print("timeout: %s seconds" % wait["timeout"])
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

    if args.json:
        print(json.dumps({
            "jobs": [dict(record, result=store.read_result(record["job"]))
                     for record in records],
        }, indent=2, sort_keys=True))
        if any(record["status"] == store.CANCELLED for record in records):
            return ExitCode.CHILD_CANCELLED
        if any(record["status"] == store.FAILED for record in records):
            return ExitCode.CHILD_FAILED
        return ExitCode.OK

    if args.rewake:
        # Preserve the legacy foreground stderr/exit-code contract.
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
        if delivery.get("event"):
            line += " event=%s" % delivery["event"]
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


def _parse_address_arg(addr):
    """Parse harness:session-id from a CLI argument."""
    if not addr or ":" not in addr:
        raise UsageError(
            "session address must be 'harness:session-id': %r" % addr,
            hint="for example codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3")
    harness, _, session_id = addr.partition(":")
    harness = harness.strip().lower()
    session_id = session_id.strip()
    if not session_id:
        raise UsageError("session address %r has no session id" % addr)
    return harness, session_id


def _print_sub(record):
    print("sub: %s" % record.get("sub"))
    print("target: %s" % record.get("target_address"))
    if record.get("from_address"):
        print("from: %s" % record["from_address"])
    else:
        print("from: unknown")
    if record.get("job"):
        print("job: %s" % record["job"])
    print("status: %s" % record.get("status"))
    cb = record.get("callback", {})
    print("callback: %s" % cb.get("state", "?"))
    if cb.get("error"):
        print("  callback error: %s" % cb["error"])
    print("created: %s" % _stamp(record.get("created_at")))
    if record.get("target_turn_id"):
        print("turn: %s" % record["target_turn_id"])
    if record.get("result_stored_at") is not None:
        print("result: %s" % (record.get("result") or "(native turn ended without assistant text)"))
    else:
        print("result: (pending)")
    if record.get("completion_status"):
        print("completion: %s" % record["completion_status"])
    if record.get("dispatch_error"):
        print("dispatch error: %s" % record["dispatch_error"])


def _print_sub_line(record):
    sub_id = record.get("sub", "?")
    target = record.get("target_address", "?")
    from_addr = record.get("from_address") or "unknown"
    status = record.get("status", "?")
    cb_state = (record.get("callback") or {}).get("state", "?")
    print("%-18s %-40s %-14s %-10s cb=%s" % (
        sub_id, target, from_addr, status, cb_state))


def _print_dict(d, indent=0):
    prefix = "  " * indent
    for k, v in sorted(d.items()):
        if isinstance(v, dict):
            print("%s%s:" % (prefix, k))
            _print_dict(v, indent + 1)
        else:
            print("%s%s: %s" % (prefix, k, v))
