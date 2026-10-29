"""Codex notification adapter.

Mechanism: ``codex queue --thread <id> --message <text>``.

This was verified end to end against a live Codex Desktop task: the queued
message is accepted while a turn is active and delivered as a normal follow-up
turn once the thread becomes idle. It is Codex's durable queue, so a parent
that is mid-turn does not need to be interrupted or polled.

The thread id is a Codex thread id. It is not the transcript filename, and it
is not an agentctl job handle.
"""

import re
import json
import os
import selectors
import tempfile
import time
import subprocess
import socket

from .. import config
from ..errors import DeliveryError, InvalidTargetError
from .base import NotifyAdapter

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# codex queue accepts a session UUID or an exact session name.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._\-]{0,127}$")


class CodexNotify(NotifyAdapter):
    harness = "codex"

    def validate(self, target_id):
        if UUID_RE.match(target_id) or NAME_RE.match(target_id):
            return
        raise InvalidTargetError(
            "codex notification target is not a thread UUID or session name: "
            "%r" % target_id,
            hint="use the thread UUID shown for the current Codex task")

    def preflight(self, target_id):
        """Read the target through the existing app-server; never queue a probe."""
        self.validate(target_id)
        argv = [config.resolve_binary("codex"), "app-server", "proxy"]
        control = os.environ.get("AGENTCTL_CODEX_CONTROL_SOCKET")
        if control:
            argv += ["--sock", control]
        process = None
        try:
            with tempfile.TemporaryFile() as errors:
                process = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=errors)
                pending = b""
                def rpc(request):
                    nonlocal pending
                    process.stdin.write((json.dumps(request) + "\n").encode())
                    process.stdin.flush()
                    deadline = time.monotonic() + 5
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        while time.monotonic() < deadline:
                            while b"\n" in pending:
                                line, pending = pending.split(b"\n", 1)
                                response = json.loads(line)
                                if response.get("id") == request["id"]:
                                    if response.get("error"):
                                        raise InvalidTargetError("Codex thread probe rejected: %s" % response["error"])
                                    return response.get("result")
                            if not selector.select(max(0, deadline - time.monotonic())):
                                break
                            chunk = os.read(process.stdout.fileno(), 65536)
                            if not chunk:
                                errors.seek(0)
                                detail = errors.read().decode("utf-8", "replace").strip()
                                raise ValueError(detail or "app-server control route closed")
                            pending += chunk
                    raise ValueError("app-server read-only probe timed out")
                rpc({"id": 1, "method": "initialize", "params": {
                    "clientInfo": {"name": "agentctl", "version": "0.2"}}})
                process.stdin.write(b'{"method":"initialized"}\n'); process.stdin.flush()
                result = rpc({"id": 2, "method": "thread/read", "params": {
                    "threadId": target_id, "includeTurns": False}})
                if not isinstance(result, dict) or not result.get("thread"):
                    raise ValueError("app-server did not confirm the target thread")
                result["verified"] = True
                return result
        except (OSError, ValueError) as exc:
            # Desktop's queue IPC is independent of app-server proxy. Probe
            # the queue route without submitting a message or starting a turn.
            try:
                return self._preflight_queue(target_id)
            except (OSError, ValueError, subprocess.TimeoutExpired) as queue_error:
                return {"verified": False, "state": "unverified",
                        "note": "cannot verify Codex callback route for %s: "
                                "proxy: %s; queue: %s" % (target_id, exc, queue_error)}
        finally:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait()
                for stream in (process.stdin, process.stdout):
                    if stream:
                        stream.close()

    def _preflight_queue(self, target_id):
        """Check the local Desktop queue listener and stored target, read-only.

        This confirms current prerequisites, not future delivery. Only the
        eventual queue command can acknowledge acceptance of a completion.
        """
        from .. import discover
        targets = discover._codex_sessions()
        target = next((row for row in targets
                       if row.get("native_id") == target_id or
                       row.get("title") == target_id), None)
        if target is None:
            raise ValueError("target is not present in the local Codex store")
        help_result = subprocess.run(
            [config.resolve_binary("codex"), "queue", "--help"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        if help_result.returncode or b"--thread" not in help_result.stdout:
            raise ValueError("Codex CLI does not support queue --thread")
        codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
        path = os.path.join(codex_home, "ipc", "ipc.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(path)
        return {"verified": True, "transport": "codex queue",
                "thread": {"id": target["native_id"]}}

    def deliver(self, target_id, message, notification_id):
        argv = [config.resolve_binary("codex"), "queue",
                "--thread", target_id, "--message", message]
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        try:
            out, err = process.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            process.kill(); process.communicate()
            raise
        stdout = (out or b"").decode("utf-8", "replace").strip()
        stderr = (err or b"").decode("utf-8", "replace").strip()
        if process.returncode != 0:
            raise DeliveryError(
                "codex queue failed (exit %s): %s"
                % (process.returncode, stderr or stdout),
                hint="the thread must be visible to the local Codex "
                     "app-server daemon")
        return {"transport": "codex queue", "response": stdout}
