"""End-to-end tests over the real daemon, using stand-in harness binaries.

Every test drives the actual CLI, the actual socket, and the actual job store.
The only substitutions are the harness binaries themselves, injected through
the documented AGENTCTL_*_BIN overrides, so no test spends agent credits.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKES = os.path.join(ROOT, "tests", "fakes")
CLI = os.path.join(ROOT, "bin", "agentctl")


class Harness(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="agentctl-test-")
        self.workdir = tempfile.mkdtemp(prefix="agentctl-work-")
        self.queue_log = os.path.join(self.home, "queue.log")
        self.env = dict(os.environ)
        self.env.update({
            "AGENTCTL_HOME": self.home,
            "AGENTCTL_CLAUDE_BIN": os.path.join(FAKES, "fake-claude"),
            "AGENTCTL_CODEX_BIN": os.path.join(FAKES, "fake-codex"),
            "FAKE_CODEX_QUEUE_LOG": self.queue_log,
            # Harness tests must never contact the operator's Desktop.
            "AGENTCTL_CODEX_IPC_SOCKET": os.path.join(self.home, "absent-desktop.sock"),
        })
        self.env.pop("FAKE_DELAY", None)
        self.env.pop("FAKE_CODEX_QUEUE_FAIL", None)

    def tearDown(self):
        self.run_cli("daemon", "stop", check=False)
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def run_cli(self, *args, **kwargs):
        check = kwargs.pop("check", True)
        process = subprocess.run(
            [CLI] + list(args), env=self.env, cwd=self.workdir,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        out = process.stdout.decode()
        err = process.stderr.decode()
        if check and process.returncode != 0:
            self.fail("agentctl %s exited %s\nstdout: %s\nstderr: %s"
                      % (" ".join(args), process.returncode, out, err))
        return process.returncode, out, err

    def start(self, harness, prompt):
        _, out, _ = self.run_cli("start", harness, prompt)
        job = out.strip()
        self.assertTrue(job.startswith("job_"), out)
        return job

    def status(self, job):
        _, out, _ = self.run_cli("status", job, "--json")
        return json.loads(out)

    def wait_terminal(self, job, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            record = self.status(job)
            if record["status"] in ("succeeded", "failed", "cancelled"):
                return record
            time.sleep(0.2)
        self.fail("job %s never reached a terminal state" % job)

    def queued(self):
        if not os.path.isfile(self.queue_log):
            return []
        with open(self.queue_log) as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def wait_for_delivery(self, job, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            record = self.status(job)
            waits = record.get("waits") or []
            if waits and waits[0]["delivery"]["state"] != "pending":
                return waits[0]
            time.sleep(0.2)
        self.fail("delivery for %s stayed pending" % job)


class TestLifecycle(Harness):
    def test_interrupt_stops_worker_resumes_session_and_deduplicates(self):
        import signal
        _, out, _ = self.run_cli('start', 'codex', 'SLOW original', '--options-json',
                                 '{"reasoning_effort":"low"}', '--json')
        first = json.loads(out)
        deadline = time.time() + 5
        while not self.status(first['job']).get('native_session_id'):
            self.assertLess(time.time(), deadline)
            time.sleep(.05)
        original = self.status(first['job'])
        _, out, _ = self.run_cli('interrupt', first['job'], 'Use the existing parser',
                                 '--request-id', 'correction-1', '--json')
        replacement = json.loads(out)
        self.assertEqual('cancelled', self.status(first['job'])['status'])
        with self.assertRaises(ProcessLookupError):
            os.kill(original['pid'], 0)
        self.assertEqual(original['native_session_id'], replacement['native_session_id'])
        self.assertEqual(original['options'], replacement['options'])
        self.assertEqual(first['job'], replacement['followup_of'])
        self.wait_terminal(replacement['job'])
        _, result, _ = self.run_cli('read', replacement['job'])
        self.assertIn('Use the existing parser', result)
        _, repeated, _ = self.run_cli('interrupt', first['job'], 'Use the existing parser',
                                      '--request-id', 'correction-1', '--json')
        self.assertEqual(replacement['job'], json.loads(repeated)['job'])
        self.assertEqual([], self.queued())
        code, _, _ = self.run_cli('interrupt', first['job'], 'different', check=False)
        self.assertNotEqual(0, code)

    def test_interrupt_claude_resumes_the_same_session(self):
        first = self.start('claude', 'HANG until corrected')
        original = self.status(first)
        _, out, _ = self.run_cli('interrupt', first, 'Continue with the corrected task', '--json')
        replacement = json.loads(out)
        self.assertEqual(original['native_session_id'], replacement['native_session_id'])
        self.assertEqual('cancelled', self.status(first)['status'])
        self.assertEqual('succeeded', self.wait_terminal(replacement['job'])['status'])
        _, result, _ = self.run_cli('read', replacement['job'])
        self.assertIn('Continue with the corrected task', result)

    def test_interrupt_rejects_session_delivery(self):
        _, out, _ = self.run_cli('notify', 'codex:manager', '--message', 'hello', '--json')
        delivery = json.loads(out)
        code, _, _ = self.run_cli('interrupt', delivery['job'], 'correction', check=False)
        self.assertNotEqual(0, code)

    def test_codex_options_are_recorded_passed_and_inherited_by_followup(self):
        options = {'reasoning_effort': 'low', 'service_tier': 'priority'}
        _, out, _ = self.run_cli('start', 'codex', 'option test', '--options-json',
                                 json.dumps(options), '--json')
        first = json.loads(out)
        self.wait_terminal(first['job'])
        self.assertEqual(options, first['options'])
        self.assertIn('model_reasoning_effort="low"', first['command'])
        self.assertIn('service_tier="priority"', first['command'])
        _, out, _ = self.run_cli('followup', first['job'], 'Continue', '--json')
        second = json.loads(out)
        self.wait_terminal(second['job'])
        self.assertEqual(options, second['options'])
        _, out, _ = self.run_cli('followup', second['job'], 'Clear options',
                                 '--options-json', '{}', '--json')
        cleared = json.loads(out)
        self.wait_terminal(cleared['job'])
        self.assertEqual({}, cleared['options'])
        self.assertNotIn('model_reasoning_effort="low"', cleared['command'])

    def test_invalid_or_unsupported_worker_options_fail_before_launch(self):
        for harness, options in [('codex', {'reasoning_effort': 'invalid'}),
                                 ('codex', {'unknown': 'low'}),
                                 ('claude', {'reasoning_effort': 'low'}),
                                 ('codex', [])]:
            code, _, _ = self.run_cli('start', harness, 'Must not start',
                                      '--options-json', json.dumps(options), check=False)
            self.assertEqual(64, code)
        _, out, _ = self.run_cli('list', '--json')
        self.assertEqual([], json.loads(out))

    def test_start_runs_to_success_and_records_result(self):
        job = self.start("claude", "do the thing")
        record = self.wait_terminal(job)
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["exit_code"], 0)
        _, out, _ = self.run_cli("read", job)
        self.assertIn("handled: do the thing", out)

    def test_native_session_id_is_known_up_front(self):
        job = self.start("claude", "hello")
        record = self.status(job)
        self.assertTrue(record["native_session_id"])
        _, out, _ = self.run_cli("status", job)
        self.assertIn("child: claude:%s" % record["native_session_id"], out)

    def test_child_failure_is_terminal_and_loud(self):
        job = self.start("claude", "please FAIL")
        record = self.wait_terminal(job)
        self.assertEqual(record["status"], "failed")
        code, _, _ = self.run_cli("read", job, check=False)
        self.assertEqual(code, 70)

    def test_codex_child_reports_harness_failure(self):
        job = self.start("codex", "please FAIL")
        record = self.wait_terminal(job)
        self.assertEqual(record["status"], "failed")
        self.assertIn("out of credits", record["error"])

    def test_codex_child_result_comes_from_output_last_message(self):
        job = self.start("codex", "write it")
        record = self.wait_terminal(job)
        self.assertEqual(record["status"], "succeeded")
        _, out, _ = self.run_cli("read", job)
        self.assertIn("codex handled: write it", out)

    def test_cancel_marks_the_job_cancelled(self):
        self.env["FAKE_DELAY"] = "0"
        job = self.start("claude", "HANG forever")
        deadline = time.time() + 10
        while self.status(job)["status"] != "running" and time.time() < deadline:
            time.sleep(0.1)
        self.run_cli("cancel", job)
        record = self.wait_terminal(job)
        self.assertEqual(record["status"], "cancelled")

    def test_unknown_handle_fails_with_its_own_exit_code(self):
        code, _, err = self.run_cli("status", "job_missing", check=False)
        self.assertEqual(code, 65)
        self.assertIn("unknown job handle", err)

    def test_followup_reuses_the_native_session(self):
        job = self.start("claude", "first")
        first = self.wait_terminal(job)
        _, out, _ = self.run_cli("followup", job, "second")
        child = out.strip()
        record = self.wait_terminal(child)
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["native_session_id"],
                         first["native_session_id"])
        self.assertEqual(record["followup_of"], job)


class TestNotify(Harness):
    def test_arbitrary_message_and_id_survive_restart_without_duplicate(self):
        target = "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        args = ("notify", target, "--message", "question\nsecond line", "--id", "alert-one", "--json")
        _, out, _ = self.run_cli(*args)
        first = json.loads(out)
        self.assertEqual(first["kind"], "session_delivery")
        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(first["transport_delivery"]["transport"], "codex queue")
        self.assertIn("steer_warning", first["transport_delivery"])
        self.run_cli("daemon", "stop")
        time.sleep(0.3)
        _, out, _ = self.run_cli(*args)
        self.assertEqual(json.loads(out)["job"], first["job"])
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(self.queued()[0]["message"], "question\nsecond line")
        code, _, _ = self.run_cli("notify", target, "--message", "different", "--id", "alert-one", check=False)
        self.assertNotEqual(code, 0)

    def test_message_file_empty_invalid_and_delivery_failure(self):
        target = "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        file = os.path.join(self.home, "message.txt")
        with open(file, "w") as handle:
            handle.write("from file")
        self.run_cli("notify", target, "--message-file", file)
        self.assertEqual(self.queued()[0]["message"], "from file")
        for args in (("notify", target, "--message", ""), ("notify", "invalid", "--message", "x")):
            self.assertNotEqual(self.run_cli(*args, check=False)[0], 0)

    def test_failed_delivery_is_not_retried_by_id(self):
        self.env["FAKE_CODEX_QUEUE_FAIL"] = "1"
        args = ("notify", "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3", "--message", "x", "--id", "failed-alert")
        self.assertEqual(self.run_cli(*args, check=False)[0], 68)
        code, _, err = self.run_cli(*args, check=False)
        self.assertNotEqual(code, 0)
        self.assertIn("ambiguous", err)

    def test_start_request_id_does_not_launch_twice(self):
        args = ("start", "claude", "one launch", "--cwd", self.workdir, "--request-id", "launch-one", "--json")
        _, out, _ = self.run_cli(*args)
        first = json.loads(out)
        _, out, _ = self.run_cli(*args)
        self.assertEqual(first["job"], json.loads(out)["job"])
        _, out, _ = self.run_cli("list", "--json")
        self.assertEqual(len(json.loads(out)), 1)


class TestAsyncWait(Harness):
    def test_asyncwait_returns_immediately_and_delivers_once(self):
        self.env["FAKE_DELAY"] = "2"
        job = self.start("claude", "slow work")
        thread = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        began = time.time()
        _, out, _ = self.run_cli("asyncwait", job, "--notify",
                                 "codex:%s" % thread)
        self.assertLess(time.time() - began, 2.0,
                        "asyncwait blocked instead of returning")
        self.assertIn("delivery: pending", out)

        wait = self.wait_for_delivery(job)
        self.assertEqual(wait["delivery"]["state"], "delivered")

        messages = self.queued()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["thread"], thread)
        body = messages[0]["message"]
        self.assertIn("[agentctl completion]", body)
        self.assertIn("job: %s" % job, body)
        self.assertIn("status: succeeded", body)
        self.assertIn("full: agentctl read %s" % job, body)

    def test_asyncwait_on_a_finished_job_delivers_at_once(self):
        job = self.start("claude", "quick")
        self.wait_terminal(job)
        self.run_cli("asyncwait", job, "--notify",
                     "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3")
        wait = self.wait_for_delivery(job)
        self.assertEqual(wait["delivery"]["state"], "delivered")
        self.assertEqual(len(self.queued()), 1)

    def test_repeated_asyncwait_registrations_deliver_once_each(self):
        job = self.start("claude", "quick")
        self.wait_terminal(job)
        for _ in range(3):
            self.run_cli("asyncwait", job, "--notify",
                         "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3")
        deadline = time.time() + 20
        while time.time() < deadline:
            record = self.status(job)
            states = [w["delivery"]["state"] for w in record["waits"]]
            if all(state != "pending" for state in states):
                break
            time.sleep(0.2)
        self.assertEqual(len(self.queued()), 3,
                         "three distinct waits should send three messages")

        # Re-running the daemon's recovery must not resend anything.
        self.run_cli("daemon", "stop")
        time.sleep(0.5)
        self.run_cli("daemon", "start")
        time.sleep(1.5)
        self.assertEqual(len(self.queued()), 3,
                         "a daemon restart must not duplicate deliveries")

    def test_failed_job_still_notifies_with_failed_status(self):
        job = self.start("claude", "please FAIL")
        self.run_cli("asyncwait", job, "--notify",
                     "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3")
        self.wait_for_delivery(job)
        self.assertIn("status: failed", self.queued()[0]["message"])

    def test_delivery_failure_is_recorded_and_not_retried(self):
        self.env["FAKE_CODEX_QUEUE_FAIL"] = "1"
        job = self.start("claude", "quick")
        self.wait_terminal(job)
        # The job is already terminal, so delivery is attempted inline and
        # its failure is reported by asyncwait itself.
        code, _, err = self.run_cli(
            "asyncwait", job, "--notify",
            "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3", check=False)
        self.assertEqual(code, 68)
        self.assertIn("codex queue failed", err)
        wait = self.wait_for_delivery(job)
        self.assertEqual(wait["delivery"]["state"], "failed")
        self.assertIn("codex queue failed", wait["delivery"]["error"])
        # The result stays readable even though delivery failed.
        _, out, _ = self.run_cli("read", job)
        self.assertIn("handled: quick", out)

    def test_invalid_targets_are_rejected_at_registration(self):
        job = self.start("claude", "quick")
        self.wait_terminal(job)
        for target, expected in (
                ("nonsense", 66),
                ("cursor:bc-123", 67),
                ("opencode:not-a-session", 66),
                ("claude:whatever", 67)):
            code, _, err = self.run_cli("asyncwait", job, "--notify", target,
                                        check=False)
            self.assertEqual(code, expected, "%s -> %s" % (target, err))
        self.assertEqual(self.status(job)["waits"], [])


class TestBlockingWait(Harness):
    def test_wait_blocks_then_prints_the_envelope(self):
        self.env["FAKE_DELAY"] = "1"
        job = self.start("claude", "slow")
        code, out, _ = self.run_cli("wait", job, check=False)
        self.assertEqual(code, 0)
        self.assertIn("[agentctl completion]", out)
        self.assertIn("status: succeeded", out)

    def test_wait_exit_code_reports_a_failed_child(self):
        job = self.start("claude", "please FAIL")
        code, out, _ = self.run_cli("wait", job, check=False)
        self.assertEqual(code, 70)
        self.assertIn("status: failed", out)

    def test_rewake_writes_to_stderr_and_exits_two(self):
        job = self.start("claude", "quick")
        code, out, err = self.run_cli("wait", job, "--rewake", check=False)
        self.assertEqual(code, 2, "Claude's asyncRewake contract needs exit 2")
        self.assertEqual(out.strip(), "")
        self.assertIn("[agentctl completion]", err)

    def test_wait_covers_several_jobs(self):
        first = self.start("claude", "one")
        second = self.start("claude", "two")
        code, out, _ = self.run_cli("wait", first, second, check=False)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("[agentctl completion]"), 2)

    def test_wait_timeout_is_explicit(self):
        job = self.start("claude", "HANG please")
        code, _, err = self.run_cli("wait", job, "--timeout", "1", check=False)
        self.assertNotEqual(code, 0)
        self.assertIn("timed out waiting", err)
        self.run_cli("cancel", job)


class TestDaemon(Harness):
    def test_daemon_starts_on_demand_and_reports_status(self):
        code, out, _ = self.run_cli("daemon", "status", check=False)
        self.assertEqual(code, 69)
        self.assertIn("running: False", out)
        self.run_cli("daemon", "start")
        code, out, _ = self.run_cli("daemon", "status")
        self.assertEqual(code, 0)
        self.assertIn("running: True", out)

    def test_a_child_outlives_a_daemon_restart(self):
        self.env["FAKE_DELAY"] = "4"
        job = self.start("claude", "slow across a restart")
        self.run_cli("daemon", "stop")
        time.sleep(0.5)
        self.run_cli("daemon", "start")
        record = self.wait_terminal(job, timeout=30)
        self.assertEqual(record["status"], "succeeded")
        with open(os.path.join(self.home, "jobs", job, "result.txt")) as f:
            self.assertIn("handled: slow across a restart", f.read())

    def test_a_wait_registered_before_a_restart_still_delivers(self):
        self.env["FAKE_DELAY"] = "5"
        job = self.start("claude", "slow with a wait")
        self.run_cli("asyncwait", job, "--notify",
                     "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3")
        self.run_cli("daemon", "stop")
        time.sleep(0.5)
        self.run_cli("daemon", "start")
        wait = self.wait_for_delivery(job, timeout=40)
        self.assertEqual(wait["delivery"]["state"], "delivered")
        self.assertEqual(len(self.queued()), 1)

    def test_list_shows_jobs(self):
        job = self.start("claude", "quick")
        self.wait_terminal(job)
        _, out, _ = self.run_cli("list")
        self.assertIn(job, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
