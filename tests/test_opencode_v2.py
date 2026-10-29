import unittest
from unittest.mock import patch

from agentctl import discover, sender
from agentctl.children.opencode import OpenCodeChild
from agentctl.notify.opencode import OpenCodeNotify


class TestOpenCodeV2Child(unittest.TestCase):
    def setUp(self):
        self.child = OpenCodeChild()

    @patch("agentctl.children.opencode.httpc.request")
    @patch("agentctl.children.opencode.config.opencode_auth_header",
           return_value="Basic test")
    @patch("agentctl.children.opencode.config.opencode_base_url",
           return_value="http://127.0.0.1:4096")
    def test_preallocates_session_with_v2_api(self, base, auth, request):
        request.return_value = (200, {"data": {"id": "ses_abc123"}})

        session = self.child.preallocate_session("/tmp/project", "openai/big-pickle")

        self.assertEqual(session, "ses_abc123")
        request.assert_called_once_with(
            "POST", "http://127.0.0.1:4096/api/session",
            {"location": {"directory": "/tmp/project"}},
            auth_header="Basic test")

    @patch("agentctl.children.opencode.config.resolve_binary",
           return_value="/opt/homebrew/bin/opencode")
    @patch("agentctl.children.opencode.config.opencode_base_url",
           return_value="http://127.0.0.1:4096")
    def test_runs_attached_session_with_current_cli_flags(self, base, binary):
        command = self.child.start_command(
            "ses_abc123", "/tmp/project", "openai/big-pickle", "/tmp/result")

        self.assertEqual(command, [
            "/opt/homebrew/bin/opencode", "run",
            "--server", "http://127.0.0.1:4096",
            "--session", "ses_abc123",
            "--format", "json", "--auto", "-m", "openai/big-pickle"])
        self.assertNotIn("--attach", command)
        self.assertNotIn("--dir", command)


class TestOpenCodeV2Notify(unittest.TestCase):
    def setUp(self):
        self.notify = OpenCodeNotify()

    @patch("agentctl.notify.opencode.httpc.request")
    @patch("agentctl.notify.opencode.config.opencode_auth_header",
           return_value="Basic test")
    @patch("agentctl.notify.opencode.config.opencode_base_url",
           return_value="http://127.0.0.1:4096")
    def test_preflight_reads_session_from_v2_api(self, base, auth, request):
        request.return_value = (200, {"data": {"id": "ses_abc123"}})

        self.notify.preflight("ses_abc123")

        request.assert_called_once_with(
            "GET", "http://127.0.0.1:4096/api/session/ses_abc123",
            timeout=10, auth_header="Basic test")

    @patch("agentctl.notify.opencode.httpc.request")
    @patch("agentctl.notify.opencode.config.opencode_auth_header",
           return_value="Basic test")
    @patch("agentctl.notify.opencode.config.opencode_base_url",
           return_value="http://127.0.0.1:4096")
    def test_delivers_idempotent_queued_prompt_through_v2_api(
            self, base, auth, request):
        request.return_value = (200, {"data": {"id": "msg_abc123"}})

        result = self.notify.deliver("ses_abc123", "Blocked: need a decision",
                                     "ntf_0123456789abcdef01234567")

        self.assertEqual(result["response"], 200)
        method, url, payload = request.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(url,
                         "http://127.0.0.1:4096/api/session/ses_abc123/prompt")
        self.assertEqual(payload["text"], "Blocked: need a decision")
        self.assertEqual(payload["delivery"], "queue")
        self.assertRegex(payload["id"], r"^msg_[a-f0-9]{24}$")
        self.assertNotIn("parts", payload)


class TestOpenCodeV2Sessions(unittest.TestCase):
    @patch("agentctl.httpc.request")
    @patch("agentctl.config.opencode_auth_header", return_value="Basic test")
    def test_discovery_reads_authenticated_v2_envelope(self, auth, request):
        request.return_value = (200, {"data": [{"id": "ses_abc123", "title": "Test"}]})
        rows = discover._opencode_sessions_http("http://localhost:4096")
        self.assertEqual(rows[0]["native_id"], "ses_abc123")
        self.assertEqual(request.call_args.args[1], "http://localhost:4096/api/session")
        self.assertEqual(request.call_args.kwargs["auth_header"], "Basic test")

    @patch("agentctl.httpc.request")
    @patch("agentctl.config.opencode_auth_header", return_value="Basic test")
    def test_history_reads_authenticated_v2_envelope(self, auth, request):
        request.return_value = (200, {"data": [{
            "info": {"role": "assistant"},
            "parts": [{"type": "text", "text": "A full response"}],
        }]})
        history = discover._opencode_history_http("ses_abc123", 10, "http://localhost:4096")
        self.assertEqual(history, [{"role": "assistant", "content": "A full response"}])
        self.assertEqual(request.call_args.args[1], "http://localhost:4096/api/session/ses_abc123/message")
        self.assertEqual(request.call_args.kwargs["auth_header"], "Basic test")

    @patch("agentctl.httpc.request", return_value=(202, {}))
    @patch("agentctl.config.opencode_auth_header", return_value="Basic test")
    def test_send_admits_authenticated_queued_prompt(self, auth, request):
        receipt = sender._dispatch_opencode("ses_abc123", "Continue", request_nonce="stable")
        self.assertTrue(receipt.accepted)
        method, url, payload = request.call_args.args
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/api/session/ses_abc123/prompt"))
        self.assertEqual(payload["text"], "Continue")
        self.assertEqual(payload["delivery"], "queue")
        self.assertEqual(payload["id"], receipt.details["message_id"])
        self.assertEqual(request.call_args.kwargs["auth_header"], "Basic test")


if __name__ == "__main__":
    unittest.main(verbosity=2)
