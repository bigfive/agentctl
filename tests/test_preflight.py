"""A notification target is checked when the wait is registered.

The failure this prevents is the worst one available: asyncwait returns 0, the
agent promises the user an answer, and the delivery quietly fails later where
only the job record shows it.
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


class TestPreflight(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="agentctl-pre-")
        self.env = dict(os.environ)
        self.env.update({
            "AGENTCTL_HOME": self.home,
            "AGENTCTL_CLAUDE_BIN": os.path.join(FAKES, "fake-claude"),
            "AGENTCTL_CODEX_BIN": os.path.join(FAKES, "fake-codex"),
            # A port nothing is listening on.
            "AGENTCTL_OPENCODE_URL": "http://127.0.0.1:1",
        })

    def tearDown(self):
        self.run_cli("daemon", "stop")
        shutil.rmtree(self.home, ignore_errors=True)

    def run_cli(self, *args):
        process = subprocess.run([CLI] + list(args), env=self.env,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=120)
        return (process.returncode, process.stdout.decode(),
                process.stderr.decode())

    def finished_job(self):
        _, out, _ = self.run_cli("start", "claude", "quick")
        job = out.strip()
        deadline = time.time() + 30
        while time.time() < deadline:
            _, out, _ = self.run_cli("status", job, "--json")
            if json.loads(out)["status"] != "running":
                return job
            time.sleep(0.2)
        self.fail("job never finished")

    def test_unreachable_opencode_server_fails_at_registration(self):
        job = self.finished_job()
        code, _, err = self.run_cli("asyncwait", job, "--notify",
                                    "opencode:ses_abc123")
        self.assertEqual(code, 66)
        self.assertIn("no OpenCode server is reachable", err)
        self.assertIn("agentctl wait", err)

    def test_a_refused_target_registers_no_wait(self):
        job = self.finished_job()
        self.run_cli("asyncwait", job, "--notify", "opencode:ses_abc123")
        _, out, _ = self.run_cli("status", job, "--json")
        self.assertEqual(json.loads(out)["waits"], [],
                         "a refused target must not leave a wait behind")

    def test_unknown_cursor_chat_fails_at_registration(self):
        job = self.finished_job()
        code, _, err = self.run_cli(
            "asyncwait", job, "--notify",
            "cursor:aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
        self.assertEqual(code, 66)
        self.assertIn("no chat", err)

    def test_codex_target_still_registers(self):
        # Codex has no preflight: its queue command is the check.
        job = self.finished_job()
        code, out, _ = self.run_cli(
            "asyncwait", job, "--notify",
            "codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3")
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
