"""Claude notification target.

Claude has no external command that injects a message into a live session, so
there is nothing for a push adapter to call. Starting ``claude -p --resume``
would open a separate headless client against the stored session; it would not
wake the session the parent agent is sitting in.

For sessions registered via agentctl setup hooks, delivery goes to a durable
per-session inbox file keyed by the canonical 'claude:<session-id>' address.
The Claude asyncRewake hook ('agentctl _hook receive') drains the inbox on the
next Stop event, reads all claimed messages to stderr, and exits 2 to wake
the session.

For asyncwait --notify claude:X, still use agentctl wait instead. preflight()
raises UnsupportedOperationError to prevent asyncwait from using this path.

Claude's own wake mechanism is used instead for asyncwait:
- background it and let the harness deliver the finished task, or
- register it as an ``asyncRewake`` hook, where the command runs in the
  background and exit code 2 wakes Claude with the command's stderr.

``agentctl wait <job> --rewake`` implements exactly that contract: it blocks
until the job is terminal, writes the completion envelope to stderr, and exits
2.

Passing ``--notify claude:<id>`` via asyncwait therefore fails loudly rather
than pretending to have a push transport.
"""

from ..errors import InvalidTargetError, UnsupportedOperationError
from .base import NotifyAdapter


class ClaudeNotify(NotifyAdapter):
    harness = "claude"

    def validate(self, target_id):
        """Validate that target_id is a non-empty string.

        Does NOT raise for non-empty ids. Raises InvalidTargetError only for
        empty string.
        """
        if not target_id:
            raise InvalidTargetError(
                "claude target id must be non-empty")

    def preflight(self, target_id):
        """Preflight always raises for asyncwait --notify claude:X.

        Use agentctl wait instead. For subscription callback delivery, use
        deliver() which writes to the canonical inbox.
        """
        raise UnsupportedOperationError(
            "claude has no external message-injection command, so "
            "'--notify claude:<id>' is not supported",
            hint="from Claude, background 'agentctl wait <job>' or register "
                 "'agentctl wait <job> --rewake' as an asyncRewake hook")

    def deliver(self, target_id, message, notification_id):
        """Deliver message to the session's inbox for the asyncRewake hook to drain.

        Uses the canonical 'claude:<target_id>' address as the inbox key so
        that inbox_claim (which receives the full canonical address from
        setup.py) finds the same file.
        """
        self.validate(target_id)
        from .. import sessions
        # Use canonical address for inbox key so deliver and claim match.
        # setup.py calls inbox_claim with address="claude:<id>" so we must
        # write to the same key.
        canonical = "claude:%s" % target_id
        return sessions.inbox_put(canonical, message, notification_id)
