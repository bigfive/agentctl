"""Cursor notification adapter.

Mechanism: ``cursor-agent -p --resume <chat-id>`` with the completion envelope
on stdin. Resuming appends a new turn to that stored chat.

Two things are deliberately explicit here, because conflating them is the main
Cursor trap:

- A local chat id is a bare UUID, as printed by ``cursor-agent create-chat``.
- A cloud agent id starts with ``bc-``. Cloud agents are not supported by this
  adapter, and passing one is an error rather than a silent local attempt.

Cursor keeps two separate transcripts for a chat id: the CLI's own store under
``~/.cursor/chats``, and the IDE's store. Resuming an IDE chat id from the CLI
does **not** join the conversation the user is looking at. It writes a fresh
CLI-side transcript carrying only the new turn, and the IDE never shows it.

That was measured: a completion delivered to a live Cursor IDE chat reported
success, a Cursor agent picked it up and answered it, and none of it appeared
on screen. Delivering into an IDE chat is therefore refused here rather than
reported as delivered, since a silent success is the worst outcome available.

An IDE chat is identified by its presence in Cursor's IDE agent store. CLI
chats, including every chat made by ``cursor-agent create-chat``, never appear
there.

Cursor also has no durable message queue like Codex's. Measured on cursor-cli
2026.07.23: resuming a CLI chat that already has a turn in flight is
*accepted*, not rejected, and both turns complete and land in that chat. So
the failure mode is not a refusal; it is that two clients write to one chat
concurrently and the turn order is whatever the two runs produce.

The busy check below is kept for the case where Cursor does report a busy
chat, which is documented for its cloud agents. It has not been observed on
the local path. Delivery is never retried either way, because a retry could
duplicate a follow-up that in fact arrived.
"""

import os
import re
import subprocess

from .. import config
from ..errors import DeliveryError, InvalidTargetError, UnsupportedOperationError
from .base import NotifyAdapter

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

BUSY_MARKERS = ("agent_busy", "already running", "in progress", "busy")

# Cursor's IDE agent store. A chat id present here belongs to a window the user
# has open, and cannot be reached from the CLI. The path is macOS-specific; on
# a platform where it does not exist the check simply finds nothing.
IDE_STORE = os.path.expanduser(
    "~/Library/Application Support/Cursor/AgentStores/cursor_agent_stores")


def is_ide_chat(chat_id):
    """True when this chat belongs to the Cursor IDE rather than the CLI."""
    return os.path.isdir(os.path.join(IDE_STORE, chat_id))


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
        if is_ide_chat(target_id):
            raise UnsupportedOperationError(
                "%s is a Cursor IDE chat, which cannot receive a delivery: "
                "resuming it from the CLI writes a separate transcript that "
                "the IDE never displays" % target_id,
                hint="block on 'agentctl wait <job>' instead, which returns "
                     "the result to you directly")

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
