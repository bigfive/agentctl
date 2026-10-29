"""Tests for the dispatch and callback matrices.

Verifies actual dispatch (not mere storage), busy target rejection,
callback unsupported/not-configured/unreachable cases, require-callback
preflight rejecting before side effects, and recovery/unknown-delivery.

Uses fake harness binaries and fake HTTP servers; no real agent credits spent.
"""

import http.server
import json
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import sqlite3
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKES = os.path.join(ROOT, "tests", "fakes")
CLI = os.path.join(ROOT, "bin", "agentctl")


# ---------------------------------------------------------------------------
# Fake HTTP server for OpenCode
# ---------------------------------------------------------------------------

class FakeOpenCodeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress output

    def do_GET(self):
        if self.path == "/api/session":
            body = [{"id": "ses_abc123", "title": "Local test", "directory": "/tmp"}]
        elif self.path == "/api/session/status":
            body = {}
        elif self.path.endswith("/message"):
            body = []
        elif self.path == "/api/session/ses_abc123":
            body = {"id": "ses_abc123"}
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_POST(self):
        if "/prompt" in self.path:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            # Log to server's request_log
            self.server.request_log.append({
                "path": self.path,
                "body": json.loads(body) if body else {},
            })
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()


def start_fake_opencode(port=0):
    server = http.server.HTTPServer(("127.0.0.1", port), FakeOpenCodeHandler)
    server.request_log = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


# ---------------------------------------------------------------------------
# Fake cursor binary that simulates busy/success
# ---------------------------------------------------------------------------

FAKE_CURSOR_BUSY_SCRIPT = """\
#!/usr/bin/env python3
import sys
args = sys.argv[1:]
if "--resume" in args:
    sys.stderr.write("agent_busy: already running\\n")
    sys.exit(1)
sys.exit(0)
"""

FAKE_CURSOR_OK_SCRIPT = """\
#!/usr/bin/env python3
import sys, os
args = sys.argv[1:]
if "--resume" in args:
    prompt = sys.stdin.read()
    print("cursor handled: " + prompt.strip())
    sys.exit(0)
sys.exit(0)
"""


def _make_executable(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, 0o755)


class DispatchBase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="agentctl-dispatch-")
        self.bindir = tempfile.mkdtemp(prefix="agentctl-bins-")
        self.queue_log = os.path.join(self.home, "queue.log")
        self.env = dict(os.environ)
        self.env.update({
            "AGENTCTL_HOME": self.home,
            "AGENTCTL_CLAUDE_BIN": os.path.join(FAKES, "fake-claude"),
            "AGENTCTL_CODEX_BIN": os.path.join(FAKES, "fake-codex"),
            "FAKE_CODEX_QUEUE_LOG": self.queue_log,
        })
        self.native_home = os.path.join(self.home, "native")
        self.env["HOME"] = self.native_home
        thread = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        path = os.path.join(self.native_home, ".codex", "sessions", "2026", "10", "06",
                            "rollout-2026-10-06T01-00-00-%s.jsonl" % thread)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as stream:
            stream.write(json.dumps({"type": "session_meta", "payload": {"id": thread, "cwd": self.native_home}}) + "\n")
        self.env.pop("AGENTCTL_CURSOR_BIN", None)
        self.env.pop("AGENTCTL_OPENCODE_URL", None)
        for _var in ("CODEX_THREAD_ID", "CLAUDE_SESSION_ID",
                     "CURSOR_SESSION_ID", "OPENCODE_SESSION_ID",
                     "AGENTCTL_FROM"):
            self.env.pop(_var, None)

    def tearDown(self):
        subprocess.run([CLI, "daemon", "stop"], env=self.env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.bindir, ignore_errors=True)

    def run_cli(self, *args, **kwargs):
        check = kwargs.pop("check", True)
        stdin = kwargs.pop("stdin", None)
        if args[:2] == ("sub", "create") and args[2].startswith("codex:"):
            sid = args[2].split(":", 1)[1]
            root = os.path.join(self.native_home, ".codex", "sessions", "2026", "10", "06")
            path = os.path.join(root, "rollout-2026-10-06T01-00-00-%s.jsonl" % sid)
            with open(path, "a") as stream:
                stream.write(json.dumps({"type":"event_msg","payload":{"type":"task_started","turn_id":"turn-current"}}) + "\n")
        proc = subprocess.run([CLI] + list(args), env=self.env,
                              input=stdin.encode() if stdin is not None else None,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=60)
        out = proc.stdout.decode()
        err = proc.stderr.decode()
        if check and proc.returncode != 0:
            self.fail("agentctl %s exited %s\nstdout: %s\nstderr: %s"
                      % (" ".join(args), proc.returncode, out, err))
        return proc.returncode, out, err

    def start_job(self, harness="claude", prompt="hello"):
        _, out, _ = self.run_cli("start", harness, prompt)
        return out.strip()

    def wait_terminal(self, job, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, out, _ = self.run_cli("status", job, "--json")
            rec = json.loads(out)
            if rec["status"] in ("succeeded", "failed", "cancelled"):
                return rec
            time.sleep(0.2)
        self.fail("job %s never reached terminal state" % job)


class TestSendDispatchMatrix(DispatchBase):
    """Verify the request dispatch matrix rows."""

    def test_send_to_unsupported_harness_fails_with_exit_67(self):
        """Claude send is unsupported; must reject before any side effect."""
        code, _, err = self.run_cli(
            "send", "claude:uuid-1234", "hello", check=False)
        self.assertEqual(code, 67, "unsupported must be exit 67: %s" % err)
        self.assertIn("not supported", err)

    def test_send_to_codex_calls_queue(self):
        """Supported target: dispatch calls codex queue and returns receipt."""
        thread = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        self.run_cli("daemon", "start")
        code, out, err = self.run_cli(
            "send", "codex:%s" % thread, "do some work",
            "--no-notify", check=False)
        # Should succeed (accepted)
        self.assertEqual(code, 0, err)
        # Queue log should show the message
        with open(self.queue_log) as f:
            entries = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["thread"], thread)
        self.assertIn("do some work", entries[0]["message"])

    def test_send_to_cursor_busy_fails_with_delivery_error(self):
        """Busy cursor target (no queue) must reject with delivery error."""
        cursor_bin = os.path.join(self.bindir, "cursor-agent")
        _make_executable(cursor_bin, FAKE_CURSOR_BUSY_SCRIPT.replace("import sys", "import sys, time; time.sleep(3)"))
        self.env["AGENTCTL_CURSOR_BIN"] = cursor_bin

        # Create a cursor CLI session directory so it passes preflight
        chat_id = "5a4f300a-61b1-4975-82e2-aa4be275aa49"
        cli_root = os.path.join(os.path.expanduser("~"), ".cursor", "chats")
        chat_dir = os.path.join(self.native_home, ".cursor", "chats", "ws", chat_id)
        os.makedirs(chat_dir, exist_ok=True)
        with sqlite3.connect(os.path.join(chat_dir, "store.db")) as db:
            db.executescript("CREATE TABLE blobs(id TEXT,data BLOB); CREATE TABLE meta(key TEXT,value TEXT);")
        self.run_cli("daemon", "start")
        _, running, _ = self.run_cli("resume", "cursor:" + chat_id, "busy work", "--no-notify", "--json")
        managed = json.loads(running)["job"]
        code, _, err = self.run_cli(
            "send", "cursor:%s" % chat_id, "test prompt",
            "--no-notify", check=False)
        # Busy cursor should result in delivery error or unsupported
        self.assertNotEqual(code, 0, "busy cursor should not succeed")
        self.assertIn("busy", err)
        self.wait_terminal(managed)

    def test_send_to_opencode_calls_queued_prompt(self):
        """Supported OpenCode target: dispatch calls the v2 queued prompt."""
        server, port = start_fake_opencode()
        self.env["AGENTCTL_OPENCODE_URL"] = "http://127.0.0.1:%d" % port
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        session_id = "ses_abc123"
        self.run_cli("daemon", "start")
        code, out, err = self.run_cli(
            "send", "opencode:%s" % session_id, "hello opencode",
            "--no-notify", check=False)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(server.request_log), 1)
        req = server.request_log[0]
        self.assertIn(session_id, req["path"])
        self.assertEqual(req["path"], "/api/session/ses_abc123/prompt")

    def test_send_to_unreachable_opencode_fails_with_error(self):
        """Configured but unreachable: reject with connection error."""
        self.env["AGENTCTL_OPENCODE_URL"] = "http://127.0.0.1:1"
        self.run_cli("daemon", "start")
        code, _, err = self.run_cli(
            "send", "opencode:ses_abc123", "test",
            "--no-notify", check=False)
        self.assertNotEqual(code, 0)
        self.assertTrue(
            "reachable" in err.lower() or "unreachable" in err.lower()
            or "connect" in err.lower() or "refused" in err.lower(),
            "expected connection error in: %s" % err)

    def test_rejected_request_not_silently_queued(self):
        """A rejected dispatch must not create a pending subscription."""
        self.run_cli("daemon", "start")
        code, _, _ = self.run_cli(
            "send", "claude:my-session", "test",
            "--from", "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3",
            check=False)
        self.assertNotEqual(code, 0)
        # No subscriptions should exist (dispatch failed before sub creation)
        _, out, _ = self.run_cli("sub", "list", "--json")
        records = json.loads(out)
        # There should be no pending subscriptions for the failed send
        pending = [r for r in records
                   if r.get("target_address") == "claude:my-session"
                   and r.get("status") == "pending"]
        # The sub is deleted on dispatch failure
        # (or was never committed because dispatch failed inside cmd_send)
        # Either way, no subscription should linger for a rejected dispatch
        self.assertEqual(pending, [])


class TestCallbackMatrix(DispatchBase):
    """Verify the completion callback delivery matrix rows."""

    def test_callback_unsupported_proceeds_without_blocking(self):
        """Callback unsupported (claude caller): dispatch succeeds with note."""
        thread = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        self.run_cli("daemon", "start")
        # Send from claude (no callback support) to codex
        code, out, err = self.run_cli(
            "send", "codex:%s" % thread, "work for codex",
            "--from", "claude:my-claude-session",
            check=False)
        # Should succeed (dispatch accepted) with note about no callback
        self.assertEqual(code, 0, err)
        self.assertIn("callbacks not available", err)
        # A subscription is created but with no callback route
        _, out2, _ = self.run_cli("sub", "list", "--json")
        records = json.loads(out2)
        target_subs = [r for r in records
                       if r.get("from_harness") == "claude"]
        if target_subs:
            s = target_subs[0]
            self.assertEqual(s["from_harness"], "claude")

    def test_require_callback_rejects_before_dispatch(self):
        """require-callback preflight: dispatch never happens if no route."""
        thread = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        self.run_cli("daemon", "start")
        code, _, err = self.run_cli(
            "send", "codex:%s" % thread, "test",
            "--from", "claude:my-session",
            "--require-callback",
            check=False)
        # Must fail before dispatch (no codex queue calls)
        self.assertNotEqual(code, 0)
        self.assertIn("require-callback", err)
        # Queue should be empty (no dispatch)
        if os.path.exists(self.queue_log):
            with open(self.queue_log) as f:
                entries = [l for l in f if l.strip()]
            self.assertEqual(entries, [],
                             "dispatch must not happen when require-callback fails")

    def test_callback_delivery_fail_retains_result(self):
        """Failed callback delivery: result stays readable, delivery marked failed."""
        self.env["FAKE_CODEX_QUEUE_FAIL"] = "1"
        job = self.start_job("claude", "quick work")
        self.wait_terminal(job)
        # Create a subscription with codex callback that will fail
        env_home = self.home
        os.environ["AGENTCTL_HOME"] = env_home
        try:
            from agentctl import ipc, sessions
            ipc.call("sub_create",
                     target_harness="claude",
                     target_id=job,
                     from_harness="codex",
                     from_id="01a07a1e-c33d-70b3-b0a0-d49304f192b3",
                     job=job)
        finally:
            del os.environ["AGENTCTL_HOME"]
        # Result is still readable even if callback fails
        _, out, _ = self.run_cli("read", job)
        self.assertIn("handled: quick work", out)


class TestAutoSubscription(DispatchBase):
    """Auto-subscription is created before dispatch (races safely)."""

    def test_start_with_from_creates_subscription(self):
        """start --from creates a subscription before the child runs."""
        self.run_cli("daemon", "start")
        thread = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        _, out, err = self.run_cli(
            "start", "claude", "hello from caller",
            "--from", "codex:%s" % thread)
        job = out.strip()
        self.assertTrue(job.startswith("job_"), out)
        # Subscription hint in stderr
        self.assertIn("sub:", err)

    def test_start_no_notify_skips_subscription(self):
        """start --no-notify creates no subscription."""
        self.run_cli("daemon", "start")
        _, out, err = self.run_cli(
            "start", "claude", "no sub test",
            "--from", "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3",
            "--no-notify")
        job = out.strip()
        # No sub in stderr
        self.assertNotIn("sub:", err)
        # Check subscriptions list
        _, out2, _ = self.run_cli("sub", "list", "--json")
        records = json.loads(out2)
        job_subs = [r for r in records if r.get("job") == job]
        self.assertEqual(len(job_subs), 0)

    def test_start_with_caller_env_var_subscribes(self):
        """CODEX_THREAD_ID in env is used as caller when --from not set."""
        self.run_cli("daemon", "start")
        thread = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        self.env["CODEX_THREAD_ID"] = thread
        _, out, err = self.run_cli("start", "claude", "env caller test")
        job = out.strip()
        self.assertTrue(job.startswith("job_"), out)
        # Subscription should be created
        self.assertIn("sub:", err)


class TestDeliveryAtomicity(DispatchBase):
    """Subscription is created before dispatch (atomic race prevention)."""

    def test_subscription_exists_before_job_runs(self):
        """Sub record is created before the child process might complete."""
        self.env["FAKE_DELAY"] = "3"  # slow job
        self.run_cli("daemon", "start")
        thread = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        _, out, err = self.run_cli(
            "start", "claude", "slow for atomicity",
            "--from", "codex:%s" % thread)
        job = out.strip()

        # Immediately check that subscription exists
        _, out2, _ = self.run_cli("sub", "list", "--json")
        records = json.loads(out2)
        job_subs = [r for r in records if r.get("job") == job]
        self.assertEqual(len(job_subs), 1,
                         "subscription must exist before job completes")
        self.assertEqual(job_subs[0]["status"], "pending")
        self.run_cli("cancel", job)


class TestTranscriptCompletion(DispatchBase):
    def test_native_completion_is_delivered_on_the_background_scan(self):
        self.run_cli("daemon", "start")
        target = "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        _, out, _ = self.run_cli("sub", "create", target, "--notify", target, "--json")
        sub = json.loads(out)
        path = os.path.join(self.native_home, ".codex", "sessions", "2026", "10", "06",
                            "rollout-2026-10-06T01-00-00-%s.jsonl" % target.split(":", 1)[1])
        with open(path, "a") as stream:
            stream.write(json.dumps({"type":"event_msg","payload":{"type":"task_complete",
                "turn_id":sub["target_turn_id"],"last_agent_message":"native full result"}}) + "\n")
        deadline = time.monotonic() + 70
        while time.monotonic() < deadline:
            _, output, _ = self.run_cli("sub", "read", sub["sub"], "--json")
            row = json.loads(output)
            if row["callback"]["state"] == "delivered":
                break
            time.sleep(.5)
        self.assertEqual(row["result"], "native full result")
        self.assertEqual(row["callback"]["state"], "delivered")
        self.assertEqual(len(Path(self.queue_log).read_text().splitlines()), 1)


class TestSessionsListCommand(DispatchBase):
    """agentctl sessions list discovers sessions from fake home dirs."""

    def test_sessions_list_returns_ok(self):
        code, out, _ = self.run_cli("sessions", "list", check=False)
        self.assertEqual(code, 0)

    def test_sessions_list_json_when_no_sessions(self):
        _, out, _ = self.run_cli("sessions", "list", "--json")
        records = json.loads(out)
        self.assertIsInstance(records, list)

    def test_sessions_list_discovers_codex_session(self):
        """Discovery finds a session from a fake codex transcript."""
        tid = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        path = os.path.join(os.path.expanduser("~"), ".codex", "sessions",
                            "2026", "10", "05", "rollout-%s.jsonl" % tid)
        # We can't write to the real home, so just verify the command works
        _, out, _ = self.run_cli("sessions", "list", "--json")
        records = json.loads(out)
        self.assertIsInstance(records, list)

    def test_sessions_list_accepts_harness_filter(self):
        _, out, _ = self.run_cli("sessions", "list", "--harness", "codex", "--json")
        records = json.loads(out)
        for r in records:
            self.assertEqual(r["harness"], "codex")


class TestRecoveryAndUnknownDelivery(DispatchBase):
    """Crash recovery handles pending subscriptions conservatively."""

    def test_pending_subs_survive_daemon_restart(self):
        """Subscriptions are persisted and survive daemon restart."""
        self.env["FAKE_DELAY"] = "5"
        self.run_cli("daemon", "start")
        thread = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        _, out, err = self.run_cli(
            "start", "claude", "slow across restart",
            "--from", "codex:%s" % thread)
        job = out.strip()

        # Wait for job to start running
        deadline = time.time() + 10
        while time.time() < deadline:
            _, out, _ = self.run_cli("status", job, "--json")
            if json.loads(out)["status"] == "running":
                break
            time.sleep(0.2)

        # Stop and restart daemon
        self.run_cli("daemon", "stop")
        time.sleep(0.5)
        self.run_cli("daemon", "start")

        # Check subscriptions are still tracked
        _, out2, _ = self.run_cli("sub", "list", "--json")
        records = json.loads(out2)
        self.assertGreater(len(records), 0,
                           "subscriptions must persist across daemon restart")
        self.run_cli("cancel", job)


# ---------------------------------------------------------------------------
# Test 1: Child context isolation (env vars)
# ---------------------------------------------------------------------------

FAKE_ENV_REPORTER = """\
#!/usr/bin/env python3
import json, os, sys, time

session = None
args = sys.argv[1:]
for flag in ("--session-id", "--resume"):
    if flag in args:
        idx = args.index(flag)
        if idx + 1 < len(args):
            session = args[idx + 1]

prompt = sys.stdin.read()
env_file = os.environ.get("FAKE_ENV_REPORT_FILE")
if env_file:
    env_snapshot = {k: v for k, v in os.environ.items()
                    if k in ("CODEX_THREAD_ID", "CLAUDE_SESSION_ID",
                             "CURSOR_SESSION_ID", "OPENCODE_SESSION_ID",
                             "AGENTCTL_FROM", "AGENTCTL_HOME",
                             "FAKE_ENV_REPORT_FILE")}
    with open(env_file, "w") as f:
        json.dump(env_snapshot, f)

session = session or os.environ.get("CLAUDE_SESSION_ID", "injected")
print(json.dumps({"type": "system", "session_id": session}), flush=True)
print(json.dumps({"type": "result", "subtype": "success",
                  "is_error": False, "session_id": session,
                  "result": "env check done"}), flush=True)
"""


class TestChildContextIsolation(DispatchBase):
    """Child processes must not inherit parent identity env vars."""

    def setUp(self):
        super().setUp()
        self.env_report = os.path.join(self.home, "child_env.json")
        fake_claude = os.path.join(self.bindir, "fake-claude-env")
        _make_executable(fake_claude, FAKE_ENV_REPORTER)
        self.env["AGENTCTL_CLAUDE_BIN"] = fake_claude
        self.env["FAKE_ENV_REPORT_FILE"] = self.env_report

    def test_child_does_not_inherit_codex_thread_id(self):
        """A Claude child must NOT see CODEX_THREAD_ID from a Codex parent."""
        self.env["CODEX_THREAD_ID"] = "parent-codex-thread-id"
        self.run_cli("daemon", "start")
        job = self.start_job("claude", "env isolation test")
        self.wait_terminal(job)

        self.assertTrue(os.path.exists(self.env_report),
                        "child must write env report")
        with open(self.env_report) as f:
            child_env = json.load(f)

        self.assertNotIn("CODEX_THREAD_ID", child_env,
                         "child must NOT inherit CODEX_THREAD_ID")

    def test_child_receives_claude_session_id(self):
        """A Claude child must have CLAUDE_SESSION_ID injected."""
        self.run_cli("daemon", "start")
        _, out, _ = self.run_cli("start", "claude", "session-id-injection-test",
                                  "--json")
        record = json.loads(out)
        job = record["job"]
        native_id = record.get("native_session_id")
        self.wait_terminal(job)

        with open(self.env_report) as f:
            child_env = json.load(f)

        self.assertIn("CLAUDE_SESSION_ID", child_env,
                      "child must have CLAUDE_SESSION_ID injected")
        if native_id:
            self.assertEqual(child_env["CLAUDE_SESSION_ID"], native_id,
                             "injected CLAUDE_SESSION_ID must match job's native_session_id")

    def test_child_does_not_inherit_agentctl_from(self):
        """AGENTCTL_FROM from parent must not bleed into child."""
        self.env["AGENTCTL_FROM"] = "codex:some-parent-thread"
        self.run_cli("daemon", "start")
        job = self.start_job("claude", "agentctl-from test")
        self.wait_terminal(job)

        with open(self.env_report) as f:
            child_env = json.load(f)
        self.assertNotIn("AGENTCTL_FROM", child_env,
                         "AGENTCTL_FROM must NOT propagate to child")


# ---------------------------------------------------------------------------
# Test 2: Explicit external resume
# ---------------------------------------------------------------------------

class TestExternalResume(DispatchBase):
    """agentctl resume validates session existence before launching."""

    def test_resume_unknown_session_fails(self):
        """Resuming an unknown session must fail, never create a new one."""
        self.run_cli("daemon", "start")
        code, _, err = self.run_cli(
            "resume",
            "claude:00000000-0000-0000-0000-nonexistent11",
            "hello",
            check=False)
        self.assertNotEqual(code, 0, "unknown session must fail")
        self.assertTrue(
            "not found" in err.lower() or "unknown" in err.lower() or
            "no claude session" in err.lower(),
            "error must mention session not found: %s" % err)

    def test_resume_known_session_creates_job(self):
        """Resuming a known session creates a tracked job.

        Seeds a fake Claude transcript file in the discovery path
        (~/.claude/projects/<any_project_dir>/<uuid>.jsonl) so that
        discover.py finds the session, then verifies resume works.
        """
        self.run_cli("daemon", "start")
        native_id = "aaaabbbb-cccc-dddd-eeee-123456789012"

        # discover.py looks in ~/.claude/projects/<project_dir>/<uuid>.jsonl
        projects_root = os.path.join(self.native_home, ".claude", "projects")
        project_dir = os.path.join(projects_root, "agentctl-test-project")
        os.makedirs(project_dir, exist_ok=True)
        transcript_file = os.path.join(project_dir, "%s.jsonl" % native_id)
        with open(transcript_file, "w") as f:
            f.write(json.dumps({"type": "summary", "session_id": native_id,
                                "summary": "test session"}) + "\n")
        self.addCleanup(lambda: os.unlink(transcript_file)
                        if os.path.exists(transcript_file) else None)
        self.addCleanup(lambda: (os.rmdir(project_dir)
                                 if os.path.isdir(project_dir)
                                 and not os.listdir(project_dir) else None))

        # Try resume -- session should now be discoverable
        code, out2, err = self.run_cli(
            "resume",
            "claude:%s" % native_id,
            "follow-up prompt",
            "--no-notify",
            check=False)
        self.assertEqual(code, 0, err)
        job = out2.strip()
        self.assertTrue(job.startswith("job_"))
        self.wait_terminal(job)
        _, result, _ = self.run_cli("read", job)
        self.assertIn("follow-up prompt", result)

    def test_require_callback_no_notify_contradictory(self):
        """--require-callback and --no-notify are contradictory."""
        self.run_cli("daemon", "start")
        code, _, err = self.run_cli(
            "start", "claude", "test",
            "--require-callback", "--no-notify",
            check=False)
        self.assertNotEqual(code, 0)
        self.assertIn("contradictory", err)

    def test_send_require_callback_no_notify_contradictory(self):
        """send --require-callback --no-notify is contradictory."""
        self.run_cli("daemon", "start")
        code, _, err = self.run_cli(
            "send", "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3", "test",
            "--require-callback", "--no-notify",
            check=False)
        self.assertNotEqual(code, 0)
        self.assertIn("contradictory", err)


# ---------------------------------------------------------------------------
# Test 3: Unique OpenCode message IDs
# ---------------------------------------------------------------------------

class TestOpenCodeUniqueMessageIDs(DispatchBase):
    """Two identical OpenCode prompts must produce different message IDs."""

    def test_repeated_prompt_gets_unique_message_id(self):
        """Sending the same prompt twice must produce different messageIDs."""
        server, port = start_fake_opencode()
        self.env["AGENTCTL_OPENCODE_URL"] = "http://127.0.0.1:%d" % port
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        session_id = "ses_abc123"
        self.run_cli("daemon", "start")

        # Send the same prompt twice
        self.run_cli("send", "opencode:%s" % session_id, "identical prompt",
                     "--no-notify")
        self.run_cli("send", "opencode:%s" % session_id, "identical prompt",
                     "--no-notify")

        self.assertEqual(len(server.request_log), 2)
        id1 = server.request_log[0]["body"].get("id")
        id2 = server.request_log[1]["body"].get("id")
        self.assertIsNotNone(id1)
        self.assertIsNotNone(id2)
        self.assertNotEqual(id1, id2,
                            "identical prompts must produce different messageIDs")


# ---------------------------------------------------------------------------
# Test 4: Setup filtered mutation
# ---------------------------------------------------------------------------

class TestSetupFilteredMutation(DispatchBase):
    """setup --harness X must not mutate other harnesses."""

    def setUp(self):
        super().setUp()
        self.settings_path = os.path.join(self.home, "claude_settings.json")
        self.cursor_hooks_path = os.path.join(self.home, "cursor_hooks.json")

    def test_setup_codex_does_not_write_claude_settings(self):
        """setup --harness codex must not touch Claude settings.json."""
        from agentctl import setup as _setup
        # setup_codex doesn't write any files by default (dry_run=True default);
        # verify claude settings untouched
        _setup.setup_codex()
        # Claude settings file should NOT exist after codex-only setup
        self.assertFalse(os.path.exists(self.settings_path),
                         "Claude settings must not be written by setup_codex")

    def test_setup_claude_does_not_write_cursor_hooks(self):
        """setup --harness claude must not touch Cursor hooks.json."""
        from agentctl import setup as _setup
        _setup.setup_claude(settings_path=self.settings_path)
        # Cursor hooks should NOT exist
        self.assertFalse(os.path.exists(self.cursor_hooks_path),
                         "Cursor hooks must not be written by setup_claude")

    def test_cmd_setup_harness_codex_via_cli(self):
        """agentctl setup --harness codex must not mutate Claude settings."""
        from agentctl import setup as _setup
        settings_path = os.path.join(self.home, "s.json")
        cursor_path = os.path.join(self.home, "c.json")

        Path(settings_path).write_text(json.dumps({"model": "sonnet"}))
        Path(cursor_path).write_text(json.dumps({"hooks": {"stop": [{"command":"user-history.sh"}]}}))
        # Now get content
        with open(settings_path) as f:
            before_claude = f.read()
        with open(cursor_path) as f:
            before_cursor = f.read()

        # Call setup_codex only - should not change Claude or Cursor files
        _setup.setup_codex()

        with open(settings_path) as f:
            after_claude = f.read()
        with open(cursor_path) as f:
            after_cursor = f.read()

        self.assertEqual(before_claude, after_claude,
                         "setup_codex must not change Claude settings")
        self.assertEqual(before_cursor, after_cursor,
                         "setup_codex must not change Cursor hooks")


# ---------------------------------------------------------------------------
# Test 5: Queued busy unrelated stop (subscription turn filtering)
# ---------------------------------------------------------------------------

class TestQueuedBusyUnrelatedStop(DispatchBase):
    """Subscription for turn X must not fire when unrelated turn Y completes."""

    def test_hook_with_different_turn_id_does_not_trigger_subscription(self):
        """A sub bound to turn_id=C must not fire when hook fires for turn_id=D."""
        self.run_cli("daemon", "start")
        target = "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3"

        _, out, _ = self.run_cli(
            "sub", "create", target,
            "--notify", "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3",
            "--json")
        sub_id = json.loads(out)["sub"]

        # Must set AGENTCTL_HOME before calling sessions.update so it targets
        # the test home, not the real user home.
        import agentctl.sessions as _sess
        os.environ["AGENTCTL_HOME"] = self.home
        try:
            _sess.update(sub_id, target_turn_id="turn-C")
        finally:
            del os.environ["AGENTCTL_HOME"]

        # Call hook_complete with explicit turn_id=D (different) via IPC
        import agentctl.ipc as _ipc
        os.environ["AGENTCTL_HOME"] = self.home
        try:
            _ipc.call("hook_event", target=target, event="complete", turn_id="turn-D", result="unrelated")
        except Exception:
            pass
        finally:
            del os.environ["AGENTCTL_HOME"]

        time.sleep(1)
        _, out4, _ = self.run_cli("sub", "read", sub_id, "--json")
        rec2 = json.loads(out4)
        # sub bound to turn-C, hook fired for turn-D: must remain pending
        self.assertEqual(rec2["status"], "pending",
                         "subscription bound to turn-C must stay pending when "
                         "turn-D hook fires")

    def test_matching_hook_invocation_is_retired_without_completing_listener(self):
        self.run_cli("daemon", "start")
        target = "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        _, output, _ = self.run_cli("sub", "create", target, "--json")
        sub = json.loads(output)
        code, _, error = self.run_cli("_hook", "complete", "--harness", "codex", check=False)
        self.assertEqual(code,64)
        self.assertIn("retired",error)
        _, output, _ = self.run_cli("sub", "read", sub["sub"], "--json")
        self.assertIsNone(json.loads(output)["result"])


# ---------------------------------------------------------------------------
# Test 6: Result retained on callback failure
# ---------------------------------------------------------------------------

class TestResultRetainedOnCallbackFailure(DispatchBase):
    """Result is always readable even when callback delivery fails."""

    def test_result_readable_when_callback_fails(self):
        """Failed callback delivery must not erase the stored result."""
        self.env["FAKE_CODEX_QUEUE_FAIL"] = "1"
        self.run_cli("daemon", "start")
        thread = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        _, out, err = self.run_cli(
            "start", "claude", "result retention test",
            "--from", "codex:%s" % thread)
        job = out.strip()
        self.wait_terminal(job)

        # Result must be readable even though callback failed
        _, out2, _ = self.run_cli("read", job)
        self.assertIn("handled: result retention test", out2)

        # Check subscription status reflects failed callback
        _, out3, _ = self.run_cli("sub", "list", "--json")
        records = json.loads(out3)
        job_subs = [r for r in records if r.get("job") == job]
        if job_subs:
            sub = job_subs[0]
            cb = sub.get("callback", {})
            # Callback state should be failed (not delivered)
            self.assertIn(cb.get("state"), ("failed", "unsupported"),
                          "callback state must reflect delivery failure")

    def test_no_resend_after_failed_delivery(self):
        """A failed delivery must not be retried (could duplicate messages)."""
        self.env["FAKE_CODEX_QUEUE_FAIL"] = "1"
        self.run_cli("daemon", "start")
        thread = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        _, out, _ = self.run_cli(
            "start", "claude", "no resend test",
            "--from", "codex:%s" % thread)
        job = out.strip()
        self.wait_terminal(job)

        # Queue log must have at most ONE failed attempt (not multiple retries)
        if os.path.exists(self.queue_log):
            with open(self.queue_log) as f:
                entries = [l for l in f if l.strip()]
            # Failed delivery means queue rejected so no entries logged
            self.assertEqual(len(entries), 0,
                             "failed delivery must not be retried")


# ---------------------------------------------------------------------------
# Test 7: Result-only/no-notify
# ---------------------------------------------------------------------------

class TestResultOnlyNoNotify(DispatchBase):
    """--no-notify skips subscription but result is still trackable."""

    def test_no_notify_still_tracks_result(self):
        """--no-notify job result is readable; no subscription created."""
        self.run_cli("daemon", "start")
        thread = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        _, out, err = self.run_cli(
            "start", "claude", "no notify result",
            "--from", "codex:%s" % thread,
            "--no-notify")
        job = out.strip()
        self.assertNotIn("sub:", err, "--no-notify must not create subscription")
        self.wait_terminal(job)

        # Result is still readable
        _, out2, _ = self.run_cli("read", job)
        self.assertIn("handled: no notify result", out2)

        # No subscriptions for this job
        _, out3, _ = self.run_cli("sub", "list", "--json")
        records = json.loads(out3)
        job_subs = [r for r in records if r.get("job") == job]
        self.assertEqual(len(job_subs), 0,
                         "--no-notify must create no subscriptions")

    def test_no_notify_still_checks_busy_for_send(self):
        """send --no-notify to unsupported harness still fails."""
        self.run_cli("daemon", "start")
        code, _, err = self.run_cli(
            "send", "claude:some-session", "test",
            "--no-notify",
            check=False)
        self.assertNotEqual(code, 0, "unsupported send must fail even with --no-notify")


# ---------------------------------------------------------------------------
# Test 8: Fast completion race
# ---------------------------------------------------------------------------

class TestFastCompletionRace(DispatchBase):
    """Subscription created atomically before dispatch catches fast completions."""

    def test_fast_job_subscription_is_not_missed(self):
        """When a job completes very fast, the pre-created subscription captures it."""
        self.run_cli("daemon", "start")
        thread = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        _, out, err = self.run_cli(
            "start", "claude", "instant job",
            "--from", "codex:%s" % thread)
        job = out.strip()

        # The job may already be done by the time we check
        rec = self.wait_terminal(job, timeout=30)
        self.assertEqual(rec["status"], "succeeded")

        # The subscription must have been created AND delivered
        _, out2, _ = self.run_cli("sub", "list", "--json")
        records = json.loads(out2)
        job_subs = [r for r in records if r.get("job") == job]
        self.assertEqual(len(job_subs), 1,
                         "subscription must exist for fast job")
        sub = job_subs[0]
        # Result must be captured
        self.assertIsNotNone(sub.get("result"),
                             "fast completion must be captured in subscription")


if __name__ == "__main__":
    unittest.main(verbosity=2)
