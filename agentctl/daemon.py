"""The agentctl daemon.

The daemon owns three things:

1. launching child runs and holding their process handles
2. watching each run to a deterministic terminal state
3. delivering each registered completion exactly once

A child is started in its own process session, so it survives the daemon. If
the daemon restarts it re-adopts a live child by polling its pid, and finalises
an already-exited child from the files on disk. A registered wait therefore
does not depend on the parent agent's shell staying alive, and it does not
depend on this process staying alive either.
"""

import json
import os
import signal
import socket
import sys
import threading
import time
import traceback

from . import children, config, envelope, ids, notify, store
from .errors import AgentctlError, UsageError

POLL_SECONDS = 0.5
SOCKET_BACKLOG = 32


def log(message):
    line = "%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), message)
    try:
        with open(config.daemon_log_path(), "a") as handle:
            handle.write(line)
    except IOError:
        pass


class Daemon(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.watched = set()
        self.operation_lock = threading.Lock()
        self.stopping = threading.Event()

    # ---- job lifecycle -------------------------------------------------

    def start_job(self, harness, prompt, cwd, model=None, followup_of=None,
                  session_id=None, request_id=None, options=None):
        payload = dict(harness=harness, prompt=prompt, cwd=cwd, model=model,
                       followup_of=followup_of, session_id=session_id)
        if options is not None:
            payload['options'] = options
        with self.operation_lock:
            return self._operation(request_id, payload,
                                   lambda: self._start_job(**payload))

    def _operation(self, request_id, payload, action):
        if request_id is None:
            return action()
        if not isinstance(request_id, str) or not request_id.strip():
            raise UsageError("request id must be nonempty")
        import hashlib
        path = os.path.join(config.home(), "request-" +
                            hashlib.sha256(request_id.encode()).hexdigest() + ".json")
        if os.path.exists(path):
            with open(path) as handle:
                prior = json.load(handle)
            if prior["payload"] != payload:
                raise UsageError("request id already used with different arguments")
            if prior["job"] is None:
                raise UsageError("request launch is ambiguous; reconcile it before retrying")
            return store.read(prior["job"])
        state = {"payload": payload, "job": None}
        store._atomic_write_json(path, state)
        before = {job['job'] for job in store.list_jobs()}
        try:
            result = action()
        except Exception:
            # If the operation created its durable job before failing, make the
            # same id recoverable. Otherwise leave an ambiguous intent and do
            # not launch it again.
            candidates = [job for job in store.list_jobs()
                          if job['job'] not in before and job.get('prompt') == payload.get('prompt')
                          and job.get('harness') == payload.get('harness')]
            if len(candidates) == 1:
                state['job'] = candidates[0]['job']
                store._atomic_write_json(path, state)
            raise
        state["job"] = result["job"]
        store._atomic_write_json(path, state)
        return result

    def deliver_session(self, target, prompt, request_id=None):
        harness, native = notify.parse_target(target)
        adapter = notify.get(harness)
        adapter.preflight(native)
        if not isinstance(prompt, str) or not prompt.strip():
            raise UsageError("follow-up prompt must not be empty")
        def deliver():
            record = store.create(harness, prompt, os.getcwd(), [],
                                  native_session_id=native, kind="session_delivery")
            job = record["job"]
            store.update(job, status=store.RUNNING, started_at=store.now())
            try:
                transport_result = adapter.deliver(native, prompt, "followup-" + job)
            except Exception as exc:
                store.update(job, status=store.FAILED, error=str(exc),
                             finished_at=store.now())
                raise
            store.write_result(job, "Message accepted by session transport; subsequent agent work is not tracked.")
            return store.update(job, status=store.SUCCEEDED, finished_at=store.now(),
                                transport_delivery=transport_result)
        with self.operation_lock:
            return self._operation(request_id, {"session": target, "prompt": prompt}, deliver)

    def interrupt_job(self, job_id, prompt, request_id=None):
        if not isinstance(prompt, str) or not prompt.strip():
            raise UsageError('correction must not be empty')
        original = store.read(job_id)
        if original.get('kind') == 'session_delivery' or not original.get('command'):
            raise UsageError('interrupt requires an agentctl worker job')
        if original['harness'] == 'opencode':
            from .errors import UnsupportedOperationError
            raise UnsupportedOperationError('interrupt cannot confirm termination of an OpenCode server turn')
        native = original.get('native_session_id')
        if not native:
            raise UsageError('worker has not reported a native session id yet; try again once it has')
        adapter = children.get(original['harness'])
        adapter.followup_command(native, original['cwd'], original.get('model'),
                                 store.result_path(job_id))
        payload = dict(interrupt_of=job_id, harness=original['harness'], prompt=prompt)
        def replace():
            current = store.read(job_id)
            if store.is_terminal(current):
                raise UsageError('interrupt requires a running worker; use followup for a finished job')
            self.cancel_job(job_id)
            deadline = time.monotonic() + 15
            while not store.is_terminal(store.read(job_id)):
                if time.monotonic() >= deadline:
                    raise UsageError('worker termination was not confirmed; replacement was not launched')
                time.sleep(.05)
            pid = current.get('pid')
            if pid and _pid_alive(pid):
                raise UsageError('worker process is still alive; replacement was not launched')
            if pid:
                try:
                    os.killpg(pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    os.killpg(pid, signal.SIGKILL)
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            os.killpg(pid, 0)
                        except ProcessLookupError:
                            break
                        if time.monotonic() >= deadline:
                            raise UsageError('worker process group has not exited; replacement was not launched')
                        time.sleep(.05)
            child = self._start_job(original['harness'], prompt, original['cwd'],
                                    original.get('model'), followup_of=job_id,
                                    session_id=native, options=original.get('options'))
            store.update(child['job'], interrupted_job=job_id)
            store.update(job_id, replacement_job=child['job'])
            return store.read(child['job'])
        with self.operation_lock:
            return self._operation(request_id, payload, replace)

    def _start_job(self, harness, prompt, cwd, model=None, followup_of=None,
                   session_id=None, options=None):
        """Create, launch, and begin watching one child run."""
        adapter = children.get(harness)
        option_args = adapter.option_args({} if options is None else options)
        cwd = os.path.abspath(cwd or os.getcwd())
        if not os.path.isdir(cwd):
            raise UsageError("working directory does not exist: %s" % cwd)
        if session_id is not None:
            active = [job['job'] for job in store.list_jobs()
                      if job.get('native_session_id') == session_id and not store.is_terminal(job)]
            if active:
                raise UsageError("native session already has active agentctl jobs: %s" %
                                 ', '.join(active))

        native = session_id
        if native is None:
            native = adapter.preallocate_session(cwd, model)

        record = store.create(harness, prompt, cwd, command=[], model=model,
                              followup_of=followup_of,
                              native_session_id=native)
        job_id = record["job"]

        if session_id is not None:
            argv = adapter.followup_command(native, cwd, model,
                                            store.result_path(job_id))
        else:
            argv = adapter.start_command(native, cwd, model,
                                         store.result_path(job_id))

        argv += option_args
        record = store.update(job_id, command=argv, options={} if options is None else options)
        self._launch(record, prompt)
        return store.read(job_id)

    def _launch(self, record, prompt):
        import subprocess

        job_id = record["job"]
        stream = open(store.stream_path(job_id), "ab", 0)
        errlog = open(store.stderr_path(job_id), "ab", 0)
        try:
            environment = os.environ.copy()
            if record["harness"] == "opencode":
                username, password = config.opencode_credentials()
                environment["OPENCODE_SERVER_USERNAME"] = username
                if password:
                    environment["OPENCODE_SERVER_PASSWORD"] = password
            process = subprocess.Popen(
                record["command"],
                cwd=record["cwd"],
                stdin=subprocess.PIPE,
                stdout=stream,
                stderr=errlog,
                env=environment,
                # Its own process session: the child outlives the daemon, and
                # cancelling it can signal the whole group.
                start_new_session=True,
            )
        except OSError as exc:
            stream.close()
            errlog.close()
            store.update(job_id, status=store.FAILED, error="child launch "
                         "failed: %s" % exc, finished_at=store.now())
            self._deliver_all(job_id)
            return

        try:
            process.stdin.write((prompt or "").encode("utf-8"))
        finally:
            process.stdin.close()

        stream.close()
        errlog.close()
        store.update(job_id, status=store.RUNNING, pid=process.pid,
                     started_at=store.now())
        self._watch(job_id, process=process)

    def _watch(self, job_id, process=None):
        with self.lock:
            if job_id in self.watched:
                return
            self.watched.add(job_id)
        thread = threading.Thread(target=self._watch_loop,
                                  args=(job_id, process), daemon=True)
        thread.start()

    def _watch_loop(self, job_id, process):
        try:
            if process is not None:
                while process.poll() is None:
                    self._observe_native(job_id)
                    time.sleep(POLL_SECONDS)
                self._observe_native(job_id)
                exit_code = process.returncode
            else:
                exit_code = self._poll_until_exit(job_id)
            self._finalise(job_id, exit_code)
        except Exception:
            log("watch failed for %s\n%s" % (job_id, traceback.format_exc()))
            store.update(job_id, status=store.FAILED,
                         error="agentctl lost track of the child",
                         finished_at=store.now())
            self._deliver_all(job_id)
        finally:
            with self.lock:
                self.watched.discard(job_id)

    def _observe_native(self, job_id):
        record = store.read(job_id)
        if record.get("native_session_id"):
            return
        outcome = _read_stream(store.stream_path(job_id), children.get(record["harness"]))
        if outcome.get("native_session_id"):
            store.update(job_id, native_session_id=outcome["native_session_id"])

    def _poll_until_exit(self, job_id):
        """Wait on a child this process did not spawn, by polling its pid."""
        record = store.read(job_id)
        pid = record.get("pid")
        while pid and _pid_alive(pid):
            if self.stopping.is_set():
                return None
            self._observe_native(job_id)
            time.sleep(POLL_SECONDS)
        # An adopted child's exit status is not available to a non-parent, so
        # the outcome is read from its own output instead.
        return None

    def _finalise(self, job_id, exit_code):
        """Decide the terminal status, then deliver every registered wait."""
        record = store.read(job_id)
        if store.is_terminal(record):
            self._deliver_all(job_id)
            return

        outcome = _read_stream(store.stream_path(job_id),
                               children.get(record["harness"]))

        native = outcome.get("native_session_id") or record.get(
            "native_session_id")
        result_text = outcome.get("result") or ""

        # Codex writes its own last message through --output-last-message.
        on_disk = store.read_result(job_id)
        if on_disk.strip():
            result_text = on_disk
        elif result_text:
            store.write_result(job_id, result_text)

        if record.get("cancel_requested"):
            status = store.CANCELLED
            error = "cancelled by agentctl"
        elif outcome.get("failed"):
            status = store.FAILED
            error = outcome.get("error") or "the child harness reported failure"
        elif exit_code is None:
            # Adopted child with no reachable exit status. A harness that
            # reported no failure and produced a final message succeeded;
            # anything else is a failure, not an assumption.
            if result_text.strip():
                status = store.SUCCEEDED
                error = None
            else:
                status = store.FAILED
                error = ("the child exited while the daemon was not its "
                         "parent and left no result")
        elif exit_code == 0:
            status = store.SUCCEEDED
            error = None
        else:
            status = store.FAILED
            error = "child exited with code %s" % exit_code
            tail = _tail(store.stderr_path(job_id))
            if tail:
                error += ": %s" % tail

        store.update(job_id, status=status, error=error, exit_code=exit_code,
                     native_session_id=native, finished_at=store.now())
        self._deliver_all(job_id)

    def cancel_job(self, job_id):
        record = store.read(job_id)
        if store.is_terminal(record):
            return record
        store.update(job_id, cancel_requested=True)
        pid = record.get("pid")
        if pid and _pid_alive(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except OSError:
                pass
            deadline = time.time() + 10
            while _pid_alive(pid) and time.time() < deadline:
                time.sleep(POLL_SECONDS)
            if _pid_alive(pid):
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except OSError:
                    pass
        else:
            store.update(job_id, status=store.CANCELLED,
                         error="cancelled before the child was running",
                         finished_at=store.now())
            self._deliver_all(job_id)
        return store.read(job_id)

    # ---- delivery ------------------------------------------------------

    def _deliver_all(self, job_id):
        record = store.read(job_id)
        if not store.is_terminal(record):
            return
        message = envelope.build(record, store.read_result(job_id))
        for wait in record.get("waits", []):
            self._deliver_one(job_id, wait["wait"], record, message)

    def _deliver_one(self, job_id, wait_id, record, message):
        """Deliver one wait, at most once, ever."""
        notification = None
        with self.lock:
            current = store.read(job_id)
            wait = _find_wait(current, wait_id)
            if wait is None:
                return
            delivery = wait["delivery"]
            expected = ids.notification_id(wait_id, job_id, current["status"])
            if delivery["state"] == store.DELIVERED and \
                    delivery.get("notification_id") == expected:
                return
            if delivery["state"] == store.DELIVERY_FAILED and \
                    delivery.get("notification_id") == expected:
                # A failed delivery stays failed. Retrying could duplicate a
                # follow-up that in fact arrived.
                return
            # Claim this delivery before leaving the lock.
            store.set_delivery(job_id, wait_id, store.DELIVERY_PENDING,
                               notification_id=expected)
            notification = (wait["notify_harness"], wait["notify_id"], expected)

        harness, target_id, notification_id = notification
        try:
            adapter = notify.get(harness)
            adapter.validate(target_id)
            transport_result = adapter.deliver(target_id, message, notification_id)
        except AgentctlError as exc:
            log("delivery failed for %s -> %s:%s: %s"
                % (job_id, harness, target_id, exc.message))
            store.set_delivery(job_id, wait_id, store.DELIVERY_FAILED,
                               notification_id=notification_id,
                               error=exc.message)
            return
        except Exception as exc:
            log("delivery crashed for %s: %s" % (job_id, traceback.format_exc()))
            store.set_delivery(job_id, wait_id, store.DELIVERY_FAILED,
                               notification_id=notification_id,
                               error="unexpected delivery error: %s" % exc)
            return
        store.set_delivery(job_id, wait_id, store.DELIVERED,
                           notification_id=notification_id, result=transport_result)
        log("delivered %s for %s to %s:%s"
            % (notification_id, job_id, harness, target_id))

    def register_wait(self, job_id, raw_target):
        """Register a durable wait, then deliver at once if already terminal.

        The target is checked for reachability before the wait is stored. A
        target that cannot receive a delivery fails here, while the calling
        agent is still watching, instead of silently much later.
        """
        harness, target_id = notify.parse_target(raw_target)
        record = store.read(job_id)
        notify.get(harness).preflight(target_id)
        wait = store.add_wait(job_id, harness, target_id, raw_target)
        if store.is_terminal(record):
            self._deliver_all(job_id)
        else:
            self._resume_if_needed(job_id)
        return _find_wait(store.read(job_id), wait["wait"])

    # ---- startup recovery ----------------------------------------------

    def recover(self):
        """Re-adopt or finalise every job left behind by a previous daemon."""
        for record in store.list_jobs():
            if record.get("kind") == "session_delivery" and not store.is_terminal(record):
                store.update(record["job"], status=store.FAILED,
                             error="session delivery interrupted; acceptance unknown; not retried",
                             finished_at=store.now())
                continue
            if store.is_terminal(record):
                # Deliver anything that was claimed but never sent.
                self._deliver_all(record["job"])
                continue
            self._resume_if_needed(record["job"])

    def _resume_if_needed(self, job_id):
        record = store.read(job_id)
        if store.is_terminal(record):
            return
        pid = record.get("pid")
        if pid and _pid_alive(pid):
            self._watch(job_id, process=None)
            return
        if record.get("status") == store.PENDING:
            store.update(job_id, status=store.FAILED,
                         error="the child was never launched",
                         finished_at=store.now())
            self._deliver_all(job_id)
            return
        self._finalise(job_id, None)


def _find_wait(record, wait_id):
    for wait in record.get("waits", []):
        if wait["wait"] == wait_id:
            return wait
    return None


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError as exc:
        import errno
        return exc.errno == errno.EPERM
    return True


def _tail(path, limit=400):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return " ".join(
                handle.read().decode("utf-8", "replace").split())[-limit:]
    except IOError:
        return ""


def _read_stream(path, adapter):
    """Fold a child's whole event stream into one outcome."""
    outcome = {}
    try:
        handle = open(path, "r")
    except IOError:
        return outcome
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            fragment = adapter.read_event(event)
            for key, value in (fragment or {}).items():
                if key == "failed" and not value:
                    continue
                outcome[key] = value
    return outcome
