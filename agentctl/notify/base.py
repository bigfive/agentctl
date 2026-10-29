"""Shared notification-adapter contract.

A notification adapter delivers one envelope into one live parent
session. It validates the target id before attempting delivery, and it raises
``DeliveryError`` on anything short of confirmed acceptance. It never retries:
a retry could duplicate a follow-up in the parent's context.
"""


class NotifyAdapter(object):
    harness = None

    def validate(self, target_id):
        """Raise ``InvalidTargetError`` if ``target_id`` cannot be a target."""
        raise NotImplementedError

    def preflight(self, target_id):
        """Check now that this target could actually receive a delivery.

        Called when a wait is registered, not when it fires. A target that
        cannot be reached must fail here, while the calling agent is still
        listening, rather than an hour later inside the daemon where only the
        job record would show it.

        Default: nothing to check.
        """
        return None

    def deliver(self, target_id, message, notification_id):
        """Deliver ``message``, or raise ``DeliveryError``.

        ``notification_id`` is stable for one wait reaching one event, so an
        adapter can pass it to a harness that supports
        caller-supplied message ids.
        """
        raise NotImplementedError
