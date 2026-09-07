"""Claude child adapter.

Interface: ``claude -p --output-format stream-json``. Print mode only.
Interactive and background-agent modes are deliberately unsupported, because
they persist and expose sessions differently.

Verified against Claude Code 2.1.153. ``--session-id`` lets agentctl choose the
session id before the child starts, so the child handle is known immediately
and never has to be recovered from the stream.
"""

import uuid

from .. import config
from .base import ChildAdapter

PERMISSIVE = ["--dangerously-skip-permissions"]


class ClaudeChild(ChildAdapter):
    harness = "claude"

    def preallocate_session(self, cwd, model):
        return str(uuid.uuid4())

    def _base(self, cwd, model):
        argv = [config.resolve_binary("claude"), "-p",
                "--output-format", "stream-json", "--verbose"]
        argv += PERMISSIVE
        if model:
            argv += ["--model", model]
        return argv

    def start_command(self, session_id, cwd, model, result_file):
        return self._base(cwd, model) + ["--session-id", session_id]

    def followup_command(self, session_id, cwd, model, result_file):
        return self._base(cwd, model) + ["--resume", session_id]

    def read_event(self, event):
        kind = event.get("type")
        if kind == "system" and event.get("session_id"):
            return {"native_session_id": event["session_id"]}
        if kind == "result":
            outcome = {}
            if event.get("session_id"):
                outcome["native_session_id"] = event["session_id"]
            if isinstance(event.get("result"), str):
                outcome["result"] = event["result"]
            if event.get("is_error") or event.get("subtype") not in (
                    None, "success"):
                outcome["failed"] = True
                outcome["error"] = (
                    event.get("result")
                    or "claude reported %s" % event.get("subtype"))
            return outcome
        return {}
