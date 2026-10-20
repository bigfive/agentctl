"""Stable identifier generation.

Three kinds of identifier exist and are never interchangeable:

- job handle    ``job_<12 hex>``  an agentctl job
- wait handle   ``wait_<12 hex>`` one registered asyncwait
- notification  ``ntf_<24 hex>``  one delivery, derived deterministically so a
                                  repeated terminal event cannot create a
                                  second follow-up
"""

import hashlib
import os


def _token(nbytes):
    return os.urandom(nbytes).hex()


def new_job_id():
    return "job_" + _token(6)


def new_wait_id():
    return "wait_" + _token(6)


def notification_id(wait_id, job_id, status):
    """Derive the delivery identifier for one terminal outcome.

    The same wait reaching the same terminal status always produces the same
    notification id, so a daemon restart or a duplicated terminal event is
    recognised as the delivery that already happened.
    """
    digest = hashlib.sha256(
        ("%s|%s|%s" % (wait_id, job_id, status)).encode("utf-8")
    ).hexdigest()
    return "ntf_" + digest[:24]


def opencode_message_id(notification_id_value):
    """OpenCode v2 message ids must match ``^msg_``.

    Reusing the notification id makes the OpenCode delivery idempotent on the
    server side as well as in the agentctl store.
    """
    return "msg_" + hashlib.sha256(
        notification_id_value.encode("utf-8")
    ).hexdigest()[:24]
