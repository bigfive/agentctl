"""Codex Desktop IPC: length-prefixed JSON requests to the owning window.

This is the Desktop follower protocol, not app-server JSON-RPC. Its method
versions are explicit; incompatible or ambiguous responses fail delivery.
"""
import json
import os
import re
import socket
import struct
import time
import uuid

from ..errors import DeliveryError

MAX_FRAME = 16 * 1024 * 1024


def _read(sock, size):
    data = bytearray()
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise DeliveryError("Codex IPC closed before confirming delivery")
        data.extend(part)
    return bytes(data)


class DesktopIPC:
    def __init__(self):
        override = os.environ.get("AGENTCTL_CODEX_IPC_SOCKET")
        if override is not None:
            if not override:
                raise DeliveryError("AGENTCTL_CODEX_IPC_SOCKET is empty")
            self.path = override
        else:
            home = os.environ.get("CODEX_HOME")
            if home is not None and not home:
                raise DeliveryError("CODEX_HOME is empty")
            self.path = os.path.join(home if home is not None else os.path.expanduser("~/.codex"), "ipc", "ipc.sock")
        self.sock = None

    def __enter__(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(35)
        try:
            self.sock.connect(self.path)
            result = self.call("initialize", {"clientType": "cli"}, 0)
            if result.get("resultType") != "success":
                raise DeliveryError("Codex IPC initialization rejected: %s" % result)
            return self
        except Exception:
            self.sock.close()
            raise

    def __exit__(self, *_):
        self.sock.close()

    def call(self, method, params, version, owner=None):
        request_id = str(uuid.uuid4())
        request = {"type": "request", "requestId": request_id,
                   "method": method, "version": version, "params": params}
        if owner is not None:
            request["targetClientId"] = owner
        encoded = json.dumps(request).encode("utf-8")
        if len(encoded) > MAX_FRAME:
            raise DeliveryError("Codex IPC request exceeds frame limit")
        self.sock.sendall(struct.pack("<I", len(encoded)) + encoded)
        # Broadcasts can arrive while awaiting this request. Bound their count.
        deadline = time.monotonic() + 35
        for _ in range(1024):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeliveryError("Codex IPC response deadline exceeded")
            self.sock.settimeout(remaining)
            size = struct.unpack("<I", _read(self.sock, 4))[0]
            if not 0 < size <= MAX_FRAME:
                raise DeliveryError("Codex IPC returned an invalid frame length")
            response = json.loads(_read(self.sock, size))
            if not isinstance(response, dict):
                raise DeliveryError("Codex IPC returned a non-object response")
            if response.get("type") == "client-discovery-request":
                reply = {"type": "client-discovery-response", "requestId": response["requestId"], "response": {"canHandle": False}}
                raw = json.dumps(reply).encode()
                self.sock.sendall(struct.pack("<I", len(raw)) + raw)
            elif response.get("requestId") == request_id:
                if response.get("type") != "response" or response.get("resultType") not in ("success", "error"):
                    raise DeliveryError("Codex IPC returned an invalid response envelope")
                return response
        raise DeliveryError("Codex IPC response limit exceeded")

    def steer(self, thread, message, notification_id):
        owner = self.call("thread-owner-discovery", {"hostId": "local", "conversationId": thread}, 1)
        if owner.get("resultType") != "success" or not owner.get("handledByClientId"):
            raise DeliveryError("Codex Desktop could not locate the session owner: %s" % owner.get("error", owner))
        # The owning window reads the active turn and retries a turn-id race.
        # A stable user-message ID correlates the submission; it is not a claim
        # that the Desktop protocol guarantees deduplication after ambiguity.
        message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "agentctl:%s:%s" % (thread, notification_id)))
        response = self.call("thread-follower-steer-turn", {
            "conversationId": thread, "clientUserMessageId": message_id,
            "input": [{"type": "text", "text": message, "text_elements": []}],
            "restoreMessage": {"context": {}},
        }, 1, owner["handledByClientId"])
        if response["resultType"] == "success":
            result = response.get("result")
            turn = result.get("result") if isinstance(result, dict) else None
            if not isinstance(turn, dict) or not isinstance(turn.get("turnId"), str) or not turn["turnId"]:
                raise DeliveryError("Codex steer response did not confirm a turn ID; delivery is uncertain")
            return {"transport": "codex Desktop IPC steer", "turn_id": turn["turnId"]}
        error = response.get("error")
        if isinstance(error, str) and re.fullmatch(r"Cannot steer conversation " + re.escape(thread) + r" because its active turn already ended", error):
            return None  # Explicit rejection before admission: queue is safe.
        raise DeliveryError("Codex steering failed: %s" % error)
