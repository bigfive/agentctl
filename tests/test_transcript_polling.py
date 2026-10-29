"""Hook-free native parsing, turn binding, restart, and polling delivery."""
import json
import os
import sqlite3
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest import mock

from agentctl import transcripts, sessions, store
from agentctl.daemon import Daemon
from agentctl.errors import UsageError, UnsupportedOperationError

ID = "12345678-1234-1234-1234-123456789abc"


class TranscriptPollingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"AGENTCTL_HOME": str(self.home / "agentctl")})
        env.start(); self.addCleanup(env.stop)
        self.daemon = Daemon()
        self.addCleanup(self.daemon.stopping.set)
        self.native_snapshot = transcripts.snapshot
        self.patch = mock.patch("agentctl.transcripts.snapshot", side_effect=lambda h, sid, home=None:
                                self.native_snapshot(h, sid, str(self.home)))
        self.patch.start(); self.addCleanup(self.patch.stop)

    def codex(self, events):
        path = self.home / ".codex/sessions/2026/10/06" / ("rollout-2026-10-06T00-00-00-" + ID + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": ID}}) + "\n" +
                        "".join(json.dumps({"type": "event_msg", "payload": e}) + "\n" for e in events))
        return path

    def test_poll_binds_native_turn_ignores_registry_and_keeps_full_result(self):
        path = self.codex([{"type": "task_started", "turn_id": "wanted"}])
        sessions.registry_update("codex:" + ID, state="running", current_turn_id="stale-hook-turn")
        sub = self.daemon.register_session_sub("codex", ID)
        self.assertEqual(sub["target_turn_id"], "wanted")
        self.assertEqual(sub["observer_kind"], "transcript")
        self.daemon.poll_transcript_subscriptions()
        self.assertIsNone(sessions.read(sub["sub"])["result_stored_at"])
        with path.open("a") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete",
                "turn_id": "other", "last_agent_message": "unrelated"}}) + "\n")
        self.daemon.poll_transcript_subscriptions()
        self.assertIsNone(sessions.read(sub["sub"])["result_stored_at"])
        with path.open("a") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete",
                "turn_id": "wanted", "last_agent_message": "result" * 1000}}) + "\n")
        self.daemon.poll_transcript_subscriptions()
        row = sessions.read(sub["sub"])
        self.assertEqual(row["result"], "result" * 1000)
        self.assertEqual(row["callback"]["state"], "not_requested")

    def test_completion_detected_after_restart_and_callback_attempted_once(self):
        self.codex([{"type": "task_started", "turn_id": "wanted"}])
        with mock.patch("agentctl.sender.preflight_callback", return_value={"ok": True}):
            sub = self.daemon.register_session_sub("codex", ID, from_harness="codex", from_id="listener")
        self.codex([{"type": "task_started", "turn_id": "wanted"},
                    {"type": "task_complete", "turn_id": "wanted", "last_agent_message": "done"}])
        restarted = Daemon()
        restarted.recover()
        adapter = mock.Mock()
        adapter.deliver.return_value = None
        with mock.patch("agentctl.notify.get", return_value=adapter):
            restarted.poll_transcript_subscriptions()
            restarted.poll_transcript_subscriptions()
        adapter.deliver.assert_called_once()
        self.assertEqual(sessions.read(sub["sub"])["callback"]["state"], "delivered")

    def test_idle_and_unsupported_sessions_reject_before_creating_listener(self):
        self.codex([{"type": "task_started", "turn_id": "done"}, {"type": "task_complete", "turn_id": "done"}])
        with self.assertRaises(UsageError):
            self.daemon.register_session_sub("codex", ID)
        with self.assertRaises(UnsupportedOperationError):
            self.daemon.register_session_sub("cursor", "missing")
        self.assertEqual(sessions.list_all(), [])

    def test_truncated_record_and_silence_never_complete(self):
        path = self.codex([{"type": "task_started", "turn_id": "wanted"}])
        sub = self.daemon.register_session_sub("codex", ID)
        with path.open("a") as f:
            f.write('{"type":"event_msg","payload":')
        self.daemon.poll_transcript_subscriptions()
        self.assertIsNone(sessions.read(sub["sub"])["result"])

    def test_background_poller_runs_without_search_index(self):
        self.codex([{"type": "task_started", "turn_id": "wanted"}])
        sub = self.daemon.register_session_sub("codex", ID)
        self.codex([{"type": "task_started", "turn_id": "wanted"},
                    {"type": "task_complete", "turn_id": "wanted", "last_agent_message": "done"}])
        self.assertIsNone(self.daemon.search_index)
        scanned = threading.Event()
        poll = self.daemon.poll_transcript_subscriptions
        def scan():
            poll()
            scanned.set()
        with mock.patch.object(self.daemon, "poll_transcript_subscriptions", side_effect=scan):
            self.daemon.start_transcript_polling()
            self.assertTrue(scanned.wait(2))
            self.daemon.stopping.set()
            self.daemon.transcript_poller.join(2)
        self.assertEqual(sessions.read(sub["sub"])["result"], "done")

    def test_poll_errors_remain_unknown_and_expiry_is_not_completion(self):
        self.codex([{"type": "task_started", "turn_id": "wanted"}])
        sub = self.daemon.register_session_sub("codex", ID)
        with mock.patch("agentctl.transcripts.snapshot", side_effect=OSError("unreachable store")):
            self.daemon.poll_transcript_subscriptions()
        self.assertIn("unreachable", sessions.read(sub["sub"])["observation_error"])
        sessions.update(sub["sub"], expires_at=time.time() - 1)
        self.daemon.poll_transcript_subscriptions()
        row = sessions.read(sub["sub"])
        self.assertEqual(row["status"], "expired")
        self.assertNotEqual(row.get("completion_status"), "completed")

    def test_claude_tool_result_and_tool_use_do_not_end_turn(self):
        path = self.home / ".claude/projects/project" / (ID + ".jsonl")
        path.parent.mkdir(parents=True)
        events = [{"type": "user", "uuid": "prompt", "message": {"content": "do work"}},
                  {"type": "assistant", "message": {"content": [], "stop_reason": "tool_use"}},
                  {"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}], "stop_reason": "max_tokens"}},
                  {"type": "user", "uuid": "tool", "message": {"content": [{"type": "tool_result", "content": "output"}]}}]
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        sub = self.daemon.register_session_sub("claude", ID)
        self.assertEqual(sub["target_turn_id"], "prompt")
        self.daemon.poll_transcript_subscriptions()
        self.assertIsNone(sessions.read(sub["sub"])["result"])
        with path.open("a") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "finished"}],
                "stop_reason": "end_turn"}}) + "\n")
        self.daemon.poll_transcript_subscriptions()
        self.assertEqual(sessions.read(sub["sub"])["result"], "finished")

    def test_cursor_ide_explicit_status_matches_its_user_bubble(self):
        path = self.home / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE cursorDiskKV(key TEXT PRIMARY KEY,value TEXT)")
            composer = {"name": "IDE", "status": "generating", "fullConversationHeadersOnly": [
                {"type": 1, "bubbleId": "prompt"}, {"type": 2, "bubbleId": "reply"}]}
            db.executemany("INSERT INTO cursorDiskKV VALUES (?,?)", [("composerData:" + ID, json.dumps(composer)),
                ("bubbleId:" + ID + ":prompt", json.dumps({"type": 1, "bubbleId": "prompt", "text": "work"})),
                ("bubbleId:" + ID + ":reply", json.dumps({"type": 2, "bubbleId": "reply", "text": "finished"}))])
        sub = self.daemon.register_session_sub("cursor", ID)
        self.daemon.poll_transcript_subscriptions()
        self.assertIsNone(sessions.read(sub["sub"])["result"])
        composer["status"] = "completed"
        with sqlite3.connect(path) as db:
            db.execute("UPDATE cursorDiskKV SET value=? WHERE key=?", (json.dumps(composer), "composerData:" + ID))
        self.daemon.poll_transcript_subscriptions()
        self.assertEqual(sessions.read(sub["sub"])["result"], "finished")

    def test_open_code_tool_boundary_requires_parent_terminal_marker(self):
        path = self.home / ".local/share/opencode/opencode.db"
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as db:
            db.executescript("CREATE TABLE session(id TEXT,title TEXT,directory TEXT,time_updated INTEGER);"
                             "CREATE TABLE message(id TEXT,session_id TEXT,data TEXT,time_created INTEGER);"
                             "CREATE TABLE part(id TEXT,message_id TEXT,data TEXT,time_created INTEGER);")
            db.execute("INSERT INTO session VALUES ('ses_native','OpenCode','/repo',1000)")
            db.executemany("INSERT INTO message VALUES (?, 'ses_native', ?, ?)", [
                ("prompt", json.dumps({"role": "user"}), 1),
                ("reply", json.dumps({"role": "assistant", "parentID": "prompt", "time": {"completed": 2}, "finish": "tool-calls"}), 2)])
        with mock.patch("agentctl.discover._opencode_sessions_http", return_value=[]):
            sub = self.daemon.register_session_sub("opencode", "ses_native")
            self.daemon.poll_transcript_subscriptions()
            self.assertIsNone(sessions.read(sub["sub"])["result"])
            with sqlite3.connect(path) as db:
                db.execute("UPDATE message SET data=? WHERE id='reply'", (json.dumps({"role": "assistant", "parentID": "prompt", "time": {"completed": 2}, "finish": "stop"}),))
                db.execute("INSERT INTO part VALUES ('p','reply',?,2)", (json.dumps({"type": "text", "text": "finished"}),))
            self.daemon.poll_transcript_subscriptions()
        self.assertEqual(sessions.read(sub["sub"])["result"], "finished")


if __name__ == "__main__":
    unittest.main()
