"""Claude notification target.

Claude has no external command that injects a message into a live session, so
there is nothing for a push adapter to call. Starting ``claude -p --resume``
would open a separate headless client against the stored session; it would not
wake the session the parent agent is sitting in.

Claude's own wake mechanism is used instead. The parent runs the blocking wait
and lets Claude wake it:

- background it and let the harness deliver the finished task, or
- register it as an ``asyncRewake`` hook, where the command runs in the
  background and exit code 2 wakes Claude with the command's stderr.

``agentctl wait <job> --rewake`` implements exactly that contract: it blocks
until the job is terminal, writes the completion envelope to stderr, and exits
2.

Passing ``--notify claude:<id>`` therefore fails loudly rather than pretending
to have a push transport.
"""

from ..errors import UnsupportedOperationError
from .base import NotifyAdapter


class ClaudeNotify(NotifyAdapter):
    harness = "claude"

    def validate(self, target_id):
        raise UnsupportedOperationError(
            "claude has no external message-injection command, so "
            "'--notify claude:<id>' is not supported",
            hint="from Claude, background 'agentctl wait <job>' or register "
                 "'agentctl wait <job> --rewake' as an asyncRewake hook")

    def deliver(self, target_id, message, notification_id):
        self.validate(target_id)
