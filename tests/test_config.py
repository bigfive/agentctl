"""Harness binary resolution."""

import os
import shutil
import sys
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
