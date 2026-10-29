"""Client/daemon transport.

One newline-delimited JSON request, one newline-delimited JSON response, over a
Unix socket at ``$AGENTCTL_HOME/daemon.sock``.

The CLI starts the daemon if it is not running. That is process lifecycle, not
a transport fallback: there is exactly one way for the CLI to reach the daemon,
and if the socket cannot be reached the command fails.
"""

import errno
import json
import os
import socket
import subprocess
import sys
import threading
import time

from . import config, store
from .errors import AgentctlError, DaemonUnavailableError, UsageError

CONNECT_TIMEOUT = 5
CALL_TIMEOUT = 300
START_TIMEOUT = 20
PROTOCOL_VERSION = 4
PROTOCOL_OPS = {"notify", "interrupt", "start", "send_session", "resume_external", "sub_create",
                "sub_read", "sub_list", "listeners", "hook_event", "inbox_list", "inbox_claim"}


# ---- server ------------------------------------------------------------

def serve(daemon):
    """Run the accept loop until the process is signalled."""
    config.ensure_home()
    path = config.socket_path()
    if os.path.exists(path):
        if _ping(path):
            raise DaemonUnavailableError(
                "another agentctl daemon is already listening on %s" % path)
        os.unlink(path)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    os.chmod(path, 0o600)
    server.listen(32)

    with open(config.pid_path(), "w") as handle:
        handle.write("%d\n" % os.getpid())

    daemon.recover()
    daemon.start_transcript_polling()
    # Indexing starts on first search, then resumes automatically on later boots.
    if os.path.isfile(os.path.join(config.home(), "search.sqlite3")):
        try:
            daemon.start_search_index()
        except Exception as exc:
            from .daemon import log
            log("search index startup failed: %s" % exc)

    try:
        while True:
            try:
                conn, _ = server.accept()
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                raise
            threading.Thread(target=_handle, args=(daemon, conn),
                             daemon=True).start()
    finally:
        server.close()
        for cleanup in (path, config.pid_path()):
            try:
                os.unlink(cleanup)
            except OSError:
                pass


def _handle(daemon, conn):
    try:
        conn.settimeout(CALL_TIMEOUT)
        payload = _read_line(conn)
        if payload is None:
            return
        try:
            request = json.loads(payload)
        except ValueError:
            _send(conn, {"ok": False, "error": "malformed request"})
            return
        _send(conn, dispatch(daemon, request))
    except Exception as exc:
        try:
            _send(conn, {"ok": False, "error": "daemon error: %s" % exc})
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def dispatch(daemon, request):
    op = request.get("op")
    try:
        if op in ("hook_event", "hook_complete"):
            from .errors import UnsupportedOperationError
            raise UnsupportedOperationError("native lifecycle hooks have been replaced by transcript completion polling")
        if op == "session_search":
            return {"ok": True, "data": daemon.search_sessions(
                query=request["query"], mode=request.get("mode", "literal"),
                harness=request.get("harness"), limit=request.get("limit", 20),
                include_tools=request.get("include_tools", False))}
        if op == "search_status":
            return {"ok": True, "data": daemon.search_status()}
        if op == "session_reindex":
            return {"ok": True, "data": daemon.reindex_sessions(
                include_tools=request.get("include_tools", False))}
        if op == "ping":
            return {"ok": True, "data": {"pid": os.getpid(), "protocol": PROTOCOL_VERSION, "worker_options": True}}
        if op == "start":
            record = daemon.start_job(
                request["harness"], request.get("prompt") or "",
                request.get("cwd"), request.get("model"),
                subscriber_harness=request.get("subscriber_harness"),
                subscriber_id=request.get("subscriber_id"),
                require_callback=request.get("require_callback", False),
                request_id=request.get("request_id"), options=request.get("options"),
            )
            return {"ok": True, "data": record}
        if op == "followup":
            record = store.read(request["job"])
            if not store.is_terminal(record):
                raise UsageError("cannot follow up an active job")
            native = record.get("native_session_id")
            if not native:
                from .errors import UnsupportedOperationError
                raise UnsupportedOperationError("job has no native session id")
            child = daemon.start_job(
                record["harness"], request["prompt"], record["cwd"],
                record.get("model"), followup_of=record["job"],
                session_id=native, request_id=request.get("request_id"),
                options=request["options"] if "options" in request else record.get("options"))
            return {"ok": True, "data": child}
        if op == "interrupt":
            return {"ok": True, "data": daemon.interrupt_job(
                request["job"], request["prompt"], request.get("request_id"))}
        if op == "notify":
            delivery = daemon.deliver_session(request["target"], request["message"],
                                               request.get("request_id"))
            return {"ok": True, "data": delivery}
        if op == "asyncwait":
            wait = daemon.register_wait(request["job"], request["notify"],
                                        request.get("timeout"))
            return {"ok": True, "data": wait}
        if op == "status":
            return {"ok": True, "data": store.read(request["job"])}
        if op == "list":
            return {"ok": True, "data": store.list_jobs()}
        if op == "cancel":
            return {"ok": True, "data": daemon.cancel_job(request["job"])}
        if op == "sub_create":
            record = daemon.register_session_sub(
                target_harness=request["target_harness"],
                target_id=request["target_id"],
                from_harness=request.get("from_harness"),
                from_id=request.get("from_id"),
                job=request.get("job"),
                timeout=request.get("timeout"),
                is_callback=request.get("is_callback", False),
                request_id=request.get("request_id"),
                target_turn_id=request.get("target_turn_id"),
                observe_current=request.get("observe_current", True),
            )
            return {"ok": True, "data": record}
        if op == "sub_read":
            from . import sessions as _sessions
            record = _sessions.read(request["sub"])
            return {"ok": True, "data": record}
        if op == "sub_list":
            from . import sessions as _sessions
            target = request.get("target")
            if target:
                records = _sessions.list_for_target(target)
            else:
                records = _sessions.list_all()
            return {"ok": True, "data": records}
        if op == "listeners":
            from . import sessions as _sessions
            target = request["target"]
            records = [r for r in _sessions.list_for_target(target)
                       if r.get("status") == _sessions.PENDING]
            for job in store.list_jobs():
                native = job.get("native_session_id")
                if not native or "%s:%s" % (job["harness"], native) != target:
                    continue
                for wait in job.get("waits", []):
                    if wait.get("delivery", {}).get("state") != store.DELIVERY_PENDING:
                        continue
                    records.append({"sub": wait["wait"], "legacy_wait": True,
                                    "target_address": target, "job": job["job"],
                                    "target_turn_id": job.get("native_turn_id"),
                                    "from_address": "%s:%s" % (wait["notify_harness"], wait["notify_id"]),
                                    "status": "pending", "callback": wait.get("delivery", {})})
            return {"ok": True, "data": records}

        if op == "hook_complete":
            count = daemon.notify_hook_complete(
                request["target"],
                turn_id=request.get("turn_id"),
            )
            return {"ok": True, "data": {"triggered": count,
                                         "target": request["target"]}}
        if op == "send_session":
            record = daemon.send_session(
                request["harness"], request["session_id"], request.get("prompt") or "",
                model=request.get("model"),
                subscriber_harness=request.get("subscriber_harness"),
                subscriber_id=request.get("subscriber_id"),
                require_callback=request.get("require_callback", False))
            return {"ok": True, "data": record}

        if op == "resume_external":
            record = daemon.resume_external(
                request["harness"],
                request["session_id"],
                request.get("prompt") or "",
                request.get("cwd"),
                request.get("model"),
                subscriber_harness=request.get("subscriber_harness"),
                subscriber_id=request.get("subscriber_id"),
                require_callback=request.get("require_callback", False),
            )
            return {"ok": True, "data": record}

        if op == "hook_event":
            # General hook event: start, begin, complete
            count = daemon.notify_hook_event(
                request["target"],
                event=request.get("event", "complete"),
                turn_id=request.get("turn_id"),
                transcript_path=request.get("transcript_path"),
                result=request.get("result"), request_id=request.get("request_id"),
                prompt=request.get("prompt"),
                stop_hook_active=request.get("stop_hook_active", False),
                status=request.get("status"),
            )
            return {"ok": True, "data": {"triggered": count,
                                         "target": request["target"],
                                         "event": request.get("event", "complete")}}

        if op == "inbox_list":
            from . import sessions as _sessions
            # Accept either session_id or address (harness:native-id)
            inbox_id = request.get("session_id") or request.get("address", "")
            messages = _sessions.inbox_list(inbox_id)
            return {"ok": True, "data": messages}

        if op == "inbox_claim":
            from . import sessions as _sessions
            # Accept either session_id or address (harness:native-id)
            inbox_id = request.get("session_id") or request.get("address", "")
            _sessions.registry_update(inbox_id, receiver_until=time.time() + 2)
            messages = _sessions.inbox_claim_all(inbox_id)
            # setup.py expects {message: str|null} format for single message
            # but we return a list; provide both formats
            message = "\n\n".join(r["message"] for r in messages) if messages else None
            return {"ok": True, "data": {"message": message, "messages": messages}}

        if op == "shutdown":
            threading.Timer(0.1, os._exit, args=(0,)).start()
            return {"ok": True, "data": {"stopping": True}}
        return {"ok": False, "error": "unknown op: %r" % op,
                "exit_code": 64}
    except AgentctlError as exc:
        return {"ok": False, "error": exc.message, "hint": exc.hint,
                "exit_code": exc.exit_code}
    except KeyError as exc:
        return {"ok": False, "error": "missing field: %s" % exc,
                "exit_code": 64}


def _read_line(conn):
    chunks = []
    while True:
        try:
            data = conn.recv(65536)
        except socket.timeout:
            return None
        if not data:
            break
        chunks.append(data)
        if b"\n" in data:
            break
    if not chunks:
        return None
    return b"".join(chunks).split(b"\n", 1)[0].decode("utf-8", "replace")


def _send(conn, payload):
    conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))


# ---- client ------------------------------------------------------------

def _connect(path, timeout=CONNECT_TIMEOUT):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    client.connect(path)
    return client


def _ping(path):
    try:
        client = _connect(path, timeout=2)
    except OSError:
        return False
    try:
        _send(client, {"op": "ping"})
        line = _read_line(client)
        return bool(line) and json.loads(line).get("ok") is True
    except Exception:
        return False
    finally:
        client.close()


def is_running():
    return _ping(config.socket_path())


def daemon_pid():
    try:
        with open(config.pid_path()) as handle:
            return int(handle.read().strip())
    except (IOError, ValueError):
        return None


def ensure_daemon():
    """Start the daemon if the socket is not answering."""
    if is_running():
        return False
    config.ensure_home()
    # Import the same package this process is running from, whatever the
    # caller's working directory is.
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [package_root] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    log = open(config.daemon_log_path(), "a")
    try:
        subprocess.Popen(
            [sys.executable, "-m", "agentctl", "daemon", "run"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, cwd=package_root, env=env)
    finally:
        log.close()
    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if is_running():
            return True
        time.sleep(0.2)
    raise DaemonUnavailableError(
        "the agentctl daemon did not start within %ss" % START_TIMEOUT,
        hint="see %s" % config.daemon_log_path())


def call(op, **fields):
    """Send one request, raising the daemon's error as a local error."""
    ensure_daemon()
    if op in PROTOCOL_OPS:
        version = _request({"op": "ping"}).get("data", {}).get("protocol", 1)
        if version != PROTOCOL_VERSION:
            raise DaemonUnavailableError(
                "running daemon protocol %s differs from CLI protocol %s; request was not dispatched"
                % (version, PROTOCOL_VERSION),
                hint="after active jobs finish, run 'agentctl daemon stop' and 'agentctl daemon start'")
    request = dict(fields)
    request["op"] = op
    response = _request(request)
    if response.get("ok"):
        return response.get("data")
    _raise(response)


def _request(request):
    try:
        client = _connect(config.socket_path())
    except OSError as exc:
        raise DaemonUnavailableError(
            "cannot reach the agentctl daemon at %s: %s"
            % (config.socket_path(), exc))
    try:
        client.settimeout(CALL_TIMEOUT)
        _send(client, request)
        line = _read_line(client)
    finally:
        client.close()
    if not line:
        raise DaemonUnavailableError(
            "the agentctl daemon closed the connection without answering %r"
            % request.get("op"))
    return json.loads(line)


def _raise(response):
    error = AgentctlError(response.get("error") or "daemon reported failure",
                          hint=response.get("hint"))
    error.exit_code = response.get("exit_code") or 1
    raise error
