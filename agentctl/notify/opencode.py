"""OpenCode notification adapter.

Mechanism: ``POST /session/<session-id>/prompt_async`` on an already running
OpenCode server. Verified against opencode 1.18.20, which answers 204 on
acceptance.

The request carries a caller-supplied ``messageID`` derived from the
notification id, so the same completion cannot be appended twice even if the
delivery is somehow attempted again.
"""

import re

from .. import config, httpc, ids
from ..errors import DeliveryError, InvalidTargetError
from .base import NotifyAdapter

SESSION_RE = re.compile(r"^ses_?[A-Za-z0-9]+$")


class OpenCodeNotify(NotifyAdapter):
    harness = "opencode"

    def validate(self, target_id):
        if SESSION_RE.match(target_id):
            return
        raise InvalidTargetError(
            "opencode notification target is not a session id: %r" % target_id,
            hint="OpenCode session ids look like ses_f85ae7073ffe3i7aOYmTzCVnG1")

    def preflight(self, target_id):
        """The server must be reachable now, and must own this session.

        An OpenCode TUI started without an explicit port serves nothing that
        agentctl can reach, and exports no server URL, so this is the common
        way for an OpenCode notification to be impossible.
        """
        base = config.opencode_base_url()
        try:
            status, body = httpc.request("GET", "%s/session/%s"
                                         % (base, target_id), timeout=10)
        except httpc.HttpError as exc:
            if exc.status == 0:
                raise InvalidTargetError(
                    "no OpenCode server is reachable at %s, so session %s "
                    "cannot be notified" % (base, target_id),
                    hint="run 'opencode serve' and point "
                         "AGENTCTL_OPENCODE_URL at it, or block on "
                         "'agentctl wait <job>' instead")
            if exc.status == 404:
                raise InvalidTargetError(
                    "the OpenCode server at %s does not have session %s"
                    % (base, target_id),
                    hint="confirm the session id, or point "
                         "AGENTCTL_OPENCODE_URL at the server that owns it")
            raise InvalidTargetError(
                "cannot confirm OpenCode session %s: %s" % (target_id, exc))
        if status != 200:
            raise InvalidTargetError(
                "the OpenCode server at %s answered HTTP %s for session %s"
                % (base, status, target_id))
        return None

    def deliver(self, target_id, message, notification_id):
        base = config.opencode_base_url()
        url = "%s/session/%s/prompt_async" % (base, target_id)
        payload = {
            "messageID": ids.opencode_message_id(notification_id),
            "parts": [{"type": "text", "text": message}],
        }
        try:
            status, body = httpc.request("POST", url, payload)
        except httpc.HttpError as exc:
            raise DeliveryError(
                "opencode prompt_async failed: %s" % exc,
                hint="the OpenCode server at %s must be running and must own "
                     "session %s" % (base, target_id))
        if status not in (200, 202, 204):
            raise DeliveryError(
                "opencode prompt_async returned HTTP %s: %s" % (status, body))
        return {"transport": "opencode prompt_async", "response": status}
