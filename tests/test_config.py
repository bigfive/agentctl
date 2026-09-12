"""Harness binary resolution."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentctl import config
from agentctl.errors import PrerequisiteMissingError


class TestResolveBinary(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="agentctl-bin-")
        self.saved = dict(os.environ)
        # An empty PATH, so only what a test installs can be found.
        os.environ["PATH"] = self.dir
        for var in config.ENV_OVERRIDES.values():
            os.environ.pop(var, None)
        # The known extra install locations are facts about this machine, not
        # about the lookup, so they are taken out of the picture here.
        self.saved_extra = config.EXTRA_BINARY_PATHS
        config.EXTRA_BINARY_PATHS = {}

    def tearDown(self):
        config.EXTRA_BINARY_PATHS = self.saved_extra
        os.environ.clear()
        os.environ.update(self.saved)
        shutil.rmtree(self.dir, ignore_errors=True)

    def install(self, name):
        path = os.path.join(self.dir, name)
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)
        return path

    def test_cursor_found_under_its_specific_name(self):
        expected = self.install("cursor-agent")
        self.assertEqual(config.resolve_binary("cursor"), expected)

    def test_cursor_found_under_the_generic_agent_name(self):
        # Cursor's Homebrew cask links only cursor-agent, but its own
        # installer also provides `agent`, and some machines have only that.
        expected = self.install("agent")
        self.assertEqual(config.resolve_binary("cursor"), expected)

    def test_specific_name_wins_when_both_exist(self):
        specific = self.install("cursor-agent")
        self.install("agent")
        self.assertEqual(config.resolve_binary("cursor"), specific)

    def test_env_override_beats_path(self):
        self.install("cursor-agent")
        override = self.install("somewhere-else")
        os.environ["AGENTCTL_CURSOR_BIN"] = override
        self.assertEqual(config.resolve_binary("cursor"), override)

    def test_missing_binary_names_everything_it_tried(self):
        with self.assertRaises(PrerequisiteMissingError) as caught:
            config.resolve_binary("cursor")
        self.assertIn("cursor-agent", caught.exception.message)
        self.assertIn("agent", caught.exception.message)

    def test_env_override_pointing_at_nothing_fails_loudly(self):
        os.environ["AGENTCTL_CURSOR_BIN"] = os.path.join(self.dir, "absent")
        with self.assertRaises(PrerequisiteMissingError):
            config.resolve_binary("cursor")

    def test_every_harness_has_at_least_one_candidate_name(self):
        for harness in config.HARNESSES:
            self.assertTrue(config.BINARY_NAMES[harness],
                            "%s has no candidate names" % harness)

    def test_opencode_auth_uses_agentctl_credentials(self):
        with patch.dict(os.environ, {
            "AGENTCTL_OPENCODE_USERNAME": "voice",
            "AGENTCTL_OPENCODE_PASSWORD": "secret",
            "OPENCODE_SERVER_USERNAME": "ignored",
            "OPENCODE_SERVER_PASSWORD": "ignored",
        }, clear=False):
            self.assertEqual(config.opencode_credentials(), ("voice", "secret"))
            self.assertEqual(config.opencode_auth_header(), "Basic dm9pY2U6c2VjcmV0")

    def test_opencode_auth_is_optional(self):
        with patch.dict(os.environ, {
            "AGENTCTL_OPENCODE_USERNAME": "voice",
            "AGENTCTL_OPENCODE_PASSWORD": "",
        }, clear=False):
            self.assertIsNone(config.opencode_auth_header())


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestLauncher(unittest.TestCase):
    """The launcher must work when installed onto PATH as a symlink."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="agentctl-link-")
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_runs_through_a_symlink(self):
        import subprocess
        link = os.path.join(self.dir, "agentctl")
        os.symlink(os.path.join(self.root, "bin", "agentctl"), link)
        process = subprocess.run([link, "--version"], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, cwd="/", timeout=60)
        self.assertEqual(process.returncode, 0, process.stderr.decode())
        self.assertIn("agentctl", process.stdout.decode())

    def test_runs_through_a_chain_of_symlinks(self):
        import subprocess
        first = os.path.join(self.dir, "one")
        second = os.path.join(self.dir, "agentctl")
        os.symlink(os.path.join(self.root, "bin", "agentctl"), first)
        os.symlink(first, second)
        process = subprocess.run([second, "--version"], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, cwd="/", timeout=60)
        self.assertEqual(process.returncode, 0, process.stderr.decode())


class TestCursorIdeChats(unittest.TestCase):
    """An IDE chat cannot receive a delivery, so it must be refused."""

    def setUp(self):
        from agentctl.notify import cursor as cursor_notify
        self.module = cursor_notify
        self.dir = tempfile.mkdtemp(prefix="agentctl-ide-")
        self.saved = cursor_notify.IDE_STORE
        cursor_notify.IDE_STORE = self.dir

    def tearDown(self):
        self.module.IDE_STORE = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_ide_chat_is_refused_at_registration(self):
        from agentctl import notify
        from agentctl.errors import UnsupportedOperationError
        chat = "33b8f260-b104-4342-9219-78ffd819ccce"
        os.makedirs(os.path.join(self.dir, chat))
        with self.assertRaises(UnsupportedOperationError) as caught:
            notify.parse_target("cursor:%s" % chat)
        self.assertIn("IDE chat", caught.exception.message)
        self.assertIn("agentctl wait", caught.exception.hint)

    def test_cli_chat_is_accepted(self):
        from agentctl import notify
        chat = "440e4817-0e1d-4cd2-a948-d2d59b121ae0"
        self.assertEqual(notify.parse_target("cursor:%s" % chat),
                         ("cursor", chat))

    def test_cloud_agent_is_still_refused(self):
        from agentctl import notify
        from agentctl.errors import UnsupportedOperationError
        with self.assertRaises(UnsupportedOperationError):
            notify.parse_target("cursor:bc-b8651c1b-38d5-4991-aa13-9aae3aa228ae")
