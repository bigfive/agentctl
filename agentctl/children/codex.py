"""Codex child adapter.

Interface: ``codex exec --json``. One path, no fallback.

Verified against codex-cli 0.153.4. The event stream begins with
``{"type": "thread.started", "thread_id": ...}`` and ends with
``turn.completed`` or ``turn.failed``. ``-o/--output-last-message`` makes Codex
itself write the final assistant message, so agentctl does not reconstruct it
from events.
"""

from .. import config
from .base import ChildAdapter

# Fully permissive. Any approval request would otherwise block a child that
# nobody is watching.
PERMISSIVE = ["--dangerously-bypass-approvals-and-sandbox"]


class CodexChild(ChildAdapter):
    harness = "codex"

    def _base(self, cwd, model, result_file):
        argv = [config.resolve_binary("codex"), "exec", "--json",
                "--skip-git-repo-check", "--color", "never",
                "-o", result_file, "-C", cwd]
        argv += PERMISSIVE
        if model:
            argv += ["-m", model]
        return argv

    def start_command(self, session_id, cwd, model, result_file):
        return self._base(cwd, model, result_file)

    def followup_command(self, session_id, cwd, model, result_file):
        argv = [config.resolve_binary("codex"), "exec", "resume", session_id, "-",
                "--json", "--skip-git-repo-check",
                "-o", result_file]
        argv += PERMISSIVE
        if model:
            argv += ["-m", model]
        return argv

    def option_args(self, options):
        import json
        from ..errors import UsageError
        if not isinstance(options, dict):
            raise UsageError('worker options must be a JSON object')
        allowed = {
            'reasoning_effort': ('model_reasoning_effort',
                                 {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}),
            'service_tier': ('service_tier', {'auto', 'default', 'flex', 'priority'}),
        }
        args = []
        for key, value in sorted(options.items()):
            if key not in allowed:
                raise UsageError('unsupported Codex worker option: ' + key)
            native, choices = allowed[key]
            if not isinstance(value, str) or value not in choices:
                raise UsageError('invalid ' + key + '; expected one of: ' + ', '.join(sorted(choices)))
            args += ['-c', native + '=' + json.dumps(value)]
        return args

    def read_event(self, event):
        kind = event.get("type")
        if kind == "thread.started" and event.get("thread_id"):
            return {"native_session_id": event["thread_id"]}
        if kind == "turn.failed":
            error = event.get("error") or {}
            return {"failed": True,
                    "error": error.get("message") or "codex turn failed"}
        if kind == "error" and event.get("message"):
            return {"failed": True, "error": event["message"]}
        return {}
