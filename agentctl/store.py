"""Durable job store.

One directory per job under ``$AGENTCTL_HOME/jobs``:

    <job-id>/job.json      the record below, written atomically
    <job-id>/stream.jsonl  the child's raw stdout
    <job-id>/stderr.log    the child's raw stderr
    <job-id>/result.txt    the child's final message, once extracted

Writes take an exclusive lock on ``job.lock`` so the daemon's watcher threads
and a concurrent CLI call cannot interleave a read-modify-write.
"""

import errno
import fcntl
import json
import os
import time

from . import config, ids
from .errors import UnknownHandleError

# Job status values. Only the last three are terminal.
PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL = (SUCCEEDED, FAILED, CANCELLED)

# Delivery states for one registered wait.
DELIVERY_PENDING = "pending"
DELIVERED = "delivered"
DELIVERY_FAILED = "failed"


def now():
    return time.time()


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


def _atomic_write_json(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def record_path(job_id):
    return os.path.join(config.job_dir(job_id), "job.json")


def stream_path(job_id):
    return os.path.join(config.job_dir(job_id), "stream.jsonl")


def stderr_path(job_id):
    return os.path.join(config.job_dir(job_id), "stderr.log")


def result_path(job_id):
    return os.path.join(config.job_dir(job_id), "result.txt")


def lock_for(job_id):
    return _Lock(os.path.join(config.job_dir(job_id), "job.lock"))


def create(harness, prompt, cwd, command, model=None, followup_of=None,
           native_session_id=None, kind=None):
    """Create a new job record in ``pending`` and return it."""
    job_id = ids.new_job_id()
    os.makedirs(config.job_dir(job_id), exist_ok=True)
    record = {
        "job": job_id,
        "harness": harness,
        "native_session_id": native_session_id,
        "prompt": prompt,
        "cwd": cwd,
        "command": command,
        "model": model,
        "status": PENDING,
        "exit_code": None,
        "error": None,
        "pid": None,
        "created_at": now(),
        "started_at": None,
        "finished_at": None,
        "followup_of": followup_of,
        "cancel_requested": False,
        "waits": [],
        "stream": stream_path(job_id),
        "stderr": stderr_path(job_id),
        "result": result_path(job_id),
    }
    if kind is not None:
        record["kind"] = kind
    with lock_for(job_id):
        _atomic_write_json(record_path(job_id), record)
    return record


def read(job_id):
    """Read a job record. Unknown handles fail loudly."""
    path = record_path(job_id)
    try:
        with open(path) as handle:
            return json.load(handle)
    except IOError as exc:
        if exc.errno == errno.ENOENT:
            raise UnknownHandleError(
                "unknown job handle: %s" % job_id,
                hint="run 'agentctl list' to see known jobs",
            )
        raise


def exists(job_id):
    return os.path.isfile(record_path(job_id))


def update(job_id, **fields):
    """Apply field updates under the job lock and return the new record."""
    with lock_for(job_id):
        record = read(job_id)
        record.update(fields)
        _atomic_write_json(record_path(job_id), record)
    return record


def mutate(job_id, mutator):
    """Apply ``mutator(record)`` under the job lock and return the new record."""
    with lock_for(job_id):
        record = read(job_id)
        mutator(record)
        _atomic_write_json(record_path(job_id), record)
    return record


def add_wait(job_id, target_harness, target_id, raw_target):
    """Register one notification target and return the wait record."""
    wait = {
        "wait": ids.new_wait_id(),
        "notify_harness": target_harness,
        "notify_id": target_id,
        "notify_raw": raw_target,
        "registered_at": now(),
        "delivery": {
            "state": DELIVERY_PENDING,
            "notification_id": None,
            "attempted_at": None,
            "delivered_at": None,
            "error": None,
        },
    }

    def mutator(record):
        record["waits"].append(wait)

    mutate(job_id, mutator)
    return wait


def set_delivery(job_id, wait_id, state, notification_id=None, error=None, result=None):
    def mutator(record):
        for wait in record["waits"]:
            if wait["wait"] != wait_id:
                continue
            delivery = wait["delivery"]
            delivery["state"] = state
            delivery["attempted_at"] = now()
            if notification_id:
                delivery["notification_id"] = notification_id
            delivery["error"] = error
            if result is not None:
                delivery["result"] = result
            if state == DELIVERED:
                delivery["delivered_at"] = now()

    return mutate(job_id, mutator)


def list_jobs():
    """Every known job, newest first."""
    root = config.jobs_dir()
    if not os.path.isdir(root):
        return []
    records = []
    for name in os.listdir(root):
        if not name.startswith("job_"):
            continue
        try:
            records.append(read(name))
        except (UnknownHandleError, ValueError):
            continue
    records.sort(key=lambda item: item.get("created_at") or 0, reverse=True)
    return records


def is_terminal(record):
    return record.get("status") in TERMINAL


def write_result(job_id, text):
    with open(result_path(job_id), "w") as handle:
        handle.write(text or "")


def read_result(job_id):
    try:
        with open(result_path(job_id)) as handle:
            return handle.read()
    except IOError:
        return ""
