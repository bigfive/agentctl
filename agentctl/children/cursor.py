"""Cursor child adapter.

Interface: ``cursor-agent -p --output-format stream-json`` against a chat id
allocated up front by ``cursor-agent create-chat``.

Verified against Cursor Agent 2026.07.23: ``create-chat`` prints a bare chat
UUID on stdout. Allocating the id first means the child handle is stable and
follow-ups resume exactly that chat.

Cursor has no durable message queue. That matters for notification, not for
launching children; see ``notify/cursor.py``.
"""

import subprocess

from .. import config
from ..errors import PrerequisiteMissingError
from .base import ChildAdapter

# --force runs every tool without asking. --trust accepts the workspace so a
# non-interactive run is not stopped by a trust prompt.
PERMISSIVE = ["--force", "--trust"]


class CursorChild(ChildAdapter):
    harness = "cursor"

    def preallocate_session(self, cwd, model):
        binary = config.resolve_binary("cursor")
        process = subprocess.Popen(
            [binary, "create-chat"], cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = process.communicate(timeout=120)
        chat_id = (out or b"").decode("utf-8", "replace").strip().splitlines()
        if process.returncode != 0 or not chat_id:
            raise PrerequisiteMissingError(
                "cursor-agent create-chat failed (exit %s): %s"
                % (process.returncode,
                   (err or b"").decode("utf-8", "replace").strip()))
        return chat_id[-1].strip()

    def _base(self, session_id, cwd, model):
        argv = [config.resolve_binary("cursor"), "-p",
                "--output-format", "stream-json", "--resume", session_id]
        argv += PERMISSIVE
        if model:
            argv += ["--model", model]
        return argv

    def start_command(self, session_id, cwd, model, result_file):
        return self._base(session_id, cwd, model)

    def followup_command(self, session_id, cwd, model, result_file):
        return self._base(session_id, cwd, model)

    def read_event(self, event):
        kind = event.get("type")
        if kind == "system" and event.get("session_id"):
            return {"native_session_id": event["session_id"]}
        if kind == "result":
            outcome = {}
            if isinstance(event.get("result"), str):
                outcome["result"] = event["result"]
            if event.get("is_error"):
                outcome["failed"] = True
                outcome["error"] = event.get("result") or "cursor run failed"
            return outcome
        return {}
