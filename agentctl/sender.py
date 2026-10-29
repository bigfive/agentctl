"""Dispatch a request to a target session.

Implements the PRD dispatch and callback matrices:

Request dispatch:
  Supported and reachable           -> dispatch and return receipt
  Busy with queue support           -> queue and track
  Busy without queue support        -> reject target busy
  Supported but not configured      -> reject with setup instructions
  Configured but unreachable        -> reject with connection error
  Sending unsupported               -> reject unsupported
  Acceptance uncertain after crash  -> delivery unknown; no auto resend

Completion callback:
  Supported, configured, reachable  -> auto-subscribe and deliver
  Busy with callback queue          -> queue until callable
  Callbacks unsupported             -> proceed, report, provide wait handle
  Callbacks not configured          -> proceed, report, provide wait handle
  Callback route unreachable        -> proceed, report, provide wait handle
  Callback delivery fails           -> retain result; mark failed
  Callback acceptance uncertain     -> mark unknown; no auto resend

Caller identity is resolved from:
  1. Explicit --from harness:session-id flag
  2. AGENTCTL_FROM environment variable
  3. Native harness env vars (CODEX_THREAD_ID, CLAUDE_SESSION_ID, etc.)
  4. Unknown caller -- reported clearly, subscription skipped or rejected

Callbacks never create return subscriptions.  is_callback=True in delivered
subscription records prevents automatic re-subscription.
"""

import json
import os
import re
import subprocess
import time

from . import config, ids, sessions
from .errors import (AgentctlError, DeliveryError, InvalidTargetError,
                     UnsupportedOperationError, UsageError)


# ---------------------------------------------------------------------------
# Capability tables
# ---------------------------------------------------------------------------

# Per harness: can we *send* a message to an externally running session?
# (This is different from starting an agentctl job.)
SEND_CAPABILITIES = {
    "codex": {
        "supported": True,
        "queue": True,
        "description": "codex queue --thread",
    },
    "claude": {
        "supported": False,
        "queue": False,
        "description": "no external push; use agentctl wait instead",
    },
    "cursor-cli": {
        "supported": True,
        "queue": False,
        "description": "cursor-agent -p --resume (no durable queue)",
    },
    "cursor-ide": {
        "supported": False,
        "queue": False,
        "description": ("CLI resume writes a separate transcript the IDE "
                        "never displays"),
    },
    "opencode": {
        "supported": True,
        "queue": False,
        "description": "POST /api/session/<id>/prompt (requires server)",
    },
}

# Per harness: can a *caller* in this harness receive a callback?
CALLBACK_CAPABILITIES = {
    "codex": {
        "supported": True,
        "mechanism": "codex queue",
        "requires_setup": False,
    },
    "claude": {
        "supported": False,
        "mechanism": None,
        "requires_setup": False,
        "note": "external callback delivery into Claude is unsupported without a session-owned receiver; read the stored result or use agentctl wait",
    },
    "cursor-cli": {
        "supported": True,
        "mechanism": "cursor-agent -p --resume",
        "requires_setup": False,
    },
    "cursor-ide": {
        "supported": False,
        "mechanism": None,
        "requires_setup": False,
        "note": "external delivery into Cursor IDE is unsupported; read the stored result or use agentctl wait",
    },
    "opencode": {
        "supported": True,
        "mechanism": "POST /api/session/<id>/prompt",
        "requires_setup": False,
        "requires_server": True,
    },
}

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


# ---------------------------------------------------------------------------
# Caller identity
# ---------------------------------------------------------------------------

def resolve_caller(explicit_from=None):
    """Determine the caller's harness:session-id from context.

    Returns (harness, session_id) or (None, None) if unknown.
    Never infers identity from the newest transcript.

    Priority:
    1. Explicit --from flag
    2. AGENTCTL_FROM env var (set by agentctl setup hooks — most reliable)
    3. Native harness session vars for non-Codex harnesses first, so an agent
       running inside Claude or Cursor that inherited CODEX_THREAD_ID from a
       Codex parent is not misidentified as Codex.
    4. CODEX_THREAD_ID last: only used when no other harness identity is set,
       meaning the caller is actually running inside Codex.
    """
    if explicit_from:
        return _parse_address(explicit_from)

    # AGENTCTL_FROM is set by agentctl setup hooks
    env_from = os.environ.get("AGENTCTL_FROM")
    if env_from:
        return _parse_address(env_from)

    # Check non-Codex harness session vars BEFORE CODEX_THREAD_ID so that a
    # Claude/Cursor/OpenCode agent that inherited CODEX_THREAD_ID from a Codex
    # parent is not incorrectly identified as the Codex parent.
    claude_id = os.environ.get("CLAUDE_SESSION_ID")
    if claude_id:
        return "claude", claude_id

    cursor_id = os.environ.get("CURSOR_SESSION_ID")
    if cursor_id:
        return "cursor", cursor_id

    opencode_id = os.environ.get("OPENCODE_SESSION_ID")
    if opencode_id:
        return "opencode", opencode_id

    # CODEX_THREAD_ID: only trust it if no other harness ID is present.
    # If CLAUDE_SESSION_ID etc. were set we would have returned above.
    thread_id = os.environ.get("CODEX_THREAD_ID")
    if thread_id:
        return "codex", thread_id

    return None, None


def _parse_address(addr):
    """Parse harness:session-id and return (harness, session_id)."""
    if not addr or ":" not in addr:
        raise UsageError(
            "session address must be 'harness:session-id': %r" % addr,
            hint="for example codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3")
    harness, _, session_id = addr.partition(":")
    harness = harness.strip().lower()
    session_id = session_id.strip()
    if not session_id:
        raise UsageError("session address %r has no session id" % addr)
    return harness, session_id


def caller_kind(harness, session_id=None):
    """Determine the specific client kind for a caller."""
    if harness == "cursor":
        # Distinguish IDE vs CLI
        from .discover import is_ide_chat
        if session_id and is_ide_chat(session_id):
            return "cursor-ide"
        return "cursor-cli"
    return harness


def callback_capability(harness, session_id=None):
    """Return the callback capability dict for a caller."""
    kind = caller_kind(harness, session_id)
    cap = dict(CALLBACK_CAPABILITIES.get(kind) or
               CALLBACK_CAPABILITIES.get(harness) or
               {"supported": False, "mechanism": None,
                "note": "unknown harness %r" % harness})
    return cap


def preflight_callback(harness, session_id, require_callback=False):
    """Read-only route check. Missing setup, unsupported and unverified differ."""
    from . import notify, setup, store
    kind = caller_kind(harness, session_id)
    cap = callback_capability(harness, session_id)
    def unavailable(state, note):
        if require_callback:
            error_type = UnsupportedOperationError if state == "unsupported" else (
                UsageError if state == "not_configured" else InvalidTargetError)
            raise error_type("require-callback: %s: %s" % (state, note))
        return {"ok": False, "state": state, "note": note}
    if not cap.get("supported"):
        return unavailable("unsupported", cap.get("note") or "unknown callback mechanism")
    if kind in ("cursor-cli", "opencode"):
        registry = sessions.registry_read("%s:%s" % (harness, session_id)) or {}
        running = any(r.get("harness") == harness and r.get("native_session_id") == session_id
                      and r.get("status") == store.RUNNING for r in store.list_jobs())
        if running or registry.get("state") == "running":
            return unavailable("busy", "recipient is busy and its callback transport has no durable queue")
    try:
        adapter = notify.get(harness)
        adapter.validate(session_id)
        probe = adapter.preflight(session_id)
        if isinstance(probe, dict) and probe.get("verified") is False:
            return unavailable(probe.get("state", "unverified"), probe.get("note", "route could not be verified"))
    except AgentctlError as exc:
        return unavailable("unsupported" if isinstance(exc, UnsupportedOperationError) else "unreachable", exc.message)
    return {"ok": True, "state": "configured"}


# ---------------------------------------------------------------------------
# Target capability
# ---------------------------------------------------------------------------

def target_kind(harness, native_id):
    """Determine the specific kind for a target."""
    if harness == "cursor":
        from .discover import is_ide_chat
        if is_ide_chat(native_id):
            return "cursor-ide"
        return "cursor-cli"
    return harness


def send_capability(harness, native_id):
    """Return send capability for a target."""
    kind = target_kind(harness, native_id)
    return dict(SEND_CAPABILITIES.get(kind) or
                SEND_CAPABILITIES.get(harness) or
                {"supported": False, "queue": False,
                 "description": "unknown harness"})


# ---------------------------------------------------------------------------
# Dispatch to external sessions
# ---------------------------------------------------------------------------

class DispatchReceipt:
    def __init__(self, accepted, unknown=False, error=None, busy=False,
                 details=None):
        self.accepted = accepted      # True: dispatch accepted
        self.unknown = unknown        # True: acceptance uncertain
        self.error = error            # str or None
        self.busy = busy              # True: target was busy
        self.details = details or {}


def dispatch_to_session(harness, native_id, prompt, model=None):
    """Send a prompt to an existing externally-running session.

    Returns a DispatchReceipt. Never silently creates a fresh session when
    addressing an existing one.

    This is the low-level dispatch; create the subscription record before
    calling this.
    """
    kind = target_kind(harness, native_id)
    cap = send_capability(harness, native_id)

    if not cap.get("supported"):
        raise UnsupportedOperationError(
            "sending to %s is not supported: %s" % (kind, cap["description"]),
            hint=("use 'agentctl start %s \"%s\"' to start a new session "
                  "instead" % (harness, prompt[:40])))

    if kind == "codex":
        return _dispatch_codex(native_id, prompt)
    if kind == "cursor-cli":
        return _dispatch_cursor_cli(native_id, prompt, model)
    if kind == "opencode":
        return _dispatch_opencode(native_id, prompt)

    raise UnsupportedOperationError(
        "dispatch not implemented for %s" % kind)


def _dispatch_codex(thread_id, prompt):
    """codex queue --thread <id> --message <text>"""
    argv = [config.resolve_binary("codex"), "queue",
            "--thread", thread_id, "--message", prompt]
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = process.communicate(timeout=60)
        stdout = (out or b"").decode("utf-8", "replace").strip()
        stderr = (err or b"").decode("utf-8", "replace").strip()
    except OSError as exc:
        raise DeliveryError("codex queue failed: %s" % exc)
    except subprocess.TimeoutExpired:
        # The subprocess timed out; the child process may still be running
        # and may or may not have queued the message. Report as unknown to
        # avoid false exactly-once claims; do not automatically resend.
        try:
            process.kill()
        except OSError:
            pass
        process.communicate()
        return DispatchReceipt(
            accepted=False, unknown=True,
            error="codex queue timed out; acceptance is uncertain")

    if process.returncode != 0:
        raise DeliveryError(
            "codex queue failed (exit %s): %s"
            % (process.returncode, stderr or stdout),
            hint="the thread must be visible to the local Codex app-server")
    return DispatchReceipt(accepted=True,
                           details={"transport": "codex queue",
                                    "response": stdout})


def _dispatch_cursor_cli(chat_id, prompt, model=None):
    """cursor-agent -p --output-format text --resume <id> with prompt on stdin"""
    from .notify.cursor import BUSY_MARKERS
    argv = [config.resolve_binary("cursor"), "-p",
            "--output-format", "text", "--resume", chat_id,
            "--force", "--trust"]
    if model:
        argv += ["--model", model]
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = process.communicate(prompt.encode("utf-8"), timeout=300)
        stdout = (out or b"").decode("utf-8", "replace").strip()
        stderr = (err or b"").decode("utf-8", "replace").strip()
    except OSError as exc:
        raise DeliveryError("cursor-agent resume failed: %s" % exc)
    except subprocess.TimeoutExpired:
        process.kill(); process.communicate()
        return DispatchReceipt(accepted=False, unknown=True,
                               error="cursor-agent timed out")

    if process.returncode != 0:
        combined = ("%s %s" % (stdout, stderr)).lower()
        if any(m in combined for m in BUSY_MARKERS):
            raise DeliveryError(
                "cursor chat %s is busy; Cursor has no durable message queue"
                % chat_id,
                hint="use --no-notify to send without waiting for the current "
                     "turn, or use 'agentctl wait' to block locally")
        raise DeliveryError(
            "cursor-agent resume failed (exit %s): %s"
            % (process.returncode, stderr or stdout))
    return DispatchReceipt(accepted=True,
                           details={"transport": "cursor-agent resume",
                                    "response": stdout[:400]})


def _dispatch_opencode(session_id, prompt, request_nonce=None):
    """POST /api/session/<id>/prompt"""
    from . import httpc
    base = config.opencode_base_url()
    url = "%s/api/session/%s/prompt" % (base, session_id)
    # Each request gets a unique nonce so two identical prompts produce different IDs.
    # The request_nonce parameter allows callers to provide a stable nonce for
    # idempotent retries.
    nonce = request_nonce or os.urandom(8).hex()
    msg_id = ids.opencode_message_id(nonce)
    payload = {
        "id": msg_id,
        "text": prompt,
        "delivery": "queue",
    }
    try:
        status, body = httpc.request("POST", url, payload, timeout=30,
                                     auth_header=config.opencode_auth_header())
    except Exception as exc:
        from . import httpc as _httpc
        if isinstance(exc, _httpc.HttpError) and exc.status == 0:
            return DispatchReceipt(accepted=False, unknown=True, error=str(exc),
                                   details={"message_id": msg_id})
        if isinstance(exc, _httpc.HttpError) and exc.status == 404:
            raise InvalidTargetError(
                "the OpenCode server at %s does not have session %s"
                % (base, session_id))
        raise DeliveryError("opencode v2 session prompt failed: %s" % exc)

    if status not in (200, 202, 204):
        raise DeliveryError(
            "opencode v2 session prompt returned HTTP %s: %s" % (status, body))
    return DispatchReceipt(accepted=True,
                           details={"transport": "opencode v2 session prompt",
                                    "message_id": msg_id,
                                    "response": status})
