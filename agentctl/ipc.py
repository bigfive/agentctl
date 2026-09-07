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
from .errors import AgentctlError, DaemonUnavailableError

CONNECT_TIMEOUT = 5
CALL_TIMEOUT = 300
START_TIMEOUT = 20


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
        if op == "ping":
            return {"ok": True, "data": {"pid": os.getpid()}}
        if op == "start":
            record = daemon.start_job(
                request["harness"], request.get("prompt") or "",
                request.get("cwd"), request.get("model"))
            return {"ok": True, "data": record}
        if op == "followup":
            record = store.read(request["job"])
            native = record.get("native_session_id")
            if not native:
                from .errors import UnsupportedOperationError
                raise UnsupportedOperationError(
                    "job %s has no native session id, so it cannot take a "
                    "follow-up" % record["job"])
            child = daemon.start_job(
                record["harness"], request.get("prompt") or "",
                record["cwd"], record.get("model"),
                followup_of=record["job"], session_id=native)
            return {"ok": True, "data": child}
        if op == "asyncwait":
            wait = daemon.register_wait(request["job"], request["notify"])
            return {"ok": True, "data": wait}
        if op == "status":
            return {"ok": True, "data": store.read(request["job"])}
        if op == "list":
            return {"ok": True, "data": store.list_jobs()}
        if op == "cancel":
            return {"ok": True, "data": daemon.cancel_job(request["job"])}
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
    request = dict(fields)
    request["op"] = op
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
            % op)
    response = json.loads(line)
    if response.get("ok"):
        return response.get("data")
    _raise(response)


def _raise(response):
    error = AgentctlError(response.get("error") or "daemon reported failure",
                          hint=response.get("hint"))
    error.exit_code = response.get("exit_code") or 1
    raise error
