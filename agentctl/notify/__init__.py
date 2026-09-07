"""Notification target parsing and adapter registry."""

from ..errors import InvalidTargetError
from .claude import ClaudeNotify
from .codex import CodexNotify
from .cursor import CursorNotify
from .opencode import OpenCodeNotify

ADAPTERS = {
    "codex": CodexNotify(),
    "opencode": OpenCodeNotify(),
    "cursor": CursorNotify(),
    "claude": ClaudeNotify(),
}


def parse_target(raw):
    """Split ``<harness>:<session-id>`` and validate it.

    Returns ``(harness, session_id)``. Anything unparseable, any unknown
    harness, and any id the adapter rejects fails here, before a job is ever
    watched, so a bad target is reported at registration time instead of at
    delivery time.
    """
    if not raw or ":" not in raw:
        raise InvalidTargetError(
            "notification target must be '<harness>:<session-id>': %r" % raw,
            hint="for example codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3")
    harness, _, target_id = raw.partition(":")
    harness = harness.strip().lower()
    target_id = target_id.strip()
    if harness not in ADAPTERS:
        raise InvalidTargetError(
            "unknown notification harness: %r" % harness,
            hint="supported: %s" % ", ".join(sorted(ADAPTERS)))
    if not target_id:
        raise InvalidTargetError("notification target %r has no session id" % raw)
    ADAPTERS[harness].validate(target_id)
    return harness, target_id


def get(harness):
    try:
        return ADAPTERS[harness]
    except KeyError:
        raise InvalidTargetError("unknown notification harness: %r" % harness)
