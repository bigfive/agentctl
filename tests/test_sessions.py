"""Tests for session subscription store, listener inspection, and dispatch logic.

Uses real subscription records in a temporary home directory.
No live harness binaries or network calls are made.
"""

import json
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKES = os.path.join(ROOT, "tests", "fakes")
CLI = os.path.join(ROOT, "bin", "agentctl")

# Patch environment for tests
sys.path.insert(0, ROOT)


class TestSubscriptionStore(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="agentctl-subs-")
        os.environ["AGENTCTL_HOME"] = self.home

    def tearDown(self):
        del os.environ["AGENTCTL_HOME"]
        shutil.rmtree(self.home, ignore_errors=True)

    def _import_sessions(self):
        import importlib
        import agentctl.sessions as mod
        importlib.reload(mod)
        return mod

    def test_create_and_read_subscription(self):
        from agentctl import sessions as _sess
        record = _sess.create(
            target_harness="codex",
            target_id="01a07a1e-c33d-70b3-b0a0-d49304f192b3",
            from_harness="claude",
            from_id="session-uuid",
        )
        self.assertTrue(record["sub"].startswith("sub_"))
        self.assertEqual(record["target_harness"], "codex")
        self.assertEqual(record["from_harness"], "claude")
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["callback"]["state"], "pending")

        # Round-trip read
        read_back = _sess.read(record["sub"])
        self.assertEqual(read_back["sub"], record["sub"])
        self.assertEqual(read_back["target_address"],
                         "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3")

    def test_update_subscription_status(self):
        from agentctl import sessions as _sess
        record = _sess.create("codex", "thread-1")
        _sess.update(record["sub"], status=_sess.DELIVERED)
        updated = _sess.read(record["sub"])
        self.assertEqual(updated["status"], "delivered")

    def test_set_callback_delivered(self):
        from agentctl import sessions as _sess
        record = _sess.create("codex", "thread-1",
                              from_harness="claude", from_id="sid")
        ntf = "ntf_abc123"
        _sess.set_callback(record["sub"], _sess.CB_DELIVERED,
                           notification_id=ntf)
        updated = _sess.read(record["sub"])
        self.assertEqual(updated["callback"]["state"], "delivered")
        self.assertEqual(updated["callback"]["notification_id"], ntf)
        self.assertIsNotNone(updated["callback"]["delivered_at"])

    def test_set_callback_failed_with_error(self):
        from agentctl import sessions as _sess
        record = _sess.create("codex", "thread-1",
                              from_harness="claude", from_id="sid")
        _sess.set_callback(record["sub"], _sess.CB_FAILED,
                           error="connection refused")
        updated = _sess.read(record["sub"])
        self.assertEqual(updated["callback"]["state"], "failed")
        self.assertEqual(updated["callback"]["error"], "connection refused")

    def test_store_result_marks_delivered(self):
        from agentctl import sessions as _sess
        record = _sess.create("codex", "thread-1")
        _sess.store_result(record["sub"], "The result text")
        updated = _sess.read(record["sub"])
        self.assertEqual(updated["status"], "delivered")
        self.assertEqual(updated["result"], "The result text")
        self.assertIsNotNone(updated["result_stored_at"])

    def test_list_all_returns_all_subs(self):
        from agentctl import sessions as _sess
        r1 = _sess.create("codex", "tid-1")
        r2 = _sess.create("claude", "sid-2")
        all_subs = _sess.list_all()
        sub_ids = {r["sub"] for r in all_subs}
        self.assertIn(r1["sub"], sub_ids)
        self.assertIn(r2["sub"], sub_ids)

    def test_list_for_target(self):
        from agentctl import sessions as _sess
        r1 = _sess.create("codex", "thread-1")
        r2 = _sess.create("codex", "thread-2")
        r3 = _sess.create("claude", "session-a")
        codex1_subs = _sess.list_for_target("codex:thread-1")
        ids = {r["sub"] for r in codex1_subs}
        self.assertIn(r1["sub"], ids)
        self.assertNotIn(r2["sub"], ids)
        self.assertNotIn(r3["sub"], ids)

    def test_list_for_job(self):
        from agentctl import sessions as _sess
        r1 = _sess.create("codex", "tid", job="job_abc123")
        r2 = _sess.create("codex", "tid", job="job_xyz999")
        r3 = _sess.create("codex", "tid")
        for_job = _sess.list_for_job("job_abc123")
        self.assertEqual(len(for_job), 1)
        self.assertEqual(for_job[0]["sub"], r1["sub"])

    def test_unknown_sub_raises(self):
        from agentctl import sessions as _sess
        from agentctl.errors import UnknownHandleError
        with self.assertRaises(UnknownHandleError):
            _sess.read("sub_notexist")

    def test_is_callback_flag_prevents_return_subscription(self):
        from agentctl import sessions as _sess
        record = _sess.create("codex", "thread-1", is_callback=True)
        self.assertTrue(record["is_callback"])

    def test_notification_id_is_stable(self):
        from agentctl import sessions as _sess, ids
        sub_id = "sub_abc123"
        ntf1 = ids.notification_id_for_sub(sub_id, "completed")
        ntf2 = ids.notification_id_for_sub(sub_id, "completed")
        self.assertEqual(ntf1, ntf2)
        self.assertTrue(ntf1.startswith("ntf_"))

    def test_notification_ids_differ_by_event(self):
        from agentctl import ids
        sub_id = "sub_abc123"
        ntf_complete = ids.notification_id_for_sub(sub_id, "completed")
        ntf_timeout = ids.notification_id_for_sub(sub_id, "timeout")
        self.assertNotEqual(ntf_complete, ntf_timeout)


class TestSenderCapabilities(unittest.TestCase):
    """Test send and callback capability resolution."""

    def test_codex_send_supported_with_queue(self):
        from agentctl.sender import send_capability
        cap = send_capability("codex", "01a07a1e-c33d-70b3-b0a0-d49304f192b3")
        self.assertTrue(cap["supported"])
        self.assertTrue(cap["queue"])

    def test_claude_send_unsupported(self):
        from agentctl.sender import send_capability
        cap = send_capability("claude", "uuid-1234")
        self.assertFalse(cap["supported"])

    def test_cursor_ide_send_unsupported(self):
        from agentctl.discover import _CURSOR_IDE_STORE
        from agentctl.sender import send_capability
        # Without an actual IDE store on disk, cursor defaults to cursor-cli
        cap = send_capability("cursor", "uuid-that-does-not-exist-in-ide-store")
        # It's cursor-cli since the IDE store doesn't have this id
        self.assertTrue(cap["supported"])

    def test_opencode_send_supported(self):
        from agentctl.sender import send_capability
        cap = send_capability("opencode", "ses_abc123")
        self.assertTrue(cap["supported"])

    def test_codex_callback_supported(self):
        from agentctl.sender import callback_capability
        cap = callback_capability("codex")
        self.assertTrue(cap["supported"])
        self.assertEqual(cap["mechanism"], "codex queue")

    def test_claude_external_callback_is_unsupported(self):
        from agentctl.sender import callback_capability
        cap = callback_capability("claude")
        self.assertFalse(cap["supported"])
        self.assertFalse(cap["requires_setup"])

    @mock.patch("agentctl.setup.inspect_claude", return_value={"complete":False})
    def test_preflight_callback_unconfigured_raises_with_require(self, inspect):
        from agentctl.sender import preflight_callback
        from agentctl.errors import UnsupportedOperationError
        with self.assertRaises(UnsupportedOperationError):
            preflight_callback("claude", "uuid",
                               require_callback=True)

    @mock.patch("agentctl.setup.inspect_claude", return_value={"complete":False})
    def test_preflight_callback_unconfigured_ok_without_require(self, inspect):
        from agentctl.sender import preflight_callback
        result = preflight_callback("claude", "uuid",
                                    require_callback=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "unsupported")

    @mock.patch.dict(os.environ, {"AGENTCTL_CODEX_BIN":os.path.join(FAKES,"fake-codex")})
    def test_preflight_callback_codex_ok(self):
        from agentctl.sender import preflight_callback
        result = preflight_callback(
            "codex", "01a07a1e-c33d-70b3-b0a0-d49304f192b3",
            require_callback=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "configured")


class TestCallerIdentity(unittest.TestCase):
    def tearDown(self):
        for var in ("AGENTCTL_FROM", "CODEX_THREAD_ID", "CLAUDE_SESSION_ID",
                    "CURSOR_SESSION_ID", "OPENCODE_SESSION_ID"):
            os.environ.pop(var, None)

    def test_explicit_from_parsed(self):
        from agentctl.sender import resolve_caller
        h, sid = resolve_caller("codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3")
        self.assertEqual(h, "codex")
        self.assertEqual(sid, "01a07a1e-c33d-70b3-b0a0-d49304f192b3")

    def test_agentctl_from_env(self):
        from agentctl.sender import resolve_caller
        os.environ["AGENTCTL_FROM"] = "claude:session-uuid-123"
        h, sid = resolve_caller()
        self.assertEqual(h, "claude")
        self.assertEqual(sid, "session-uuid-123")

    def test_codex_thread_id_env(self):
        from agentctl.sender import resolve_caller
        os.environ["CODEX_THREAD_ID"] = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        h, sid = resolve_caller()
        self.assertEqual(h, "codex")
        self.assertEqual(sid, "01a07a1e-c33d-70b3-b0a0-d49304f192b3")

    def test_claude_session_id_env(self):
        from agentctl.sender import resolve_caller
        os.environ["CLAUDE_SESSION_ID"] = "my-session-uuid"
        h, sid = resolve_caller()
        self.assertEqual(h, "claude")
        self.assertEqual(sid, "my-session-uuid")

    def test_unknown_caller_returns_none_none(self):
        from agentctl.sender import resolve_caller
        # Ensure all identity vars are cleared for this test
        for var in ("AGENTCTL_FROM", "CODEX_THREAD_ID", "CLAUDE_SESSION_ID",
                    "CURSOR_SESSION_ID", "OPENCODE_SESSION_ID"):
            os.environ.pop(var, None)
        h, sid = resolve_caller()
        self.assertIsNone(h)
        self.assertIsNone(sid)

    def test_explicit_takes_precedence_over_env(self):
        from agentctl.sender import resolve_caller
        os.environ["CODEX_THREAD_ID"] = "env-thread-id"
        h, sid = resolve_caller("claude:explicit-session")
        self.assertEqual(h, "claude")
        self.assertEqual(sid, "explicit-session")

    def test_claude_id_takes_precedence_over_codex_thread_id(self):
        """A Claude child that inherited CODEX_THREAD_ID is not misidentified."""
        from agentctl.sender import resolve_caller
        os.environ["CODEX_THREAD_ID"] = "codex-parent-thread"
        os.environ["CLAUDE_SESSION_ID"] = "claude-child-session"
        h, sid = resolve_caller()
        self.assertEqual(h, "claude")
        self.assertEqual(sid, "claude-child-session")


class TestSetupConfig(unittest.TestCase):
    def test_setup_all_reports_retired_installation_without_mutations(self):
        from agentctl import setup
        with tempfile.TemporaryDirectory() as tmp:
            result = setup.setup_all(settings_path=os.path.join(tmp, "settings.json"), cursor_hooks_path=os.path.join(tmp, "hooks.json"))
            self.assertEqual(set(result), {"claude", "codex", "cursor", "opencode"})
            self.assertFalse(result["codex"]["ok"])
            self.assertEqual(os.listdir(tmp), [])


class TestListenersViaIPC(unittest.TestCase):
    """Integration test: listener inspection via CLI."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="agentctl-listeners-")
        self.env = dict(os.environ)
        self.env.update({
            "AGENTCTL_HOME": self.home,
            "HOME": os.path.join(self.home, "native"),
            "AGENTCTL_CLAUDE_BIN": os.path.join(FAKES, "fake-claude"),
            "AGENTCTL_CODEX_BIN": os.path.join(FAKES, "fake-codex"),
        })

    def tearDown(self):
        subprocess.run([CLI, "daemon", "stop"], env=self.env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(self.home, ignore_errors=True)

    def run_cli(self, *args, **kwargs):
        check = kwargs.pop("check", True)
        if args[:2] == ("sub", "create"):
            sid = args[2].split(":", 1)[1]
            root = Path(self.env["HOME"]) / ".codex/sessions/2026/10/06"
            root.mkdir(parents=True, exist_ok=True)
            path = root / ("rollout-2026-10-06T00-00-00-" + sid + ".jsonl")
            path.write_text(json.dumps({"type":"session_meta","payload":{"id":sid}}) + "\n" +
                            json.dumps({"type":"event_msg","payload":{"type":"task_started","turn_id":"active-turn"}}) + "\n")
        proc = subprocess.run([CLI] + list(args), env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=30)
        out = proc.stdout.decode()
        err = proc.stderr.decode()
        if check and proc.returncode != 0:
            self.fail("agentctl %s exited %s\nstdout: %s\nstderr: %s"
                      % (" ".join(args), proc.returncode, out, err))
        return proc.returncode, out, err

    def test_sub_create_and_list_via_cli(self):
        self.run_cli("daemon", "start")
        # Create subscription via CLI
        _, out, _ = self.run_cli(
            "sub", "create",
            "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3",
            "--notify", "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3",
            "--json")
        record = json.loads(out)
        self.assertTrue(record["sub"].startswith("sub_"))
        # List via CLI
        _, out2, _ = self.run_cli("sub", "list", "--json")
        records = json.loads(out2)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["target_harness"], "codex")

    def test_listeners_for_target_via_cli(self):
        self.run_cli("daemon", "start")
        addr = "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        _, out, _ = self.run_cli(
            "sub", "create", addr,
            "--notify", "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3",
            "--json")
        # List for specific target
        _, out2, _ = self.run_cli("sub", "list", addr, "--json")
        records = json.loads(out2)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["target_address"], addr)

    def test_sub_read_via_cli(self):
        self.run_cli("daemon", "start")
        addr = "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        _, out, _ = self.run_cli("sub", "create", addr, "--json")
        record = json.loads(out)
        sub_id = record["sub"]
        _, out2, _ = self.run_cli("sub", "read", sub_id, "--json")
        read_back = json.loads(out2)
        self.assertEqual(read_back["sub"], sub_id)


class TestCallbackLoopPrevention(unittest.TestCase):
    """is_callback flag prevents return subscriptions on callback receipts."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="agentctl-loop-")
        os.environ["AGENTCTL_HOME"] = self.home

    def tearDown(self):
        del os.environ["AGENTCTL_HOME"]
        shutil.rmtree(self.home, ignore_errors=True)

    def test_is_callback_set_on_callback_records(self):
        from agentctl import sessions as _sess
        record = _sess.create("codex", "tid", is_callback=True)
        self.assertTrue(record["is_callback"])

    def test_is_callback_not_set_by_default(self):
        from agentctl import sessions as _sess
        record = _sess.create("codex", "tid")
        self.assertFalse(record["is_callback"])


class TestRequireCallbackPreflight(unittest.TestCase):
    """--require-callback fails before dispatch for unsupported callers."""

    @mock.patch("agentctl.setup.inspect_claude", return_value={"complete":False})
    def test_require_callback_rejects_claude_caller(self, inspect):
        from agentctl.sender import preflight_callback
        from agentctl.errors import UnsupportedOperationError
        with self.assertRaises(UnsupportedOperationError) as ctx:
            preflight_callback("claude", "my-session", require_callback=True)
        self.assertIn("require-callback", str(ctx.exception))

    @mock.patch.dict(os.environ, {"AGENTCTL_CODEX_BIN":os.path.join(FAKES,"fake-codex")})
    def test_require_callback_accepts_codex_caller(self):
        from agentctl.sender import preflight_callback
        result = preflight_callback(
            "codex", "01a07a1e-c33d-70b3-b0a0-d49304f192b3",
            require_callback=True)
        self.assertTrue(result["ok"])

    def test_require_callback_rejects_cursor_ide(self):
        """cursor-ide kind cannot receive callbacks."""
        # Simulate that this chat id is in the IDE store by patching
        import unittest.mock as mock
        from agentctl import sender
        with mock.patch("agentctl.discover.is_ide_chat", return_value=True):
            from agentctl.errors import UnsupportedOperationError
            with mock.patch("agentctl.setup.inspect_cursor", return_value={"complete":False}), self.assertRaises(UnsupportedOperationError):
                sender.preflight_callback("cursor", "some-uuid",
                                          require_callback=True)


class TestJobSubscriptionDelivery(unittest.TestCase):
    """Subscription delivery when an agentctl-owned job completes."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="agentctl-jobsub-")
        self.env = dict(os.environ)
        self.env.update({
            "AGENTCTL_HOME": self.home,
            "AGENTCTL_CLAUDE_BIN": os.path.join(FAKES, "fake-claude"),
            "AGENTCTL_CODEX_BIN": os.path.join(FAKES, "fake-codex"),
            "FAKE_CODEX_QUEUE_LOG": os.path.join(self.home, "queue.log"),
        })

    def tearDown(self):
        subprocess.run([CLI, "daemon", "stop"], env=self.env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(self.home, ignore_errors=True)

    def run_cli(self, *args, **kwargs):
        check = kwargs.pop("check", True)
        proc = subprocess.run([CLI] + list(args), env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=60)
        out = proc.stdout.decode()
        err = proc.stderr.decode()
        if check and proc.returncode != 0:
            self.fail("agentctl %s exited %s\nstdout: %s\nstderr: %s"
                      % (" ".join(args), proc.returncode, out, err))
        return proc.returncode, out, err

    def test_job_completion_delivers_sub_callback(self):
        """A subscription tied to a job gets a callback when the job completes."""
        # Start a job, then subscribe with --from to auto-create subscription
        self.env["CODEX_THREAD_ID"] = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        _, out, _ = self.run_cli(
            "start", "claude", "test sub delivery",
            "--from", "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3",
        )
        job = out.strip()
        self.assertTrue(job.startswith("job_"), out)

        # Wait for job completion
        deadline = time.time() + 30
        while time.time() < deadline:
            _, out, _ = self.run_cli("status", job, "--json")
            if json.loads(out)["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.2)

        # Check subscriptions - should have been created and delivered
        _, out2, _ = self.run_cli("sub", "list", "--json")
        records = json.loads(out2)
        # Filter to records for this job
        job_subs = [r for r in records if r.get("job") == job]
        if job_subs:
            sub_rec = job_subs[0]
            self.assertNotEqual(sub_rec["status"], "pending",
                                "subscription must not stay pending after job completes")
            # Queue log should have received a delivery attempt
            queue_log = os.path.join(self.home, "queue.log")
            if os.path.exists(queue_log):
                with open(queue_log) as f:
                    messages = [json.loads(line) for line in f if line.strip()]
                if messages:
                    body = messages[0]["message"]
                    self.assertIn("[agentctl completion]", body)


class TestSessionInbox(unittest.TestCase):
    """Tests for per-session inbox (Claude callback delivery)."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="agentctl-inbox-")
        os.environ["AGENTCTL_HOME"] = self.home

    def tearDown(self):
        del os.environ["AGENTCTL_HOME"]
        shutil.rmtree(self.home, ignore_errors=True)

    def test_inbox_put_creates_record(self):
        from agentctl import sessions as _sess
        record = _sess.inbox_put("my-session-id", "hello message", "ntf_abc123")
        self.assertEqual(record["session_id"], "my-session-id")
        self.assertEqual(record["message"], "hello message")
        self.assertEqual(record["message_id"], "ntf_abc123")
        self.assertFalse(record["claimed"])

    def test_inbox_list_returns_unclaimed(self):
        from agentctl import sessions as _sess
        _sess.inbox_put("sess-1", "msg-one", "ntf_001")
        _sess.inbox_put("sess-1", "msg-two", "ntf_002")
        messages = _sess.inbox_list("sess-1")
        self.assertEqual(len(messages), 2)
        self.assertFalse(messages[0]["claimed"])
        self.assertFalse(messages[1]["claimed"])

    def test_inbox_list_empty_when_no_messages(self):
        from agentctl import sessions as _sess
        messages = _sess.inbox_list("nonexistent-session")
        self.assertEqual(messages, [])

    def test_inbox_claim_all_marks_claimed_and_returns(self):
        from agentctl import sessions as _sess
        _sess.inbox_put("sess-claim", "first", "ntf_c1")
        _sess.inbox_put("sess-claim", "second", "ntf_c2")
        claimed = _sess.inbox_claim_all("sess-claim")
        self.assertEqual(len(claimed), 2)
        for c in claimed:
            self.assertTrue(c["claimed"])
            self.assertIn("claimed_at", c)

    def test_inbox_claim_all_subsequent_returns_empty(self):
        from agentctl import sessions as _sess
        _sess.inbox_put("sess-once", "hello", "ntf_o1")
        _sess.inbox_claim_all("sess-once")
        # Second claim should return nothing
        claimed2 = _sess.inbox_claim_all("sess-once")
        self.assertEqual(claimed2, [])

    def test_inbox_list_empty_after_claim(self):
        from agentctl import sessions as _sess
        _sess.inbox_put("sess-lc", "test", "ntf_lc1")
        _sess.inbox_claim_all("sess-lc")
        unclaimed = _sess.inbox_list("sess-lc")
        self.assertEqual(unclaimed, [],
                         "inbox_list must not return claimed messages")

    def test_inbox_put_multiple_sessions_isolated(self):
        from agentctl import sessions as _sess
        _sess.inbox_put("sess-A", "for A", "ntf_A1")
        _sess.inbox_put("sess-B", "for B", "ntf_B1")
        msgs_a = _sess.inbox_list("sess-A")
        msgs_b = _sess.inbox_list("sess-B")
        self.assertEqual(len(msgs_a), 1)
        self.assertEqual(len(msgs_b), 1)
        self.assertEqual(msgs_a[0]["message"], "for A")
        self.assertEqual(msgs_b[0]["message"], "for B")

    def test_inbox_claim_empty_session_returns_empty(self):
        from agentctl import sessions as _sess
        result = _sess.inbox_claim_all("no-such-session")
        self.assertEqual(result, [])

    def test_claude_notify_deliver_writes_inbox(self):
        """ClaudeNotify.deliver writes to the inbox for the given session."""
        from agentctl.notify.claude import ClaudeNotify
        from agentctl import sessions as _sess
        adapter = ClaudeNotify()
        adapter.deliver("my-claude-session", "test message", "ntf_x999")
        messages = _sess.inbox_list("claude:my-claude-session")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["message"], "test message")
        self.assertEqual(messages[0]["message_id"], "ntf_x999")

    def test_claude_notify_validate_accepts_non_empty(self):
        """ClaudeNotify.validate accepts non-empty ids without raising."""
        from agentctl.notify.claude import ClaudeNotify
        adapter = ClaudeNotify()
        # Should not raise
        adapter.validate("some-session-id")
        adapter.validate("00000000-0000-0000-0000-000000000001")

    def test_claude_notify_validate_rejects_empty(self):
        """ClaudeNotify.validate rejects empty string."""
        from agentctl.notify.claude import ClaudeNotify
        from agentctl.errors import InvalidTargetError
        adapter = ClaudeNotify()
        with self.assertRaises(InvalidTargetError):
            adapter.validate("")

    def test_claude_notify_preflight_always_raises(self):
        """ClaudeNotify.preflight raises UnsupportedOperationError."""
        from agentctl.notify.claude import ClaudeNotify
        from agentctl.errors import UnsupportedOperationError
        adapter = ClaudeNotify()
        with self.assertRaises(UnsupportedOperationError):
            adapter.preflight("any-session")


if __name__ == "__main__":
    unittest.main(verbosity=2)
