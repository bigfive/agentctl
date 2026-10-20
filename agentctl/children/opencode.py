"""OpenCode child adapter.

A running OpenCode server is a prerequisite. agentctl never starts one.

The session is created on that server first (``POST /api/session``), then
``opencode run --server <url> --session <id>`` drives the turn as a watchable
subprocess. The v2 REST API and CLI were verified against opencode 2.0.32.
"""

from .. import config, httpc
from ..errors import PrerequisiteMissingError
from .base import ChildAdapter


class OpenCodeChild(ChildAdapter):
    harness = "opencode"

    def preallocate_session(self, cwd, model):
        base = config.opencode_base_url()
        url = "%s/api/session" % base
        try:
            status, body = httpc.request(
                "POST", url, {"location": {"directory": cwd}},
                auth_header=config.opencode_auth_header())
        except httpc.HttpError as exc:
            raise PrerequisiteMissingError(
                "cannot create an OpenCode session on %s: %s" % (base, exc),
                hint="start the server with 'opencode serve' or point "
                     "AGENTCTL_OPENCODE_URL at a running one")
        data = (body or {}).get("data", body) if isinstance(body, dict) else None
        session_id = data.get("id") if isinstance(data, dict) else None
        if status != 200 or not session_id:
            raise PrerequisiteMissingError(
                "OpenCode did not return a session id (HTTP %s)" % status)
        return session_id

    def _base(self, session_id, cwd, model):
        argv = [config.resolve_binary("opencode"), "run",
                "--server", config.opencode_base_url(),
                "--session", session_id,
                "--format", "json",
                # Approve every permission that is not explicitly denied. An
                # unanswered permission prompt would hang an unwatched child.
                "--auto"]
        if model:
            argv += ["-m", model]
        return argv

    def start_command(self, session_id, cwd, model, result_file):
        return self._base(session_id, cwd, model)

    def followup_command(self, session_id, cwd, model, result_file):
        return self._base(session_id, cwd, model)

    def read_event(self, event):
        # OpenCode emits raw message/part events. The assistant's text lands in
        # text parts; the last one is the run's result.
        if event.get("type") == "text" and isinstance(event.get("text"), str):
            return {"result": event["text"]}
        part = event.get("part")
        if isinstance(part, dict) and part.get("type") == "text":
            if isinstance(part.get("text"), str):
                return {"result": part["text"]}
        properties = event.get("properties")
        if isinstance(properties, dict):
            return self.read_event(properties)
        return {}
