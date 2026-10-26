"""Codex notification adapter.

Mechanism: Desktop IPC steering for active UUID targets, CLI queue for idle targets.

The owning Desktop window selects the active turn. An inactive turn uses queue
delivery. Experimental IPC failures emit a warning and fall back to queue;
ambiguous failures can duplicate a message. Exact names use queue-only delivery.

The thread id is a Codex thread id. It is not the transcript filename, and it
is not an agentctl job handle.
"""

import logging
import re
import subprocess

from .codex_ipc import DesktopIPC

from .. import config
from ..errors import DeliveryError, InvalidTargetError
from .base import NotifyAdapter

logger = logging.getLogger(__name__)

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

    def deliver(self, target_id, message, notification_id):
        warning = None
        if UUID_RE.fullmatch(target_id):
            # EXPERIMENTAL: this is the versioned, internal Codex Desktop
            # follower IPC protocol, not the public app-server transport.
            # Desktop selects/rechecks the active turn and calls turn/steer.
            # The operator explicitly chose queue fallback on ALL errors,
            # including ambiguous acceptance: rare duplicate delivery is
            # possible if Desktop accepted the steer but its reply was lost.
            try:
                with DesktopIPC() as ipc:
                    result = ipc.steer(target_id, message, notification_id)
                if result is not None:
                    return result
            except Exception as exc:
                warning = "Experimental Codex steering failed (%s): %s; falling back to codex queue" % (type(exc).__name__, exc)
                logger.warning("%s", warning)
        result = self._queue(target_id, message)
        if warning is not None:
            # Retain diagnostics in the durable notification delivery result,
            # even when the caller/daemon does not display stderr.
            result["steer_warning"] = warning
        return result

    def _queue(self, target_id, message):
        argv = [config.resolve_binary("codex"), "queue",
                "--thread", target_id, "--message", message]
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        out, err = process.communicate(timeout=120)
        stdout = (out or b"").decode("utf-8", "replace").strip()
        stderr = (err or b"").decode("utf-8", "replace").strip()
        if process.returncode != 0:
            raise DeliveryError(
                "codex queue failed (exit %s): %s"
                % (process.returncode, stderr or stdout),
                hint="the thread must be visible to the local Codex "
                     "app-server daemon")
        return {"transport": "codex queue", "response": stdout}
