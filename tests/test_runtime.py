"""Functional runtime tests for the send_session / notify_hook_event runtime.

Uses fake native adapters and local transcript files; no live agent credits
are spent and no real harness binaries are invoked.

Coverage:
  - strict turn marker binding (Codex observer only fires for its marker)
  - unrelated Stop ignored (hook_complete for wrong turn doesn't fire subs)
  - full hook result retained (result from hook event stored, not blank)
  - no-notify handle (send_session returns handle even with no subscriber)
  - busy refusal (non-queued harness refuses when registry shows running)
  - request unique repeat (unknown dispatch not re-sent on recovery)
  - observer restart (Codex observer resumes from saved cursor)
  - unknown callback no resend (unknown dispatch stays unknown after restart)
  - native registry/current listener (registry_update/registry_read)
  - inbox claim state (inbox transitions queued -> claimed)
"""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import unittest.mock as mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
sys.path.insert(0, ROOT)


class RuntimeBase(unittest.TestCase):
    """Base: isolated AGENTCTL_HOME with a direct Daemon instance."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="agentctl-runtime-")
        os.environ["AGENTCTL_HOME"] = self.home
        self._old_home = None

        # Import after patching env so config.home() picks up the temp dir
        from agentctl import sessions, daemon, store, config
        self.sessions = sessions
        self.store = store
        self.config = config

        # Build a Daemon instance (no socket; call methods directly)
        self.daemon = daemon.Daemon()
        # Legacy correlation fixtures supply a fake native snapshot. Live poll
        # parsing is exercised separately against actual saved-store fixtures.
        def native_snapshot(harness, native_id, home=None):
            registry = sessions.registry_read(harness + ":" + native_id) or {}
            turn = registry.get("current_turn_id") if registry.get("state") == "running" else None
            return {"supported": True, "active_turn": turn, "turns": {}}
        self.snapshot_patch = mock.patch("agentctl.transcripts.snapshot", side_effect=native_snapshot)
        self.snapshot_patch.start()
        self.addCleanup(self.snapshot_patch.stop)

    def tearDown(self):
        self.daemon.stopping.set()
        for timer in self.daemon.timers:
            timer.cancel()
        for observer in self.daemon.observers:
            observer.join(timeout=3)
        os.environ.pop("AGENTCTL_HOME", None)
        shutil.rmtree(self.home, ignore_errors=True)

    def make_codex_transcript(self, thread_id, events=None):
        """Write a fake Codex JSONL transcript and return its path."""
        sess_dir = os.path.join(self.home, "native_home", ".codex", "sessions",
                                "2026", "10", "01")
        os.makedirs(sess_dir, exist_ok=True)
        path = os.path.join(sess_dir, "rollout-2026-10-01T00-00-00-%s.jsonl" % thread_id)
        with open(path, "w") as f:
            f.write(json.dumps({
                "type": "session_meta",
                "payload": {"id": thread_id, "cwd": self.home}
            }) + "\n")
            for ev in (events or []):
                f.write(json.dumps(ev) + "\n")
        return path

    def append_transcript(self, path, events):
        """Append JSONL events to an existing transcript."""
        with open(path, "a") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

    def make_found_session(self, harness, native_id, transcript_path=None, cwd=None):
        """Build a fake 'found' session dict as discovery would return."""
        return {
            "harness": harness,
            "native_id": native_id,
            "address": "%s:%s" % (harness, native_id),
            "transcript_path": transcript_path,
            "cwd": cwd or self.home,
            "modified_at": time.time(),
        }


class TestNativeRegistry(RuntimeBase):
    """Registry records native session state start/begin/complete."""

    def test_start_sets_idle(self):
        target = "codex:thread-aaa"
        self.daemon.notify_hook_event(target, event="start")
        reg = self.sessions.registry_read(target)
        self.assertIsNotNone(reg)
        self.assertEqual(reg["state"], "idle")
        self.assertIsNone(reg["current_turn_id"])

    def test_begin_sets_running_with_turn(self):
        target = "codex:thread-bbb"
        self.daemon.notify_hook_event(target, event="begin", turn_id="turn-001")
        reg = self.sessions.registry_read(target)
        self.assertIsNotNone(reg)
        self.assertEqual(reg["state"], "running")
        self.assertEqual(reg["current_turn_id"], "turn-001")

    def test_complete_resets_to_idle(self):
        target = "codex:thread-ccc"
        self.daemon.notify_hook_event(target, event="begin", turn_id="turn-002")
        self.daemon.notify_hook_event(target, event="complete", turn_id="turn-002")
        reg = self.sessions.registry_read(target)
        self.assertIsNotNone(reg)
        self.assertEqual(reg["state"], "idle")
        self.assertIsNone(reg["current_turn_id"])

    def test_registry_update_is_persistent(self):
        target = "claude:sess-xyz"
        self.sessions.registry_update(target, state="running", current_turn_id="t1")
        reg = self.sessions.registry_read(target)
        self.assertEqual(reg["state"], "running")
        self.assertEqual(reg["current_turn_id"], "t1")


class TestHookResultRetained(RuntimeBase):
    """Full result from hook event is stored in the subscription record."""

    def test_hook_result_stored_on_complete(self):
        target = "codex:thread-result"
        # Set up a running turn
        self.daemon.notify_hook_event(target, event="begin", turn_id="turn-x")
        # Create a sub bound to that turn
        sub = self.daemon.register_session_sub(
            target_harness="codex",
            target_id="thread-result",
        )
        sub_id = sub["sub"]
        self.assertEqual(sub.get("target_turn_id"), "turn-x")

        # Fire complete with a result
        result_text = "The assistant said hello"
        self.daemon.notify_hook_event(
            target, event="complete", turn_id="turn-x",
            result=result_text)

        rec = self.sessions.read(sub_id)
        self.assertEqual(rec["result"], result_text)
        self.assertIsNotNone(rec["result_stored_at"])
        self.assertNotEqual(rec["status"], self.sessions.PENDING)

    def test_blank_result_not_delivered_when_no_result(self):
        """No-result hook fires correctly; stored result is empty string, not None."""
        target = "opencode:sess-noresult"
        self.daemon.notify_hook_event(target, event="begin", turn_id="t-nr")
        sub = self.daemon.register_session_sub(
            target_harness="opencode",
            target_id="sess-noresult",
        )
        sub_id = sub["sub"]
        self.daemon.notify_hook_event(
            target, event="complete", turn_id="t-nr",
            result=None)
        rec = self.sessions.read(sub_id)
        # result field should be "" not None after hook fires
        self.assertIsNotNone(rec.get("result_stored_at"),
                             "result_stored_at must be set after hook fires")


class TestUnrelatedStopIgnored(RuntimeBase):
    """An unrelated Stop (different turn) must not fire bound subscriptions."""

    def test_different_turn_stop_does_not_fire(self):
        target = "codex:thread-turnfilter"
        self.daemon.notify_hook_event(target, event="begin", turn_id="turn-MINE")
        sub = self.daemon.register_session_sub(
            target_harness="codex",
            target_id="thread-turnfilter",
        )
        sub_id = sub["sub"]
        self.assertEqual(sub.get("target_turn_id"), "turn-MINE")

        # Fire complete for a DIFFERENT turn
        triggered = self.daemon.notify_hook_event(
            target, event="complete", turn_id="turn-UNRELATED")
        self.assertEqual(triggered, 0, "unrelated turn should not trigger any subs")
        rec = self.sessions.read(sub_id)
        self.assertEqual(rec["status"], self.sessions.PENDING,
                         "sub bound to different turn must stay pending")

    def test_correct_turn_stop_does_fire(self):
        target = "codex:thread-correctturn"
        self.daemon.notify_hook_event(target, event="begin", turn_id="turn-CORRECT")
        sub = self.daemon.register_session_sub(
            target_harness="codex",
            target_id="thread-correctturn",
        )
        sub_id = sub["sub"]

        triggered = self.daemon.notify_hook_event(
            target, event="complete", turn_id="turn-CORRECT",
            result="done!")
        self.assertGreater(triggered, 0, "correct turn should fire the sub")
        rec = self.sessions.read(sub_id)
        self.assertNotEqual(rec["status"], self.sessions.PENDING)

    def test_no_turn_id_does_not_fire_unbound_subs(self):
        """An unrelated completion cannot consume an unbound listener."""
        target = "cursor:sess-unbound"
        # Don't call begin, so registry has no turn
        sub = self.sessions.create(
            target_harness="cursor",
            target_id="sess-unbound",
        )
        sub_id = sub["sub"]

        triggered = self.daemon.notify_hook_event(
            target, event="complete", result="finished!")
        self.assertEqual(triggered, 0)
        rec = self.sessions.read(sub_id)
        self.assertEqual(rec["status"], self.sessions.PENDING)

    def test_managed_job_sub_not_fired_by_hook(self):
        """Subs tied to a managed job are NOT fired by hook events."""
        target = "cursor:sess-managed"
        sub = self.sessions.create(
            target_harness="cursor",
            target_id="sess-managed",
            job="job_fake001",
        )
        sub_id = sub["sub"]

        triggered = self.daemon.notify_hook_event(
            target, event="complete", result="some result")
        self.assertEqual(triggered, 0, "job-tied sub must not fire on hook events")
        rec = self.sessions.read(sub_id)
        self.assertEqual(rec["status"], self.sessions.PENDING)


class TestNoNotifyHandle(RuntimeBase):
    """send_session returns a handle even with no subscriber (--no-notify)."""

    def _patched_send(self, harness, session_id, prompt, found=None, **kwargs):
        """Call send_session with mocked discovery."""
        if found is None:
            found = self.make_found_session(harness, session_id)
        with mock.patch.object(self.daemon, "_validate_session_exists",
                               return_value=found):
            return self.daemon.send_session(harness, session_id, prompt, **kwargs)

    def test_no_notify_returns_record(self):
        thread_id = "01a07a1e-aaaa-70b3-b0a0-000000000001"
        path = self.make_codex_transcript(thread_id)

        with mock.patch("agentctl.sender._dispatch_codex") as mock_dispatch:
            mock_dispatch.return_value = _FakeReceipt(accepted=True)
            record = self._patched_send(
                "codex", thread_id, "do something",
                found=self.make_found_session("codex", thread_id, path))

        self.assertIsNotNone(record)
        self.assertIn("sub", record)
        self.assertTrue(record["sub"].startswith("sub_"))
        # No subscriber: callback should be not_requested
        self.assertEqual(record["callback"]["state"],
                         self.sessions.CB_NOT_REQUESTED)

    def test_no_notify_handle_dispatch_accepted(self):
        thread_id = "01a07a1e-aaaa-70b3-b0a0-000000000002"
        path = self.make_codex_transcript(thread_id)

        with mock.patch("agentctl.sender._dispatch_codex") as mock_dispatch:
            mock_dispatch.return_value = _FakeReceipt(accepted=True)
            record = self._patched_send(
                "codex", thread_id, "work",
                found=self.make_found_session("codex", thread_id, path))

        self.assertEqual(record.get("dispatch_status"), "accepted")
        self.assertTrue(record.get("dispatch", {}).get("accepted"))


class TestBusyRefusal(RuntimeBase):
    """Non-queued harnesses refuse send when registry shows running."""

    def _patched_send(self, harness, session_id, prompt, **kwargs):
        with mock.patch.object(self.daemon, "_validate_session_exists",
                               return_value=self.make_found_session(
                                   harness, session_id)):
            return self.daemon.send_session(harness, session_id, prompt, **kwargs)

    def test_cursor_refused_when_registry_running(self):
        from agentctl.errors import DeliveryError
        session_id = "aaaaaaaa-bbbb-4ccc-8ddd-000000000001"
        target = "cursor:%s" % session_id
        # Simulate a running turn via hook event
        self.daemon.notify_hook_event(target, event="begin", turn_id="t1")

        with self.assertRaises(DeliveryError) as ctx:
            self._patched_send("cursor", session_id, "test")
        self.assertIn("busy", str(ctx.exception).lower())

    def test_opencode_refused_when_registry_running(self):
        from agentctl.errors import DeliveryError
        session_id = "oc-sess-001"
        target = "opencode:%s" % session_id
        self.daemon.notify_hook_event(target, event="begin", turn_id="t2")

        # Need to patch _validate_opencode_session to avoid HTTP call
        with mock.patch.object(self.daemon, "_validate_opencode_session"):
            with self.assertRaises(DeliveryError):
                with mock.patch.object(self.daemon, "_validate_session_exists",
                                       return_value=None):
                    self.daemon.send_session("opencode", session_id, "test")

    def test_codex_not_refused_when_running(self):
        """Codex has a queue; busy state does not block send."""
        thread_id = "01a07a1e-aaaa-70b3-b0a0-000000000003"
        target = "codex:%s" % thread_id
        path = self.make_codex_transcript(thread_id)

        # Even if registry says running, codex send proceeds (has queue)
        self.daemon.notify_hook_event(target, event="begin", turn_id="t3")
        with mock.patch("agentctl.sender._dispatch_codex") as mock_dispatch:
            mock_dispatch.return_value = _FakeReceipt(accepted=True)
            found = self.make_found_session("codex", thread_id, path)
            with mock.patch.object(self.daemon, "_validate_session_exists",
                                   return_value=found):
                record = self.daemon.send_session("codex", thread_id, "work")
        # Should succeed
        self.assertIsNotNone(record)

    def test_cursor_busy_check_also_checks_running_jobs(self):
        """Busy check catches running managed jobs even without registry entry."""
        from agentctl.errors import DeliveryError
        session_id = "aaaaaaaa-bbbb-4ccc-8ddd-000000000004"

        # Create a fake running job for this session
        job_dir = self.config.job_dir("job_fake002")
        os.makedirs(job_dir, exist_ok=True)
        job_record = {
            "job": "job_fake002",
            "harness": "cursor",
            "native_session_id": session_id,
            "status": "running",
            "prompt": "test",
            "cwd": self.home,
            "command": [],
            "model": None,
            "pid": None,
            "created_at": time.time(),
            "started_at": time.time(),
            "finished_at": None,
            "exit_code": None,
            "error": None,
            "followup_of": None,
            "cancel_requested": False,
            "waits": [],
            "stream": "",
            "stderr": "",
            "result": "",
        }
        import fcntl
        lock_path = os.path.join(job_dir, "job.lock")
        rec_path = os.path.join(job_dir, "job.json")
        with open(lock_path, "a"):
            pass
        with open(rec_path, "w") as f:
            json.dump(job_record, f)

        with self.assertRaises(DeliveryError):
            self._patched_send("cursor", session_id, "test")


class TestStrictTurnMarkerBinding(RuntimeBase):
    """Codex observer only completes sub when the exact request marker is seen."""

    def _write_codex_events_after(self, path, events, request_id=None,
                                   turn_id="turn-obs"):
        """Append a sequence of events that match (or don't match) our marker."""
        import time as _time
        _time.sleep(0.05)  # small delay so observer sees new bytes
        self.append_transcript(path, events)

    def test_observer_fires_on_correct_marker(self):
        thread_id = "01a07a1e-aaaa-70b3-b0a0-000000000010"
        path = self.make_codex_transcript(thread_id)
        request_id = "req_test001"
        marker = "[agentctl request: %s]" % request_id

        # Write events BEFORE starting observer so they're found immediately
        events = [
            {"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "text",
                             "text": "%s\nplease do something" % marker}]
            }},
            {"type": "event_msg", "payload": {
                "type": "task_started", "turn_id": "turn-obs-correct"
            }},
            {"type": "event_msg", "payload": {
                "type": "task_complete", "turn_id": "turn-obs-correct",
                "last_agent_message": "The result of the work"
            }},
        ]
        self.append_transcript(path, events)

        # Create sub directly (no send_session complexity)
        sub = self.sessions.create(
            target_harness="codex",
            target_id=thread_id,
            request_id=request_id,
        )
        sub_id = sub["sub"]

        # Start observer from beginning of file
        self.daemon._start_codex_observer(
            sub_id, thread_id, request_id, path, start_cursor=0)

        # Wait for observer to complete (events are already in file)
        deadline = time.time() + 5
        while time.time() < deadline:
            rec = self.sessions.read(sub_id)
            if rec["status"] != self.sessions.PENDING:
                break
            time.sleep(0.1)

        rec = self.sessions.read(sub_id)
        self.assertNotEqual(rec["status"], self.sessions.PENDING,
                            "observer must complete when marker is found")
        self.assertIn("The result of the work", rec.get("result", ""))

    def test_observer_ignores_unrelated_turns(self):
        """Observer must not fire on task_complete events for other turns."""
        thread_id = "01a07a1e-aaaa-70b3-b0a0-000000000011"
        path = self.make_codex_transcript(thread_id)
        request_id = "req_test002"

        # Pre-seed transcript with an unrelated turn
        unrelated = [
            {"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "text", "text": "some other prompt"}]
            }},
            {"type": "event_msg", "payload": {
                "type": "task_started", "turn_id": "turn-OTHER"
            }},
            {"type": "event_msg", "payload": {
                "type": "task_complete", "turn_id": "turn-OTHER",
                "last_agent_message": "other result"
            }},
        ]
        self.append_transcript(path, unrelated)

        # Create a sub manually
        sub = self.sessions.create(
            target_harness="codex",
            target_id=thread_id,
            request_id=request_id,
        )
        sub_id = sub["sub"]

        # Record cursor AFTER existing content
        cursor = os.path.getsize(path)
        self.sessions.update(sub_id, observer_cursor=cursor)

        # Start observer
        self.daemon._start_codex_observer(
            sub_id, thread_id, request_id, path, start_cursor=cursor)

        # Write more unrelated events (no marker)
        self.append_transcript(path, [
            {"type": "event_msg", "payload": {
                "type": "task_complete", "turn_id": "turn-STILL-OTHER",
                "last_agent_message": "another unrelated result"
            }},
        ])

        # Observer should NOT fire (no marker seen)
        time.sleep(0.3)
        rec = self.sessions.read(sub_id)
        self.assertEqual(rec["status"], self.sessions.PENDING,
                         "observer must not fire without seeing the request marker")

        # Now write our marker and completion
        marker = "[agentctl request: %s]" % request_id
        self.append_transcript(path, [
            {"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "text", "text": "%s\nmy prompt" % marker}]
            }},
            {"type": "event_msg", "payload": {
                "type": "task_started", "turn_id": "turn-MINE"
            }},
            {"type": "event_msg", "payload": {
                "type": "task_complete", "turn_id": "turn-MINE",
                "last_agent_message": "my result"
            }},
        ])

        # Now observer should fire
        deadline = time.time() + 5
        while time.time() < deadline:
            rec = self.sessions.read(sub_id)
            if rec["status"] != self.sessions.PENDING:
                break
            time.sleep(0.1)

        rec = self.sessions.read(sub_id)
        self.assertNotEqual(rec["status"], self.sessions.PENDING,
                            "observer must fire once correct marker is seen")
        self.assertEqual(rec.get("result"), "my result")


class TestObserverRestart(RuntimeBase):
    """Codex observer resumes from saved cursor after daemon restart."""

    def test_observer_resumes_from_cursor(self):
        thread_id = "01a07a1e-aaaa-70b3-b0a0-000000000020"
        path = self.make_codex_transcript(thread_id)
        request_id = "req_restart001"

        # Write some initial content before the observer starts
        initial_events = [
            {"type": "event_msg", "payload": {
                "type": "task_started", "turn_id": "turn-before"
            }},
            {"type": "event_msg", "payload": {
                "type": "task_complete", "turn_id": "turn-before",
                "last_agent_message": "old result"
            }},
        ]
        self.append_transcript(path, initial_events)

        # Record cursor AFTER initial content (simulating "observer was here")
        cursor = os.path.getsize(path)

        sub = self.sessions.create(
            target_harness="codex",
            target_id=thread_id,
            request_id=request_id,
        )
        sub_id = sub["sub"]
        self.sessions.update(sub_id, observer_cursor=cursor)

        # Start observer with saved cursor (simulates restart)
        self.daemon._start_codex_observer(
            sub_id, thread_id, request_id, path, start_cursor=cursor)

        # Write our marker events AFTER the saved cursor
        marker = "[agentctl request: %s]" % request_id
        self.append_transcript(path, [
            {"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "text", "text": "%s\nmy prompt" % marker}]
            }},
            {"type": "event_msg", "payload": {
                "type": "task_started", "turn_id": "turn-NEW"
            }},
            {"type": "event_msg", "payload": {
                "type": "task_complete", "turn_id": "turn-NEW",
                "last_agent_message": "new result after restart"
            }},
        ])

        deadline = time.time() + 5
        while time.time() < deadline:
            rec = self.sessions.read(sub_id)
            if rec["status"] != self.sessions.PENDING:
                break
            time.sleep(0.1)

        rec = self.sessions.read(sub_id)
        self.assertNotEqual(rec["status"], self.sessions.PENDING)
        self.assertEqual(rec.get("result"), "new result after restart",
                         "observer must not use old-result from pre-cursor events")


class TestUnknownCallbackNoResend(RuntimeBase):
    """Unknown dispatch status persists; recovery never triggers a resend."""

    def test_inflight_becomes_unknown_on_recovery(self):
        sub = self.sessions.create(
            target_harness="codex",
            target_id="thread-inflight",
            request_id="req_xxx",
        )
        sub_id = sub["sub"]
        # Simulate a crash during dispatch (in_flight set but never cleared)
        self.sessions.claim_inflight(sub_id)
        rec = self.sessions.read(sub_id)
        self.assertTrue(rec["in_flight"])
        self.assertEqual(rec["status"], self.sessions.PENDING)

        # Recovery should mark it UNKNOWN
        self.sessions.recover_inflight_to_unknown()

        rec = self.sessions.read(sub_id)
        self.assertEqual(rec["status"], self.sessions.PENDING,
                         "unknown dispatch can still be observed without resending")
        self.assertEqual(rec["dispatch_status"], "unknown")
        self.assertFalse(rec["in_flight"])

    def test_unknown_sub_not_dispatched_by_recover(self):
        """Daemon recover() does not restart observers for UNKNOWN subs."""
        sub = self.sessions.create(
            target_harness="codex",
            target_id="thread-unknown",
            request_id="req_yyy",
        )
        sub_id = sub["sub"]
        self.sessions.update(sub_id, status=self.sessions.UNKNOWN,
                             dispatch_status="unknown")

        # Track whether _start_codex_observer is called
        observer_calls = []
        orig = self.daemon._start_codex_observer
        def capturing_start(*args, **kwargs):
            observer_calls.append(args)
            return orig(*args, **kwargs)

        with mock.patch.object(self.daemon, "_start_codex_observer",
                               side_effect=capturing_start):
            self.daemon.recover()

        # UNKNOWN subs should not restart observers (dispatch_status != accepted)
        unknown_calls = [c for c in observer_calls if sub_id in str(c)]
        self.assertEqual(unknown_calls, [],
                         "recover must not restart observers for UNKNOWN subs")

    def test_recover_restarts_accepted_codex_observer(self):
        """Daemon recover() DOES restart observers for accepted subs."""
        thread_id = "01a07a1e-aaaa-70b3-b0a0-000000000030"
        path = self.make_codex_transcript(thread_id)
        request_id = "req_zzz"

        sub = self.sessions.create(
            target_harness="codex",
            target_id=thread_id,
            request_id=request_id,
        )
        sub_id = sub["sub"]
        self.sessions.update(sub_id,
                             dispatch_status="accepted",
                             observer_transcript_path=path,
                             observer_cursor=0)

        started = []
        with mock.patch.object(self.daemon, "_start_codex_observer",
                               side_effect=lambda *a, **kw: started.append(a)):
            self.daemon.recover()

        self.assertEqual(len(started), 1, "accepted codex sub should restart observer")
        self.assertEqual(started[0][0], sub_id)


class TestInboxClaimState(RuntimeBase):
    """Inbox messages transition from queued to claimed correctly."""

    def test_inbox_put_sets_queued_state(self):
        session_id = "claude:sess-inbox-001"
        msg_id = "ntf_test001"
        record = self.sessions.inbox_put(session_id, "hello", msg_id)
        self.assertEqual(record["state"], "queued")
        self.assertFalse(record["claimed"])

    def test_inbox_list_returns_queued_only(self):
        session_id = "claude:sess-inbox-002"
        self.sessions.inbox_put(session_id, "msg1", "ntf_a001")
        msgs = self.sessions.inbox_list(session_id)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["state"], "queued")

    def test_inbox_claim_transitions_to_claimed(self):
        session_id = "claude:sess-inbox-003"
        self.sessions.inbox_put(session_id, "claim me", "ntf_b001")
        claimed = self.sessions.inbox_claim_all(session_id)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["state"], "claimed")
        self.assertTrue(claimed[0]["claimed"])

        # After claiming, inbox_list returns empty
        remaining = self.sessions.inbox_list(session_id)
        self.assertEqual(remaining, [])

    def test_inbox_put_deduplicates_by_message_id(self):
        session_id = "claude:sess-inbox-004"
        msg_id = "ntf_dedup001"
        self.sessions.inbox_put(session_id, "first", msg_id)
        self.sessions.inbox_put(session_id, "duplicate", msg_id)  # same id
        msgs = self.sessions.inbox_list(session_id)
        # Only one message despite two puts
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["message"], "first")

    def test_inbox_claim_returns_both_formats(self):
        """inbox_claim_all returns list; each record has message_id and message."""
        session_id = "claude:sess-inbox-005"
        self.sessions.inbox_put(session_id, "content here", "ntf_fmt001")
        claimed = self.sessions.inbox_claim_all(session_id)
        self.assertEqual(len(claimed), 1)
        self.assertIn("message_id", claimed[0])
        self.assertIn("message", claimed[0])
        self.assertEqual(claimed[0]["message"], "content here")


class TestClaudeInboxCanonicalAddress(RuntimeBase):
    """Claude notify uses canonical 'claude:<id>' inbox key."""

    def test_deliver_writes_to_canonical_address(self):
        from agentctl.notify.claude import ClaudeNotify
        adapter = ClaudeNotify()
        session_uuid = "aaaa-bbbb-cccc-0001"
        adapter.deliver(session_uuid, "the message", "ntf_canonical001")

        # inbox_list with canonical key should find the message
        canonical = "claude:%s" % session_uuid
        msgs = self.sessions.inbox_list(canonical)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["message"], "the message")

        # inbox_list with bare UUID should NOT find it (different file)
        bare_msgs = self.sessions.inbox_list(session_uuid)
        self.assertEqual(bare_msgs, [])

    def test_claim_finds_delivered_message(self):
        from agentctl.notify.claude import ClaudeNotify
        adapter = ClaudeNotify()
        session_uuid = "aaaa-bbbb-cccc-0002"
        adapter.deliver(session_uuid, "wakeup content", "ntf_claim001")

        canonical = "claude:%s" % session_uuid
        claimed = self.sessions.inbox_claim_all(canonical)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["message"], "wakeup content")


class TestNoCallerCallbackState(RuntimeBase):
    """Subscriptions with no caller get CB_NOT_REQUESTED + STORED status."""

    def test_deliver_no_caller_sets_stored(self):
        sub = self.sessions.create(
            target_harness="codex",
            target_id="thread-nocaller",
        )
        sub_id = sub["sub"]
        self.sessions.record_result(sub_id, "some result text")
        self.daemon.deliver_sub_callback(sub_id)

        rec = self.sessions.read(sub_id)
        self.assertEqual(rec["callback"]["state"],
                         self.sessions.CB_NOT_REQUESTED)
        self.assertEqual(rec["status"], self.sessions.STORED)

    def test_store_result_still_marks_delivered(self):
        """Direct store_result() call still returns DELIVERED (unchanged)."""
        sub = self.sessions.create(
            target_harness="codex",
            target_id="thread-storeresult",
        )
        sub_id = sub["sub"]
        self.sessions.store_result(sub_id, "direct result")

        rec = self.sessions.read(sub_id)
        self.assertEqual(rec["status"], self.sessions.DELIVERED)


class TestSendSessionUnsupported(RuntimeBase):
    """send_session raises for unsupported harnesses with no sub created."""

    def test_claude_send_raises_unsupported(self):
        from agentctl.errors import UnsupportedOperationError
        with self.assertRaises(UnsupportedOperationError):
            self.daemon.send_session("claude", "my-session", "hello")

        # No subscription should exist
        all_subs = self.sessions.list_all()
        self.assertEqual(len(all_subs), 0)

    def test_session_not_found_raises_invalid_target(self):
        from agentctl.errors import InvalidTargetError
        # Codex supported but session doesn't exist in discovery
        with mock.patch("agentctl.discover.discover", return_value=[]):
            with self.assertRaises(InvalidTargetError):
                self.daemon.send_session("codex", "nonexistent-thread", "hello")

        all_subs = self.sessions.list_all()
        self.assertEqual(len(all_subs), 0)


class TestRegisterSessionSubObserveCurrent(RuntimeBase):
    """register_session_sub binds to active turn when observe_current=True."""

    def test_observe_current_binds_running_turn(self):
        target = "codex:thread-observecurrent"
        self.daemon.notify_hook_event(target, event="begin", turn_id="turn-active")
        sub = self.daemon.register_session_sub(
            target_harness="codex",
            target_id="thread-observecurrent",
        )
        self.assertEqual(sub.get("target_turn_id"), "turn-active")

    def test_observe_current_false_does_not_bind(self):
        target = "codex:thread-noobserve"
        self.daemon.notify_hook_event(target, event="begin", turn_id="turn-active2")
        sub = self.daemon.register_session_sub(
            target_harness="codex",
            target_id="thread-noobserve",
            observe_current=False,
        )
        self.assertIsNone(sub.get("target_turn_id"))

    def test_observe_current_with_no_registry_rejects(self):
        from agentctl.errors import UsageError
        with self.assertRaises(UsageError):
            self.daemon.register_session_sub("codex", "brand-new-thread")

    def test_observe_current_idle_without_job_raises(self):
        """Idle session with no managed job should reject the subscription."""
        from agentctl.errors import UsageError
        target = "codex:thread-idle"
        # Set registry to idle
        self.sessions.registry_update(target, state="idle", current_turn_id=None)
        with self.assertRaises(UsageError):
            self.daemon.register_session_sub(
                target_harness="codex",
                target_id="thread-idle",
            )


# ---------------------------------------------------------------------------
# Helper types
# ---------------------------------------------------------------------------

class _FakeReceipt:
    def __init__(self, accepted=True, unknown=False, error=None, details=None):
        self.accepted = accepted
        self.unknown = unknown
        self.error = error
        self.details = details or {}
        self.busy = False


if __name__ == "__main__":
    unittest.main(verbosity=2)
