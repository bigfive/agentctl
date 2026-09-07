"""Cursor notification adapter.

Mechanism: ``cursor-agent -p --resume <chat-id>`` with the completion envelope
on stdin. Resuming appends a new turn to that stored chat.

Two things are deliberately explicit here, because conflating them is the main
Cursor trap:

- A local chat id is a bare UUID, as printed by ``cursor-agent create-chat``.
- A cloud agent id starts with ``bc-``. Cloud agents are not supported by this
  adapter, and passing one is an error rather than a silent local attempt.

Cursor has no durable message queue. If the target chat already has a turn in
flight, delivery fails and says so; agentctl does not queue behind it and does
not retry, because a retry could duplicate the follow-up.
"""

import re
import subprocess

from .. import config
from ..errors import DeliveryError, InvalidTargetError, UnsupportedOperationError
from .base import NotifyAdapter

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

BUSY_MARKERS = ("agent_busy", "already running", "in progress", "busy")


class CursorNotify(NotifyAdapter):
    harness = "cursor"

    def validate(self, target_id):
        if target_id.startswith("bc-"):
            raise UnsupportedOperationError(
                "%s is a Cursor cloud agent id; this adapter delivers to local "
                "chats only" % target_id,
                hint="pass the local chat UUID from ~/.cursor/chats/<workspace>/")
        if not UUID_RE.match(target_id):
            raise InvalidTargetError(
                "cursor notification target is not a local chat UUID: %r"
                % target_id,
                hint="a local chat id looks like "
                     "5a4f300a-61b1-4975-82e2-aa4be275aa49")

    def deliver(self, target_id, message, notification_id):
        argv = [config.resolve_binary("cursor"), "-p",
                "--output-format", "text", "--resume", target_id,
                "--force", "--trust"]
        process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        out, err = process.communicate(message.encode("utf-8"), timeout=300)
        stdout = (out or b"").decode("utf-8", "replace").strip()
        stderr = (err or b"").decode("utf-8", "replace").strip()
        if process.returncode != 0:
            combined = ("%s %s" % (stdout, stderr)).lower()
            if any(marker in combined for marker in BUSY_MARKERS):
                raise DeliveryError(
                    "cursor chat %s is busy; Cursor has no durable message "
                    "queue, so this completion was not delivered" % target_id,
                    hint="read the result with 'agentctl read <job>' instead")
            raise DeliveryError(
                "cursor-agent resume failed (exit %s): %s"
                % (process.returncode, stderr or stdout))
        return {"transport": "cursor-agent --resume", "response": stdout[:400]}
