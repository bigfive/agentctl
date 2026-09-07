"""Shared notification-adapter contract.

A notification adapter delivers one completion envelope into one live parent
session. It validates the target id before attempting delivery, and it raises
``DeliveryError`` on anything short of confirmed acceptance. It never retries:
a retry could duplicate a follow-up in the parent's context.
"""


class NotifyAdapter(object):
    harness = None

    def validate(self, target_id):
        """Raise ``InvalidTargetError`` if ``target_id`` cannot be a target."""
        raise NotImplementedError

    def deliver(self, target_id, message, notification_id):
        """Deliver ``message``, or raise ``DeliveryError``.

        ``notification_id`` is stable for one wait reaching one terminal
        status, so an adapter can pass it to a harness that supports
        caller-supplied message ids.
        """
        raise NotImplementedError
