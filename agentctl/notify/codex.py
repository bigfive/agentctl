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
import subprocess

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

    def deliver(self, target_id, message, notification_id):
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
