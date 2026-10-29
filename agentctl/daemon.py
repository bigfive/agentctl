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
import math
import re
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback

from . import children, config, envelope, ids, notify, sessions, store
from .errors import AgentctlError, DeliveryError, InvalidTargetError, UsageError

POLL_SECONDS = 0.5
SOCKET_BACKLOG = 32
# Observer timeout: 1 hour max wait for external sessions
OBSERVER_TIMEOUT = 3600
OBSERVER_POLL_INTERVAL = 1.0
OPENCODE_OBSERVER_POLL_INTERVAL = 2.0


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
        self.delivering = set()
        self.operation_lock = threading.Lock()
        self.stopping = threading.Event()
        self.session_locks = {}
        self.observers = []
        self.timers = []
        self.search_index = None
        self.transcript_poller = None

    def start_transcript_polling(self):
        if self.transcript_poller is not None:
            return
        def run():
            while not self.stopping.is_set():
                self.poll_transcript_subscriptions()
                self.stopping.wait(60)
        self.transcript_poller = threading.Thread(target=run, daemon=True)
        self.transcript_poller.start()

    def poll_transcript_subscriptions(self):
        from . import transcripts
        snapshots = {}
        for sub in sessions.list_all():
            if (sub.get("status") != sessions.PENDING or sub.get("job")
                    or sub.get("observer_kind") != "transcript" or sub.get("result_stored_at") is not None):
                continue
            try:
                if sub.get("expires_at") is not None and sub["expires_at"] <= time.time():
                    self._expire_sub(sub["sub"])
                    continue
                address = sub["target_address"]
                if address not in snapshots:
                    snapshots[address] = transcripts.snapshot(sub["target_harness"], sub["target_id"])
                snapshot = snapshots[address]
                turn = snapshot.get("turns", {}).get(sub.get("target_turn_id"))
                if not snapshot.get("supported"):
                    sessions.update(sub["sub"], observation_error=snapshot.get("reason"))
                elif turn and turn["state"] != "running":
                    sessions.update(sub["sub"], completion_status=turn["state"], observation_error=None)
                    sessions.record_result(sub["sub"], turn.get("result") or "")
                    self.deliver_sub_callback(sub["sub"])
                elif not turn:
                    sessions.update(sub["sub"], observation_error="bound turn is not present in the saved transcript; completion remains unknown")
            except Exception as exc:
                sessions.update(sub["sub"], observation_error=str(exc))

    def start_search_index(self):
        from .search import SearchIndex
        with self.lock:
            if self.search_index is not None:
                return
            self.search_index = SearchIndex()
            worker = threading.Thread(target=self.search_index.maintain,
                                      args=(self.stopping,), daemon=True)
            worker.start()

    def search_sessions(self, **options):
        if self.search_index is None:
            self.start_search_index()
        index = self.search_index
        if not index.status()["last_refresh"] or (options.get("include_tools") and not index.status()["include_tools"]):
            index.refresh(include_tools=True if options.get("include_tools") else None)
        return index.search(**options)

    def search_status(self):
        if self.search_index is None:
            self.start_search_index()
        return self.search_index.status()

    def reindex_sessions(self, include_tools=False):
        if self.search_index is None:
            self.start_search_index()
        return self.search_index.refresh(force=True, include_tools=include_tools)

    # ---- job lifecycle -------------------------------------------------

    def start_job(self, harness, prompt, cwd, model=None, followup_of=None,
                  session_id=None, request_id=None, options=None,
                  subscriber_harness=None, subscriber_id=None, require_callback=False):
        payload = dict(harness=harness, prompt=prompt, cwd=cwd, model=model,
                       followup_of=followup_of, session_id=session_id,
                       subscriber_harness=subscriber_harness, subscriber_id=subscriber_id,
                       require_callback=require_callback)
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
                   session_id=None, options=None, subscriber_harness=None,
                   subscriber_id=None, require_callback=False):
        """Create, launch, and begin watching one child run."""
        callback_info = None
        if subscriber_harness and subscriber_id:
            from .sender import preflight_callback
            callback_info = preflight_callback(subscriber_harness, subscriber_id, require_callback)
        elif require_callback:
            raise UsageError("require-callback: caller identity is unknown")
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

        # Create subscription atomically before launch so a fast child cannot
        # complete before its listener exists.
        sub_record = None
        if subscriber_harness and subscriber_id:
            sub_record = sessions.create(
                target_harness=harness,
                target_id=native or job_id,
                from_harness=subscriber_harness,
                from_id=subscriber_id,
                job=job_id,
            )
            self._set_callback_availability(sub_record["sub"], callback_info)
            sub_record = sessions.read(sub_record["sub"])

        if session_id is not None:
            argv = adapter.followup_command(native, cwd, model,
                                            store.result_path(job_id))
        else:
            argv = adapter.start_command(native, cwd, model,
                                         store.result_path(job_id))

        argv += option_args
        record = store.update(job_id, command=argv, options={} if options is None else options)
        self._launch(record, prompt)
        result = store.read(job_id)
        if sub_record:
            result["_subscription"] = sub_record
        return result

    def _launch(self, record, prompt):
        import subprocess

        job_id = record["job"]
        stream = open(store.stream_path(job_id), "ab", 0)
        errlog = open(store.stderr_path(job_id), "ab", 0)

        # Build a clean environment: strip identity vars that a parent agent
        # may have set so a child cannot impersonate a different harness.
        _IDENTITY_VARS = (
            "CODEX_THREAD_ID",
            "CLAUDE_SESSION_ID",
            "CURSOR_SESSION_ID",
            "OPENCODE_SESSION_ID",
            "AGENTCTL_FROM",
            "CLAUDE_ENV_FILE",
        )
        env = dict(os.environ)
        for _var in _IDENTITY_VARS:
            env.pop(_var, None)

        # Inject the child's own identity so it can identify itself in callbacks.
        harness = record.get("harness", "")
        native_session_id = record.get("native_session_id")
        if native_session_id:
            if harness == "claude":
                env["CLAUDE_SESSION_ID"] = native_session_id
            elif harness == "cursor":
                env["CURSOR_SESSION_ID"] = native_session_id
            # Codex assigns its own CODEX_THREAD_ID internally; don't pre-inject.

        try:
            environment = env
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
                    self._refresh_native_identity(job_id)
                    time.sleep(POLL_SECONDS)
                self._refresh_native_identity(job_id)
                exit_code = process.wait()
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

    def _refresh_native_identity(self, job_id):
        record = store.read(job_id)
        cursor = record.get("identity_cursor", 0)
        next_cursor, events = _read_transcript_from(store.stream_path(job_id), cursor)
        adapter = children.get(record["harness"])
        native = record.get("native_session_id")
        for event in events:
            native = (adapter.read_event(event) or {}).get("native_session_id") or native
        fields = {"identity_cursor": next_cursor}
        if native:
            fields["native_session_id"] = native
        store.update(job_id, **fields)
        if native:
            for sub in sessions.list_for_job(job_id):
                if sub.get("target_id") != native:
                    sessions.update(sub["sub"], target_id=native,
                                    target_address="%s:%s" % (record["harness"], native))

    def _poll_until_exit(self, job_id):
        """Wait on a child this process did not spawn, by polling its pid."""
        record = store.read(job_id)
        pid = record.get("pid")
        while pid and _pid_alive(pid):
            if self.stopping.is_set():
                return None
            self._refresh_native_identity(job_id)
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

    # ---- send_session --------------------------------------------------

    def _set_callback_availability(self, sub_id, info):
        if info and not info.get("ok"):
            sessions.set_callback(sub_id, info.get("state") or "unsupported", error=info.get("note"))

    def _target_lock(self, harness, session_id):
        with self.lock:
            return self.session_locks.setdefault((harness, session_id), threading.RLock())

    def send_session(self, harness, session_id, prompt, model=None, **kwargs):
        with self._target_lock(harness, session_id):
            return self._send_session(harness, session_id, prompt, model=model, **kwargs)

    def _send_session(self, harness, session_id, prompt, model=None,
                     subscriber_harness=None, subscriber_id=None,
                     require_callback=False):
        """Dispatch a prompt to an existing external session.

        Creates a durable request record before any dispatch attempt and
        always returns it. Validates session existence before dispatching.
        Busy check fires even when no subscriber (--no-notify).

        Capability/existence/callback checks that fail raise errors without
        creating any sub record (no side effects before record creation).
        """
        from . import sender as _sender, discover as _discover

        # --- Pre-record checks (no side effects) ---

        kind = _sender.target_kind(harness, session_id)
        cap = _sender.send_capability(harness, session_id)

        if not cap.get("supported"):
            from .errors import UnsupportedOperationError
            raise UnsupportedOperationError(
                "sending to %s is not supported: %s" % (kind, cap["description"]),
                hint="use 'agentctl start %s' to launch a new session" % harness)

        # Callback preflight (raises for require_callback; read-only otherwise)
        cb_info = {"ok": False, "state": "not_requested"}
        if subscriber_harness and subscriber_id:
            cb_info = _sender.preflight_callback(
                subscriber_harness, subscriber_id,
                require_callback=require_callback)
        elif require_callback:
            from .errors import UnsupportedOperationError
            raise UnsupportedOperationError(
                "require-callback: no subscriber specified",
                hint="pass --from harness:session-id")

        # Session existence check (no side effects)
        found = self._validate_session_exists(harness, session_id)

        # Busy check for non-queued harnesses (no side effects)
        if not cap.get("queue", True):
            self._check_busy_for_send(harness, session_id)

        # --- Create durable request record ---

        request_id = "req_" + os.urandom(6).hex()

        sub_record = sessions.create(
            target_harness=harness,
            target_id=session_id,
            from_harness=subscriber_harness,
            from_id=subscriber_id,
            request_id=request_id,
            timeout=OBSERVER_TIMEOUT,
        )
        sub_id = sub_record["sub"]

        # Set initial callback state based on preflight result
        if not subscriber_harness or not subscriber_id:
            sessions.set_callback(sub_id, sessions.CB_NOT_REQUESTED)
        elif not cb_info.get("ok"):
            _state_map = {
                "unsupported": sessions.CB_UNSUPPORTED,
                "not_configured": sessions.CB_NOT_CONFIGURED,
                "unreachable": sessions.CB_UNREACHABLE,
                "busy": "busy", "unverified": sessions.CB_UNVERIFIED,
            }
            cb_st = _state_map.get(cb_info.get("state"), sessions.CB_UNSUPPORTED)
            sessions.set_callback(sub_id, cb_st, error=cb_info.get("note"))

        self._schedule_sub_timeout(sub_id, sub_record["expires_at"])
        # --- Dispatch ---

        # Persist in_flight before making the external call so that a crash
        # during dispatch results in UNKNOWN (not re-sent) on recovery.
        sessions.claim_inflight(sub_id)

        try:
            self._do_send_dispatch(
                sub_id, kind, harness, session_id, prompt, model,
                found, request_id, subscriber_harness, subscriber_id, cb_info)
        except Exception:
            log("send_session unexpected error for %s\n%s"
                % (sub_id, traceback.format_exc()))
            try:
                sessions.update(sub_id, dispatch_status="unknown", in_flight=False)
            except Exception:
                pass

        result = sessions.read(sub_id)
        # Add dispatch summary for CLI
        result["dispatch"] = {
            "accepted": result.get("dispatch_status") == "accepted",
            "unknown": result.get("dispatch_status") == "unknown",
            "rejected": result.get("dispatch_status") == "rejected",
        }
        return result

    def _do_send_dispatch(self, sub_id, kind, harness, session_id, prompt,
                          model, found, request_id, subscriber_harness,
                          subscriber_id, cb_info):
        """Execute the actual dispatch and start observers. Updates sub record."""
        from . import sender as _sender

        try:
            if kind == "cursor-cli":
                cwd = found.get("cwd") if found else None
                job_record = self.resume_external(
                    harness, session_id, prompt, cwd or os.getcwd(),
                    model=model, subscription_id=sub_id)
                sessions.update(sub_id, dispatch_status="accepted", in_flight=False)
                if job_record["status"] == store.FAILED:
                    sessions.update(sub_id, dispatch_status="rejected")

            elif kind == "codex":
                # Codex: queue with unique request marker for turn correlation
                marked_prompt = "[agentctl request: %s]\n%s" % (request_id, prompt)
                try:
                    transcript_path = found.get("transcript_path") if found else None
                    cursor = os.path.getsize(transcript_path) if transcript_path else 0
                    sessions.update(sub_id, observer_transcript_path=transcript_path,
                                    observer_cursor=cursor)
                    receipt = _sender.dispatch_to_session(
                        harness, session_id, marked_prompt, model)
                    if receipt.accepted:
                        dispatch_status = "accepted"
                    elif receipt.unknown:
                        dispatch_status = "unknown"
                    else:
                        dispatch_status = "rejected"
                    transcript_path = found.get("transcript_path") if found else None
                    sessions.update(sub_id,
                                    dispatch_status=dispatch_status,
                                    observer_transcript_path=transcript_path,
                                    in_flight=False)
                    if dispatch_status in ("accepted", "unknown"):
                        # Start observer to track this request in the transcript
                        self._start_codex_observer(
                            sub_id, session_id, request_id,
                            transcript_path, start_cursor=cursor)
                        log("send_session codex dispatch %s sub %s"
                            % (dispatch_status, sub_id))
                except AgentctlError as exc:
                    sessions.update(sub_id, dispatch_status="rejected",
                                    dispatch_error=exc.message, in_flight=False)
                    sessions.set_callback(sub_id, sessions.CB_FAILED,
                                          error=exc.message)
                    sessions.update(sub_id, status=sessions.FAILED)
                    log("send_session codex dispatch failed for %s: %s"
                        % (sub_id, exc.message))

            elif kind == "opencode":
                # OpenCode: queued v2 prompt with stable message_id for correlation
                try:
                    receipt = _sender._dispatch_opencode(
                        session_id, prompt, request_nonce=request_id)
                    if receipt.accepted:
                        msg_id = receipt.details.get("message_id")
                        sessions.update(sub_id,
                                        dispatch_status="accepted",
                                        message_id=msg_id,
                                        in_flight=False)
                        self._start_opencode_observer(sub_id, session_id, msg_id)
                        log("send_session opencode dispatch accepted sub %s msg %s"
                            % (sub_id, msg_id))
                    elif receipt.unknown:
                        msg_id = (receipt.details.get("message_id")
                                  if receipt.details else None)
                        sessions.update(sub_id,
                                        dispatch_status="unknown",
                                        message_id=msg_id,
                                        in_flight=False)
                        if msg_id:
                            self._start_opencode_observer(sub_id, session_id, msg_id)
                    else:
                        sessions.update(sub_id, dispatch_status="rejected",
                                        in_flight=False)
                        sessions.update(sub_id, status=sessions.FAILED)
                except AgentctlError as exc:
                    sessions.update(sub_id, dispatch_status="rejected",
                                    dispatch_error=exc.message, in_flight=False)
                    sessions.set_callback(sub_id, sessions.CB_FAILED,
                                          error=exc.message)
                    sessions.update(sub_id, status=sessions.FAILED)
                    log("send_session opencode dispatch failed for %s: %s"
                        % (sub_id, exc.message))
            else:
                sessions.update(sub_id, dispatch_status="rejected", in_flight=False)
                sessions.update(sub_id, status=sessions.FAILED)

        except Exception:
            log("send_session _do_dispatch error for %s\n%s"
                % (sub_id, traceback.format_exc()))
            try:
                sessions.update(sub_id, in_flight=False)
            except Exception:
                pass
            raise

    def _validate_session_exists(self, harness, session_id):
        """Check session existence; raise InvalidTargetError if not found.

        Returns the discovered session dict (for transcript_path, cwd etc)
        or None for opencode (validated via HTTP).
        """
        from . import discover as _discover

        if harness == "opencode":
            self._validate_opencode_session(session_id)
            return None
        else:
            all_sessions = _discover.discover([harness])
            found = next(
                (s for s in all_sessions if s.get("native_id") == session_id),
                None)
            if found is None:
                raise InvalidTargetError(
                    "session %s:%s not found in local discovery"
                    % (harness, session_id),
                    hint="run 'agentctl sessions list --harness %s' to see "
                         "available sessions" % harness)
            return found

    def _validate_opencode_session(self, session_id):
        """Validate an OpenCode session exists via HTTP; raise on failure."""
        try:
            from . import httpc as _httpc
            base = config.opencode_base_url()
            status, body = _httpc.request(
                "GET", "%s/api/session/%s" % (base, session_id), timeout=5,
                auth_header=config.opencode_auth_header())
            if status == 404:
                raise InvalidTargetError(
                    "OpenCode session %s not found on server" % session_id)
            if status != 200:
                raise InvalidTargetError(
                    "OpenCode returned HTTP %s for session %s"
                    % (status, session_id))
        except InvalidTargetError:
            raise
        except AgentctlError:
            raise
        except Exception as exc:
            raise DeliveryError(
                "OpenCode server unreachable: %s" % exc,
                hint="start 'opencode serve' or check AGENTCTL_OPENCODE_URL")

    def _check_busy_for_send(self, harness, session_id):
        """Raise DeliveryError if session is busy (no queue, non-queued harness).

        Checks saved native turn markers and running managed jobs. Applies even with no subscriber (--no-notify).
        """
        target_address = "%s:%s" % (harness, session_id)
        from . import transcripts
        current = transcripts.snapshot(harness, session_id)
        if current.get("active_turn"):
            raise DeliveryError(
                "%s is busy (currently processing a turn; no durable queue)"
                % target_address,
                hint="wait for the current turn to complete before sending")

        # Check for running managed jobs driving this session
        running = [r for r in store.list_jobs()
                   if r.get("native_session_id") == session_id
                   and r.get("status") == store.RUNNING]
        if running:
            raise DeliveryError(
                "%s is busy (managed job %s is running)"
                % (target_address, running[0]["job"]),
                hint="wait: 'agentctl wait %s'" % running[0]["job"])

        # For opencode: also check HTTP session status
        if harness == "opencode":
            self._check_opencode_session_busy(session_id)

    def _check_opencode_session_busy(self, session_id):
        from . import httpc
        try:
            code, body = httpc.request("GET", "%s/api/session/status" % config.opencode_base_url(), timeout=5,
                                       auth_header=config.opencode_auth_header())
        except httpc.HttpError as exc:
            raise InvalidTargetError("cannot inspect OpenCode busy state: %s" % exc)
        if code != 200 or not isinstance(body, dict):
            raise InvalidTargetError("OpenCode session/status returned an invalid status map")
        state = body.get(session_id, {}).get("type", "idle")
        if state != "idle":
            raise DeliveryError("OpenCode session %s is busy (%s); no durable request queue" % (session_id, state))

    # ---- Codex observer ------------------------------------------------

    def _start_codex_observer(self, sub_id, thread_id, request_id,
                               transcript_path, start_cursor=None):
        """Start a background thread polling the Codex transcript."""
        if transcript_path is None:
            sessions.record_result(sub_id, "")
            sessions.set_callback(sub_id, sessions.CB_FAILED,
                                  error="no Codex transcript path; cannot observe")
            sessions.update(sub_id, status=sessions.FAILED)
            return

        if start_cursor is None:
            try:
                start_cursor = os.path.getsize(transcript_path)
            except OSError:
                start_cursor = 0
            sessions.update(sub_id, observer_cursor=start_cursor)

        thread = threading.Thread(
            target=self._codex_observer_loop,
            args=(sub_id, thread_id, request_id, transcript_path, start_cursor),
            daemon=True)
        self.observers.append(thread)
        thread.start()

    def _codex_observer_loop(self, sub_id, thread_id, request_id, transcript_path, start_cursor):
        """Observe the requested turn, including real task_started-before-user ordering."""
        marker = "[agentctl request: %s]" % request_id
        cursor = start_cursor
        deadline = time.monotonic() + OBSERVER_TIMEOUT
        while time.monotonic() < deadline and not self.stopping.is_set():
            sub = sessions.read(sub_id)
            if sub["status"] != sessions.PENDING or sub.get("result_stored_at") is not None:
                return
            cursor = max(cursor, sub.get("observer_cursor") or 0)
            active = sub.get("observer_active_turn")
            bound = sub.get("target_turn_id") or sub.get("observer_bound_turn_id")
            seen = bool(sub.get("observer_found_marker"))
            new_cursor, events = _read_transcript_from(transcript_path, cursor)
            for event in events:
                payload = event.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                kind = payload.get("type")
                if event.get("type") == "event_msg" and kind == "task_started":
                    active = payload.get("turn_id")
                    if seen and not bound:
                        bound = active
                if event.get("type") in ("turn_context", "event_msg") and kind == "turn_context":
                    active = payload.get("turn_id") or active
                text = ""
                if event.get("type") == "response_item" and payload.get("role") == "user":
                    text = "\n".join(c.get("text", "") for c in payload.get("content", []) if isinstance(c, dict))
                elif event.get("type") == "event_msg" and kind in ("user_message", "turn_context"):
                    content = payload.get("message") or payload.get("input") or ""
                    text = content if isinstance(content, str) else "\n".join(
                        c.get("text", "") for c in content if isinstance(c, dict))
                if marker in text:
                    seen = True
                    bound = payload.get("turn_id") or active or bound
                if bound:
                    sessions.update(sub_id, target_turn_id=bound, observer_bound_turn_id=bound)
                if event.get("type") == "event_msg" and kind in ("task_complete", "turn_aborted"):
                    completed_turn = payload.get("turn_id") or active
                    if seen and bound and completed_turn == bound:
                        outcome = "interrupted" if kind == "turn_aborted" else "completed"
                        sessions.update(sub_id, completion_status=outcome)
                        sessions.record_result(sub_id, payload.get("last_agent_message") or "")
                        self.deliver_sub_callback(sub_id)
                        return
            sessions.update(sub_id, observer_cursor=new_cursor,
                            observer_active_turn=active, observer_found_marker=seen,
                            observer_bound_turn_id=bound)
            cursor = new_cursor
            self.stopping.wait(OBSERVER_POLL_INTERVAL)
        if not self.stopping.is_set():
            sessions.update(sub_id, status=sessions.EXPIRED, completion_status="unknown",
                            observer_error="requested native turn was not observed before timeout")

    # ---- OpenCode observer ---------------------------------------------

    def _start_opencode_observer(self, sub_id, session_id, message_id):
        """Start a background thread polling OpenCode for our message's response."""
        if not message_id:
            return
        thread = threading.Thread(
            target=self._opencode_observer_loop,
            args=(sub_id, session_id, message_id),
            daemon=True)
        self.observers.append(thread)
        thread.start()

    def _opencode_observer_loop(self, sub_id, session_id, message_id):
        """Poll OpenCode HTTP API until our dispatched message gets a response.

        Looks for a response message whose parentID matches our messageID and
        whose completion time is set. Does not fire on unrelated messages.
        """
        waited = 0

        while waited < OBSERVER_TIMEOUT:
            if self.stopping.is_set():
                return

            try:
                sub = sessions.read(sub_id)
            except Exception:
                return
            if sub.get("status") != sessions.PENDING:
                return

            try:
                from . import httpc as _httpc
                base = config.opencode_base_url()
                status, body = _httpc.request(
                    "GET", "%s/api/session/%s/message" % (base, session_id),
                    timeout=10, auth_header=config.opencode_auth_header())
                if isinstance(body, dict) and "data" in body:
                    body = body["data"]
                if status == 200 and isinstance(body, list):
                    for msg in body:
                        if not isinstance(msg, dict):
                            continue
                        info = msg.get("info") or {}
                        # The assistant response to our message has parentID
                        parent_id = info.get("parentID") or info.get("parent_id")
                        if parent_id != message_id:
                            continue
                        time_info = info.get("time") or {}
                        completed = (time_info.get("completed") or
                                     info.get("completedAt") or
                                     info.get("time_completed"))
                        if info.get("error") or info.get("status") in ("error", "failed"):
                            sessions.update(sub_id, completion_status="failed", completion_error=info.get("error"))
                            sessions.record_result(sub_id, "\n".join(
                                part.get("text", "") for part in msg.get("parts", [])
                                if isinstance(part, dict) and part.get("type") == "text"))
                            self.deliver_sub_callback(sub_id)
                            return
                        finish = info.get("finish")
                        if (completed or info.get("status") == "completed") and finish not in ("tool-calls", "unknown"):
                            if info.get("role") not in (None, "assistant"):
                                continue
                            parts = msg.get("parts") or msg.get("content") or []
                            texts = [p.get("text", "") for p in parts
                                     if isinstance(p, dict)
                                     and p.get("type") == "text"]
                            result = "\n".join(texts).strip()
                            sessions.update(sub_id, completion_status="completed", target_turn_id=info.get("id"))
                            sessions.record_result(sub_id, result)
                            self.deliver_sub_callback(sub_id)
                            log("opencode observer completed for %s" % sub_id)
                            return
            except Exception as exc:
                log("opencode observer poll error for %s: %s" % (sub_id, exc))

            self.stopping.wait(OPENCODE_OBSERVER_POLL_INTERVAL)
            waited += OPENCODE_OBSERVER_POLL_INTERVAL

        log("opencode observer timed out for %s" % sub_id)
        sessions.record_result(sub_id, "")
        sessions.set_callback(sub_id, sessions.CB_FAILED,
                              error="OpenCode observer timed out after %ss"
                              % OBSERVER_TIMEOUT)
        sessions.update(sub_id, status=sessions.FAILED)

    # ---- subscription delivery -----------------------------------------

    def deliver_sub_callback(self, sub_id):
        record = sessions.read(sub_id)
        if record["status"] != sessions.PENDING or record.get("result_stored_at") is None:
            return
        if record.get("is_callback"):
            sessions.update(sub_id, status=sessions.STORED)
            return
        cb_state = record.get("callback", {}).get("state")
        if cb_state in (sessions.CB_QUEUED, sessions.CB_INFLIGHT):
            return
        if cb_state in (sessions.CB_DELIVERED, sessions.CB_FAILED, sessions.CB_UNKNOWN):
            terminal = {sessions.CB_DELIVERED: sessions.DELIVERED,
                        sessions.CB_FAILED: sessions.FAILED, sessions.CB_UNKNOWN: sessions.UNKNOWN}
            sessions.update(sub_id, status=terminal[cb_state])
            return
        if not record.get("from_address"):
            sessions.set_callback(sub_id, sessions.CB_NOT_REQUESTED)
            sessions.update(sub_id, status=sessions.STORED)
            return
        from .sender import caller_kind
        if caller_kind(record["from_harness"], record["from_id"]) in ("claude", "cursor-ide"):
            sessions.set_callback(sub_id, sessions.CB_UNSUPPORTED,
                                  error="hook-based callback receivers have been removed; the result remains readable")
            sessions.update(sub_id, status=sessions.STORED)
            return
        if cb_state in (sessions.CB_UNSUPPORTED, sessions.CB_NOT_CONFIGURED,
                        sessions.CB_UNREACHABLE, "busy"):
            sessions.update(sub_id, status=sessions.STORED)
            return
        notification = ids.notification_id_for_sub(sub_id, record.get("completion_status") or "completed")
        if not sessions.claim_callback(sub_id, notification):
            return
        message = _build_sub_envelope(record, record.get("result") or "")
        try:
            from .sender import caller_kind
            kind = caller_kind(record["from_harness"], record["from_id"])
            if kind == "cursor-ide":
                receipt = sessions.inbox_put(record["from_address"], message, notification)
            else:
                adapter = notify.get(record["from_harness"])
                adapter.validate(record["from_id"])
                receipt = adapter.deliver(record["from_id"], message, notification)
            if isinstance(receipt, dict):
                sessions.update(sub_id, transport_delivery=receipt)
            if isinstance(receipt, dict) and receipt.get("state") == "queued":
                sessions.set_callback(sub_id, sessions.CB_QUEUED, notification_id=notification)
                # The receiver may have claimed concurrently before this update.
                if sessions.inbox_state(record["from_address"], notification) == "claimed":
                    sessions.set_callback(sub_id, sessions.CB_DELIVERED)
                    sessions.update(sub_id, status=sessions.DELIVERED)
            else:
                sessions.set_callback(sub_id, sessions.CB_DELIVERED, notification_id=notification)
                sessions.update(sub_id, status=sessions.DELIVERED)
        except subprocess.TimeoutExpired as exc:
            process = getattr(exc, "process", None)
            if process is not None:
                process.kill(); process.communicate()
            sessions.set_callback(sub_id, sessions.CB_UNKNOWN, error="native callback timed out; acceptance unknown")
            sessions.update(sub_id, status=sessions.UNKNOWN)
        except AgentctlError as exc:
            state = sessions.CB_UNKNOWN if getattr(exc, "acceptance_unknown", False) else sessions.CB_FAILED
            sessions.set_callback(sub_id, state, error=exc.message)
            sessions.update(sub_id, status=sessions.UNKNOWN if state == sessions.CB_UNKNOWN else sessions.FAILED)
        except Exception as exc:
            # Once a remote attempt starts, an unexpected exception is ambiguous.
            sessions.set_callback(sub_id, sessions.CB_UNKNOWN, error=str(exc))
            sessions.update(sub_id, status=sessions.UNKNOWN)

    def resume_external(self, harness, session_id, prompt, cwd, model=None, **kwargs):
        with self._target_lock(harness, session_id):
            return self._resume_external(harness, session_id, prompt, cwd, model=model, **kwargs)

    def _resume_external(self, harness, session_id, prompt, cwd, model=None,
                         subscriber_harness=None, subscriber_id=None, subscription_id=None,
                         require_callback=False):
        """Resume an externally-created session as a tracked agentctl job.

        Unlike start_job, this uses followup_command (resume) and the session
        must be validated by the caller before calling this method.
        Unknown sessions must never be silently created as fresh ones.

        For non-queued harnesses (Cursor), rejects if another job is already
        running for the same native session (busy check before launch).
        """
        callback_info = None
        if subscriber_harness and subscriber_id:
            from .sender import preflight_callback
            callback_info = preflight_callback(subscriber_harness, subscriber_id, require_callback)
        elif require_callback:
            raise UsageError("require-callback: caller identity is unknown")
        adapter = children.get(harness)
        cwd = os.path.abspath(cwd or os.getcwd())
        if not os.path.isdir(cwd):
            raise UsageError("working directory does not exist: %s" % cwd)

        found = self._validate_session_exists(harness, session_id)
        if found and found.get("capability", {}).get("resume") is False:
            from .errors import UnsupportedOperationError
            raise UnsupportedOperationError(found["capability"].get("resume_reason") or "this session kind cannot be resumed")
        from .sender import target_kind
        if target_kind(harness, session_id) == "cursor-ide":
            from .errors import UnsupportedOperationError
            raise UnsupportedOperationError("Cursor IDE sessions cannot be resumed through the CLI store")
        self._check_busy_for_send(harness, session_id)

        record = store.create(harness, prompt, cwd, command=[],
                              native_session_id=session_id)
        job_id = record["job"]

        sub_record = None
        if subscriber_harness and subscriber_id:
            sub_record = sessions.create(
                target_harness=harness,
                target_id=session_id,
                from_harness=subscriber_harness,
                from_id=subscriber_id,
                job=job_id,
            )
            self._set_callback_availability(sub_record["sub"], callback_info)
            sub_record = sessions.read(sub_record["sub"])

        if subscription_id:
            sessions.update(subscription_id, job=job_id, dispatch_status="accepted", in_flight=False)
        # Use followup_command since we're resuming an existing session.
        # Capture the updated record so _launch gets the real command.
        argv = adapter.followup_command(session_id, cwd, model,
                                        store.result_path(job_id))
        record = store.update(job_id, command=argv)
        self._launch(record, prompt)
        result = store.read(job_id)
        if sub_record:
            result["_subscription"] = sub_record
        return result

    def notify_hook_event(self, target_address, event="complete", turn_id=None,
                          transcript_path=None, result=None, request_id=None,
                          prompt=None, stop_hook_active=False, status=None):
        registry = sessions.registry_read(target_address) or {}
        if event == "start":
            sessions.registry_update(target_address, state="idle", current_turn_id=None,
                                     transcript_path=transcript_path)
            return 0
        if event == "receive":
            sessions.registry_update(target_address, receiver_until=time.time() + 550)
            return 0
        if event == "begin":
            turn_id = turn_id or "hook_" + os.urandom(8).hex()
            sessions.registry_update(target_address, state="running", current_turn_id=turn_id,
                                     transcript_path=transcript_path or registry.get("transcript_path"))
            marker = re.search(r"\[agentctl request: (req_[a-f0-9]+)\]", prompt or "")
            request_id = request_id or (marker.group(1) if marker else None)
            if request_id:
                for sub in sessions.list_for_target(target_address):
                    if sub["status"] == sessions.PENDING and sub.get("request_id") == request_id:
                        sessions.update(sub["sub"], target_turn_id=turn_id,
                                        observer_bound_turn_id=turn_id)
            return 0
        if event != "complete":
            raise UsageError("unknown native hook event: %s" % event)
        turn_id = turn_id or registry.get("current_turn_id")
        if stop_hook_active:
            sessions.registry_update(target_address, provisional_stop=True)
            return 0
        if turn_id and turn_id == registry.get("current_turn_id"):
            sessions.registry_update(target_address, state="idle", current_turn_id=None)
        result_text = result if result is not None else _read_result_from_transcript(
            transcript_path or registry.get("transcript_path"))
        triggered = 0
        for sub in sessions.list_for_target(target_address):
            if sub["status"] != sessions.PENDING or sub.get("job"):
                continue
            if not turn_id or sub.get("target_turn_id") != turn_id:
                continue
            sessions.update(sub["sub"], completion_status=status or "completed")
            sessions.record_result(sub["sub"], result_text or "")
            self.deliver_sub_callback(sub["sub"])
            triggered += 1
        return triggered

    def notify_hook_complete(self, target_address, turn_id=None):
        """Process a 'agentctl _hook complete' event. Backward-compat wrapper."""
        return self.notify_hook_event(target_address, event="complete",
                                      turn_id=turn_id)

    def register_session_sub(self, target_harness, target_id,
                             from_harness=None, from_id=None,
                             job=None, timeout=None, is_callback=False,
                             request_id=None, target_turn_id=None,
                             observe_current=True):
        """Create a subscription for an external session turn.

        When observe_current=True (default for manual subs), binds to the
        currently active turn from saved native transcripts, or to the
        running managed job for this session. Rejects idle sessions with no
        active managed job.

        When observe_current=False (send path), creates the sub without
        binding -- the observer will bind the turn once the marker is seen.
        """
        if timeout is not None and (timeout < 0 or not math.isfinite(timeout)):
            raise UsageError("subscription timeout must be finite and non-negative")
        if observe_current and not target_turn_id and not job:
            target_address = "%s:%s" % (target_harness, target_id)
            running = [r for r in store.list_jobs() if r.get("harness") == target_harness
                       and r.get("native_session_id") == target_id and r.get("status") == store.RUNNING]
            if running:
                job = running[0]["job"]
            else:
                from . import transcripts
                current = transcripts.snapshot(target_harness, target_id)
                if not current.get("supported"):
                    from .errors import UnsupportedOperationError
                    raise UnsupportedOperationError(current.get("reason") or "transcript completion polling is unsupported")
                if not current.get("active_turn"):
                    raise UsageError("session %s has no known active turn to subscribe to" % target_address,
                                     hint="send a tracked request or subscribe while a native turn is active")
                target_turn_id = current["active_turn"]

        callback_info = None
        if from_harness and from_id:
            from .sender import preflight_callback
            callback_info = preflight_callback(from_harness, from_id)
        record = sessions.create(
            target_harness=target_harness,
            target_id=target_id,
            from_harness=from_harness,
            from_id=from_id,
            job=job,
            timeout=timeout,
            is_callback=is_callback,
            request_id=request_id,
            target_turn_id=target_turn_id,
        )

        self._set_callback_availability(record["sub"], callback_info)
        if observe_current and not job:
            sessions.update(record["sub"], observer_kind="transcript", observation_error=None)
        record = sessions.read(record["sub"])
        # Schedule expiry if timeout is set
        if timeout is not None and record.get("expires_at"):
            self._schedule_sub_timeout(record["sub"], record["expires_at"])

        return record

    def list_subs_for_target(self, target_address):
        """Return all subscriptions for a session address."""
        return sessions.list_for_target(target_address)

    # ---- delivery ------------------------------------------------------

    def _deliver_all(self, job_id):
        record = store.read(job_id)
        if not store.is_terminal(record):
            return
        # Deliver subscriptions tied to this job.
        # record_result stores without marking delivered so deliver_sub_callback
        # can still find status==PENDING and run the callback.
        result_text = store.read_result(job_id)
        for sub_record in sessions.list_for_job(job_id):
            if sub_record.get("status") == sessions.PENDING:
                native = record.get("native_session_id")
                if native:
                    sessions.update(sub_record["sub"], target_id=native,
                                    target_address="%s:%s" % (record["harness"], native),
                                    completion_status=record["status"])
                sessions.record_result(sub_record["sub"], result_text)
                self.deliver_sub_callback(sub_record["sub"])
        # Deliver asyncwait records (legacy/existing mechanism)
        for wait in record.get("waits", []):
            event = wait.get("delivery", {}).get("event")
            if event == "timeout":
                message = envelope.build(
                    record, store.read_result(job_id), event=event, wait=wait)
            else:
                message = envelope.build(record, store.read_result(job_id))
            self._deliver_one(job_id, wait["wait"], record, message,
                              event=event)

    def _deliver_one(self, job_id, wait_id, record, message, event=None):
        """Deliver one wait, at most once, ever."""
        notification = None
        with self.lock:
            current = store.read(job_id)
            wait = _find_wait(current, wait_id)
            if wait is None:
                return
            delivery = wait["delivery"]
            stored_event = delivery.get("event")
            # Completion and timeout can race. Once one path has claimed the
            # wait, the other event must not turn it into a second delivery.
            if stored_event and event and stored_event != event:
                return
            event = stored_event or event
            if event is None:
                event = current["status"]
            expected = ids.notification_id(wait_id, job_id, event)
            if delivery["state"] in (store.DELIVERY_INFLIGHT, store.DELIVERY_UNKNOWN,
                                      store.DELIVERED, store.DELIVERY_FAILED):
                return
            if delivery["state"] == store.DELIVERED and \
                    delivery.get("notification_id") == expected:
                return
            if delivery["state"] == store.DELIVERY_FAILED and \
                    delivery.get("notification_id") == expected:
                # A failed delivery stays failed. Retrying could duplicate a
                # follow-up that in fact arrived.
                return
            delivery_key = (job_id, wait_id)
            if delivery_key in self.delivering:
                return
            self.delivering.add(delivery_key)
            # Claim this delivery before leaving the lock.
            store.set_delivery(job_id, wait_id, store.DELIVERY_INFLIGHT,
                               notification_id=expected, event=event)
            notification = (wait["notify_harness"], wait["notify_id"], expected)

        harness, target_id, notification_id = notification
        try:
            try:
                adapter = notify.get(harness)
                adapter.validate(target_id)
                transport_result = adapter.deliver(target_id, message, notification_id)
            except AgentctlError as exc:
                log("delivery failed for %s -> %s:%s: %s"
                    % (job_id, harness, target_id, exc.message))
                store.set_delivery(job_id, wait_id,
                                   store.DELIVERY_UNKNOWN if getattr(exc, "acceptance_unknown", False) else store.DELIVERY_FAILED,
                                   notification_id=notification_id,
                                   error=exc.message, event=event)
                return
            except Exception as exc:
                log("delivery crashed for %s: %s"
                    % (job_id, traceback.format_exc()))
                store.set_delivery(
                    job_id, wait_id, store.DELIVERY_UNKNOWN,
                    notification_id=notification_id,
                    error="unexpected delivery error: %s" % exc,
                    event=event)
                return
            store.set_delivery(job_id, wait_id, store.DELIVERED,
                               notification_id=notification_id, event=event, result=transport_result)
            log("delivered %s for %s to %s:%s"
                % (notification_id, job_id, harness, target_id))
        finally:
            with self.lock:
                self.delivering.discard(delivery_key)

    def register_wait(self, job_id, raw_target, timeout=None):
        """Register a durable wait, then deliver at once if already terminal.

        The target is checked for reachability before the wait is stored. A
        target that cannot receive a delivery fails here, while the calling
        agent is still watching, instead of silently much later.
        """
        if timeout is not None and (timeout < 0 or not math.isfinite(timeout)):
            raise UsageError("asyncwait timeout must be a finite, non-negative "
                             "number of seconds")
        harness, target_id = notify.parse_target(raw_target)
        record = store.read(job_id)
        notify.get(harness).preflight(target_id)
        wait = store.add_wait(job_id, harness, target_id, raw_target, timeout)
        if store.is_terminal(record):
            self._deliver_all(job_id)
        else:
            self._resume_if_needed(job_id)
            current = store.read(job_id)
            if not store.is_terminal(current):
                self._schedule_wait_timeout(job_id, wait["wait"],
                                            wait.get("expires_at"))
        return _find_wait(store.read(job_id), wait["wait"])

    def _schedule_wait_timeout(self, job_id, wait_id, expires_at):
        if expires_at is None:
            return
        delay = max(0, expires_at - time.time())
        timer = threading.Timer(delay, self._timeout_wait,
                                args=(job_id, wait_id))
        timer.daemon = True
        self.timers.append(timer)
        timer.start()

    def _timeout_wait(self, job_id, wait_id):
        """Notify a timed wait even when its child is not terminal yet."""
        record = store.read(job_id)
        wait = _find_wait(record, wait_id)
        if wait is None or wait["delivery"]["state"] != store.DELIVERY_PENDING:
            return
        expires_at = wait.get("expires_at")
        if expires_at is None:
            return
        remaining = expires_at - time.time()
        if remaining > 0:
            self._schedule_wait_timeout(job_id, wait_id, expires_at)
            return
        if store.is_terminal(record):
            self._deliver_all(job_id)
            return
        message = envelope.build(record, store.read_result(job_id),
                                 event="timeout", wait=wait)
        self._deliver_one(job_id, wait_id, record, message, event="timeout")

    def _schedule_sub_timeout(self, sub_id, expires_at):
        """Schedule expiry for a subscription with a timeout."""
        delay = max(0, expires_at - time.time())
        timer = threading.Timer(delay, self._expire_sub, args=(sub_id,))
        timer.daemon = True
        self.timers.append(timer)
        timer.start()

    def _expire_sub(self, sub_id):
        """Mark a subscription as expired once its timeout is reached."""
        try:
            record = sessions.read(sub_id)
        except Exception:
            return
        if record.get("status") != sessions.PENDING:
            return
        expires_at = record.get("expires_at")
        if expires_at and time.time() < expires_at:
            # Timer fired early (e.g. clock skew); reschedule
            self._schedule_sub_timeout(sub_id, expires_at)
            return
        sessions.update(sub_id, status=sessions.EXPIRED, observation_error="subscription deadline elapsed")
        log("sub %s expired" % sub_id)

    # ---- startup recovery ----------------------------------------------

    def recover(self):
        """Re-adopt or finalise every job left behind by a previous daemon."""
        # Mark any in-flight subscriptions as UNKNOWN (crash recovery)
        sessions.recover_inflight_to_unknown()

        # Re-start observers for pending send-path subscriptions
        for sub_record in sessions.list_all():
            if sub_record.get("status") != sessions.PENDING:
                continue
            expires_at = sub_record.get("expires_at")
            if expires_at is not None:
                if expires_at <= time.time():
                    self._expire_sub(sub_record["sub"])
                    continue
                self._schedule_sub_timeout(sub_record["sub"], expires_at)
            if sub_record.get("result_stored_at") is not None:
                self.deliver_sub_callback(sub_record["sub"])
                continue
            if not sub_record.get("job") and not sub_record.get("dispatch_status") and sub_record.get("target_turn_id"):
                sessions.update(sub_record["sub"], observer_kind="transcript")
            if sub_record.get("dispatch_status") not in ("accepted", "unknown"):
                continue

            target_harness = sub_record.get("target_harness")
            target_id = sub_record.get("target_id")
            request_id = sub_record.get("request_id")

            if target_harness == "codex" and request_id:
                transcript_path = sub_record.get("observer_transcript_path")
                cursor = sub_record.get("observer_cursor") or 0
                if transcript_path:
                    self._start_codex_observer(
                        sub_record["sub"], target_id, request_id,
                        transcript_path, start_cursor=cursor)

            elif target_harness == "opencode":
                msg_id = sub_record.get("message_id")
                if msg_id:
                    self._start_opencode_observer(
                        sub_record["sub"], target_id, msg_id)


        # Recover tracked jobs
        for record in store.list_jobs():
            for wait in record.get("waits", []):
                delivery = wait.get("delivery", {})
                if delivery.get("state") == store.DELIVERY_INFLIGHT or (
                        delivery.get("state") == store.DELIVERY_PENDING and delivery.get("notification_id")):
                    store.set_delivery(record["job"], wait["wait"], store.DELIVERY_UNKNOWN,
                                       error="daemon stopped during callback; acceptance unknown")
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
            current = store.read(record["job"])
            if not store.is_terminal(current):
                for wait in current.get("waits", []):
                    if wait["delivery"]["state"] == store.DELIVERY_PENDING:
                        self._schedule_wait_timeout(
                            current["job"], wait["wait"],
                            wait.get("expires_at"))

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


def _read_transcript_from(path, cursor):
    """Read JSONL events from path starting at byte offset cursor.

    Returns (new_cursor, events) where new_cursor is the new file position.
    Uses binary mode for accurate byte-level seeking.
    """
    events = []
    new_cursor = cursor
    try:
        with open(path, "rb") as f:
            f.seek(cursor)
            while True:
                line = f.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    # A writer may not have finished its JSON record yet.
                    break
                new_cursor = f.tell()
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    event = json.loads(stripped.decode("utf-8", "replace"))
                    if isinstance(event, dict):
                        events.append(event)
                except ValueError:
                    continue
    except IOError:
        pass
    return new_cursor, events


def _read_result_from_transcript(path):
    """Read the last task_complete.last_agent_message from a transcript."""
    result = ""
    if not path:
        return result
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                t = event.get("type")
                payload = (event.get("payload")
                           if isinstance(event.get("payload"), dict) else {})
                if t == "assistant":
                    content = event.get("message", {}).get("content", [])
                    result = "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
                if t == "event_msg" and payload.get("type") == "task_complete":
                    msg = payload.get("last_agent_message")
                    if isinstance(msg, str) and msg.strip():
                        result = msg.strip()
    except IOError:
        pass
    return result


def _build_sub_envelope(sub_record, result_text=""):
    """Build a callback envelope for a subscription completion."""
    lines = [
        "[agentctl completion]",
        "sub: %s" % sub_record.get("sub"),
        "target: %s" % sub_record.get("target_address"),
        "status: %s" % (sub_record.get("completion_status") or "completed"),
    ]
    if sub_record.get("job"):
        lines.append("job: %s" % sub_record["job"])
    text = (result_text or "").strip()
    if text:
        single = " ".join(text.split())
        lines.append("result: %s" % (single[:600] + " [truncated]"
                                     if len(single) > 600 else single))
    else:
        lines.append("result: (no result recorded)")
    lines.append("full: agentctl sub read %s" % sub_record.get("sub"))
    return "\n".join(lines)
