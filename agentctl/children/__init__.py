"""Child adapter registry."""

from ..errors import UsageError
from .claude import ClaudeChild
from .codex import CodexChild
from .cursor import CursorChild
from .opencode import OpenCodeChild

ADAPTERS = {
    "codex": CodexChild(),
    "claude": ClaudeChild(),
    "cursor": CursorChild(),
    "opencode": OpenCodeChild(),
}


def get(harness):
    try:
        return ADAPTERS[harness]
    except KeyError:
        raise UsageError(
            "unknown harness: %s" % harness,
            hint="supported harnesses: %s" % ", ".join(sorted(ADAPTERS)))
