"""First-class session subscription store.

Subscriptions live outside individual job records so they can represent:
- auto-subscriptions created when agentctl starts or sends a request
- explicit subscriptions on externally running sessions

Store layout:
    $AGENTCTL_HOME/subs/<sub_id>.json

Each record:
  sub              subscription ID (sub_<hex>)
  target_harness   harness the request was sent to
  target_id        native session id of the target
  target_address   canonical address  harness:native-id
  target_turn_id   native turn/message id, once known (null until correlated)
  from_harness     caller harness (null if unknown)
  from_id          caller native session id (null if unknown)
  from_address     canonical address of caller (null if unknown)
  job              agentctl job id that caused this, if applicable (null for
                   externally-dispatched sends)
  created_at       float unix timestamp
  expires_at       float unix timestamp or null
  timeout          seconds or null
  status           pending | stored | delivered | failed | unknown | expired
  callback         delivery state dict (state, notification_id, error,
                   attempted_at, delivered_at)
  result           result text once available (null until delivered)
  result_stored_at float unix timestamp or null
  is_callback      True when this subscription itself is a callback receipt;
                   delivery for these never creates a return subscription
  dispatch_status  accepted | unknown | rejected | null (send-path only)
  request_id       unique request marker for Codex transcript correlation
  message_id       OpenCode messageID for response correlation
  in_flight        True while the external dispatch call is in progress
  observer_transcript_path  path to JSONL transcript being polled
  observer_cursor  byte offset of last read position in transcript
  observer_bound_turn_id    turn_id we confirmed our request belongs to
"""

import errno
import fcntl
import json
import os
import time

from . import config, ids
from .errors import UnknownHandleError

# Subscription status values
PENDING = "pending"
STORED = "stored"       # result available; no callback (no caller)
DELIVERED = "delivered"
FAILED = "failed"
UNKNOWN = "unknown"
EXPIRED = "expired"

# Callback delivery states
CB_PENDING = "pending"
CB_DELIVERED = "delivered"
CB_FAILED = "failed"
CB_UNKNOWN = "unknown"
CB_UNSUPPORTED = "unsupported"
CB_NOT_REQUESTED = "not_requested"   # no caller; no callback attempted
CB_NOT_CONFIGURED = "not_configured" # caller exists but callback not set up
CB_QUEUED = "queued"
CB_INFLIGHT = "in_flight"
CB_UNVERIFIED = "unverified"
CB_UNREACHABLE = "unreachable"       # callback route exists but unreachable


def subs_dir():
    return os.path.join(config.home(), "subs")


def sub_path(sub_id):
    return os.path.join(subs_dir(), sub_id + ".json")


def _lock_path(sub_id):
    return os.path.join(subs_dir(), sub_id + ".lock")


class _Lock(object):
    def __init__(self, path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.handle = open(self.path, "a+")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None
        return False


def _atomic_write(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def create(target_harness, target_id, from_harness=None, from_id=None,
           job=None, timeout=None, is_callback=False,
           request_id=None, message_id=None, target_turn_id=None):
    """Create a new subscription record and return it.

    ``target_harness`` and ``target_id`` are required. The caller's identity
    is optional; an unknown caller is recorded explicitly.
    """
    os.makedirs(subs_dir(), exist_ok=True)
    sub_id = ids.new_sub_id()
    now = time.time()
    target_address = "%s:%s" % (target_harness, target_id)
    from_address = ("%s:%s" % (from_harness, from_id)
                    if from_harness and from_id else None)
    record = {
        "sub": sub_id,
        "target_harness": target_harness,
        "target_id": target_id,
        "target_address": target_address,
        "target_turn_id": target_turn_id,
        "from_harness": from_harness,
        "from_id": from_id,
        "from_address": from_address,
        "job": job,
        "created_at": now,
        "expires_at": (now + timeout if timeout is not None else None),
        "timeout": timeout,
        "status": PENDING,
        "callback": {
            "state": CB_PENDING,
            "notification_id": None,
            "attempted_at": None,
            "delivered_at": None,
            "error": None,
        },
        "result": None,
        "result_stored_at": None,
        "is_callback": is_callback,
        # Send-path fields
        "dispatch_status": None,
        "request_id": request_id,
        "message_id": message_id,
        "in_flight": False,
        "observer_transcript_path": None,
        "observer_cursor": 0,
        "observer_bound_turn_id": None,
    }
    with _Lock(_lock_path(sub_id)):
        _atomic_write(sub_path(sub_id), record)
    return record


def read(sub_id):
    """Read a subscription record. Unknown ids fail loudly."""
    path = sub_path(sub_id)
    try:
        with open(path) as f:
            return json.load(f)
    except IOError as exc:
        if exc.errno == errno.ENOENT:
            raise UnknownHandleError(
                "unknown subscription: %s" % sub_id,
                hint="run 'agentctl listeners' to see known subscriptions")
        raise


def exists(sub_id):
    return os.path.isfile(sub_path(sub_id))


def update(sub_id, **fields):
    """Apply field updates under the lock and return the new record."""
    with _Lock(_lock_path(sub_id)):
        record = read(sub_id)
        record.update(fields)
        _atomic_write(sub_path(sub_id), record)
    return record


def set_callback(sub_id, state, notification_id=None, error=None):
    """Update the callback delivery state."""
    with _Lock(_lock_path(sub_id)):
        record = read(sub_id)
        cb = record["callback"]
        cb["state"] = state
        if notification_id is not None:
            cb["attempted_at"] = time.time()
        if notification_id is not None:
            cb["notification_id"] = notification_id
        if error is not None:
            cb["error"] = error
        if state == CB_DELIVERED:
            cb["delivered_at"] = time.time()
        _atomic_write(sub_path(sub_id), record)
    return record


def set_turn_id(sub_id, turn_id):
    """Record the native turn id once the target harness has assigned one."""
    return update(sub_id, target_turn_id=turn_id)


def record_result(sub_id, result_text):
    """Persist the result text without changing subscription status.

    Keeps result storage separate from callback delivery so deliver_sub_callback
    can still run after the result is stored.  Call this before triggering
    delivery; the delivery step sets the final status.
    """
    with _Lock(_lock_path(sub_id)):
        record = read(sub_id)
        record["result"] = result_text
        record["result_stored_at"] = time.time()
        _atomic_write(sub_path(sub_id), record)
    return record


def store_result(sub_id, result_text):
    """Persist result and mark delivered (no-callback path).

    Use when there is no callback to attempt.  If a callback will be
    attempted, call record_result() first, then deliver_sub_callback().
    """
    with _Lock(_lock_path(sub_id)):
        record = read(sub_id)
        record["result"] = result_text
        record["result_stored_at"] = time.time()
        record["status"] = DELIVERED
        _atomic_write(sub_path(sub_id), record)
    return record


def claim_inflight(sub_id):
    """Mark the subscription as in-flight before the external dispatch call.

    On daemon restart, in-flight subscriptions are moved to UNKNOWN so the
    caller knows acceptance was uncertain and must not auto-resend.
    """
    with _Lock(_lock_path(sub_id)):
        record = read(sub_id)
        record["in_flight"] = True
        _atomic_write(sub_path(sub_id), record)
    return record


def claim_callback(sub_id, notification_id):
    """Durably claim a remote callback before making any external call."""
    with _Lock(_lock_path(sub_id)):
        record = read(sub_id)
        cb = record["callback"]
        if record["status"] != PENDING or cb.get("state") not in (CB_PENDING, CB_UNVERIFIED):
            return False
        cb.update(state=CB_INFLIGHT, notification_id=notification_id,
                  attempted_at=time.time())
        _atomic_write(sub_path(sub_id), record)
    return True


def recover_inflight_to_unknown():
    """Recover dispatch/remote callback ambiguity without sending again."""
    for record in list_all():
        sub_id = record["sub"]
        if record.get("in_flight") and record.get("status") == PENDING:
            update(sub_id, in_flight=False, dispatch_status="unknown")
        cb = record.get("callback", {})
        if cb.get("state") == CB_INFLIGHT:
            state = inbox_state(record.get("from_address"), cb.get("notification_id"))
            if state == "queued":
                set_callback(sub_id, CB_QUEUED)
            elif state == "claimed":
                set_callback(sub_id, CB_DELIVERED)
                update(sub_id, status=DELIVERED)
            else:
                set_callback(sub_id, CB_UNKNOWN, error="daemon stopped during callback; acceptance uncertain")
                update(sub_id, status=UNKNOWN)
        elif cb.get("state") == CB_QUEUED:
            if inbox_state(record.get("from_address"), cb.get("notification_id")) == "claimed":
                set_callback(sub_id, CB_DELIVERED)
                update(sub_id, status=DELIVERED)



def list_all():
    """All known subscriptions, newest first."""
    root = subs_dir()
    if not os.path.isdir(root):
        return []
    records = []
    for name in os.listdir(root):
        if not name.endswith(".json"):
            continue
        sub_id = name[:-5]
        if not sub_id.startswith("sub_"):
            continue
        try:
            records.append(read(sub_id))
        except (UnknownHandleError, ValueError):
            continue
    records.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return records


def list_for_target(target_address):
    """Subscriptions targeting a specific session address."""
    return [r for r in list_all()
            if r.get("target_address") == target_address]


def list_for_job(job_id):
    """Subscriptions tied to a specific agentctl job."""
    return [r for r in list_all() if r.get("job") == job_id]


def notification_id(sub_id, event):
    """Stable callback notification id for dedup."""
    return ids.notification_id_for_sub(sub_id, event)


# ---------------------------------------------------------------------------
# Native session registry (tracks real-time state of observed sessions)
# ---------------------------------------------------------------------------

def _registry_dir():
    return os.path.join(config.home(), "registry")


def _registry_path(address):
    safe = address.replace("/", "_").replace(":", "_").replace("\\", "_")
    return os.path.join(_registry_dir(), safe + ".json")


def _registry_lock_path(address):
    safe = address.replace("/", "_").replace(":", "_").replace("\\", "_")
    return os.path.join(_registry_dir(), safe + ".lock")


def registry_read(address):
    """Return the registry entry for a session address, or None if unknown."""
    path = _registry_path(address)
    try:
        with open(path) as f:
            return json.load(f)
    except IOError:
        return None


def registry_update(address, **fields):
    """Update the registry entry for a session address atomically."""
    os.makedirs(_registry_dir(), exist_ok=True)
    path = _registry_path(address)
    lock_path = _registry_lock_path(address)
    with _Lock(lock_path):
        try:
            with open(path) as f:
                record = json.load(f)
        except IOError:
            record = {
                "address": address,
                "state": "idle",
                "current_turn_id": None,
            }
        record.update(fields)
        record["updated_at"] = time.time()
        # Ensure address is always set
        record.setdefault("address", address)
        _atomic_write(path, record)
    return record


# ---------------------------------------------------------------------------
# Per-session inbox (for Claude callback delivery)
# ---------------------------------------------------------------------------

def _inbox_dir():
    return os.path.join(config.home(), "inbox")


def _inbox_path(session_id):
    safe = session_id.replace("/", "_").replace(":", "_").replace("\\", "_")
    return os.path.join(_inbox_dir(), safe + ".jsonl")


def _inbox_lock_path(session_id):
    safe = session_id.replace("/", "_").replace(":", "_").replace("\\", "_")
    return os.path.join(_inbox_dir(), safe + ".lock")


def inbox_put(session_id, message, message_id):
    """Append a message to a session's inbox queue.

    Deduplicates by message_id: silently ignores duplicate puts. Messages
    start in state='queued'; inbox_claim_all transitions them to 'claimed'.
    """
    os.makedirs(_inbox_dir(), exist_ok=True)
    path = _inbox_path(session_id)

    with _Lock(_inbox_lock_path(session_id)):
        # Read existing records and check for duplicate
        existing = []
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        if r.get("message_id") == message_id:
                            return r  # already in inbox
                        existing.append(r)
                    except ValueError:
                        continue
        except IOError:
            pass

        record = {
            "message_id": message_id,
            "session_id": session_id,
            "message": message,
            "state": "queued",
            "claimed": False,
            "queued_at": time.time(),
        }
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
            f.flush(); os.fsync(f.fileno())
    return record


def inbox_list(session_id):
    """Return unclaimed (queued) inbox messages for a session."""
    path = _inbox_path(session_id)
    try:
        with open(path) as f:
            records = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    # Support both new state field and old claimed bool
                    is_queued = (r.get("state") == "queued" or
                                 (r.get("state") is None and not r.get("claimed")))
                    if is_queued:
                        records.append(r)
                except ValueError:
                    continue
        return records
    except IOError:
        return []


def inbox_claim_all(session_id):
    """Mark all pending inbox messages as claimed and return them."""
    path = _inbox_path(session_id)
    # If the inbox directory doesn't exist, there's nothing to claim.
    if not os.path.isdir(_inbox_dir()):
        return []
    os.makedirs(_inbox_dir(), exist_ok=True)
    claimed = []
    with _Lock(_inbox_lock_path(session_id)):
        try:
            with open(path) as f:
                records = [json.loads(l) for l in f if l.strip()]
        except (IOError, ValueError):
            return []
        updated = []
        for r in records:
            # Support both new state field and old claimed bool
            is_queued = (r.get("state") == "queued" or
                         (r.get("state") is None and not r.get("claimed")))
            if is_queued:
                r = dict(r, state="claimed", claimed=True,
                         claimed_at=time.time())
                claimed.append(r)
            updated.append(r)
        # Rewrite the file with all records updated
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            for r in updated:
                f.write(json.dumps(r) + "\n")
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    for message in claimed:
        for subscription in list_all():
            cb = subscription.get("callback", {})
            if cb.get("notification_id") == message.get("message_id"):
                set_callback(subscription["sub"], CB_DELIVERED)
                update(subscription["sub"], status=DELIVERED)
    return claimed


def inbox_state(address, message_id):
    if not address or not message_id:
        return None
    try:
        with open(_inbox_path(address)) as stream:
            for line in stream:
                record = json.loads(line)
                if record.get("message_id") == message_id:
                    return record.get("state") or ("claimed" if record.get("claimed") else "queued")
    except (OSError, ValueError):
        return None
    return None
