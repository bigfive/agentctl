"""Tests for session discovery across harnesses.

Uses temporary filesystem trees matching the real on-disk formats:

Codex: rollout-<ISO-timestamp>-<uuid>.jsonl with event types
  session_meta, event_msg (payload.type in task_started, task_complete,
  turn_context), response_item.

Claude: <uuid>.jsonl with {type: "user"|"assistant", message: {content: [...]}}
  Root sessions in projects/<encoded>/<uuid>.jsonl.
  Subagent sessions in projects/<encoded>/<parent-uuid>/<child-uuid>.jsonl.

Cursor CLI: store.db (SQLite) with blobs(id, data) and meta(key, value).
  meta key=0: hex-encoded JSON with agentId, name, createdAt.
  blobs: JSON messages with role, content fields.

No live harness binaries or network calls are made.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from agentctl.discover import (
    discover, filter_sessions, read_history,
    _codex_sessions, _claude_sessions, _cursor_sessions,
    is_ide_chat,
)


def _make_file(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# Real Codex event shapes (observed from actual transcripts)
# ---------------------------------------------------------------------------

def _real_codex_events(thread_id, user_prompt="Fix the auth bug",
                        assistant_reply="Done, auth is fixed.", cwd=None):
    """Return a list of Codex JSONL lines matching the real format."""
    return [
        # session_meta: first event, carries session id and cwd
        {"timestamp": "2026-10-05T04:05:17.831Z", "ordinal": 0,
         "type": "session_meta",
         "payload": {
             "session_id": thread_id,
             "id": thread_id,
             "cwd": cwd or "/home/user/project",
             "originator": "Codex Desktop",
             "cli_version": "0.160.0",
         }},
        # turn_context: user prompt for the first turn
        {"timestamp": "2026-10-05T04:05:20.000Z", "ordinal": 1,
         "type": "event_msg",
         "payload": {
             "type": "turn_context",
             "turn_id": thread_id + "-turn1",
             "root_turn_id": thread_id + "-turn1",
             "input": [{"type": "text", "text": user_prompt}],
         }},
        # task_started
        {"timestamp": "2026-10-05T04:05:21.000Z", "ordinal": 2,
         "type": "event_msg",
         "payload": {
             "type": "task_started",
             "turn_id": thread_id + "-turn1",
             "root_turn_id": thread_id + "-turn1",
             "started_at": 1791173117,
         }},
        # task_complete: assistant's reply
        {"timestamp": "2026-10-05T04:05:30.000Z", "ordinal": 3,
         "type": "event_msg",
         "payload": {
             "type": "task_complete",
             "turn_id": thread_id + "-turn1",
             "last_agent_message": assistant_reply,
             "started_at": 1791173117,
             "completed_at": 1791173127,
         }},
    ]


class TestCodexDiscovery(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="agentctl-disc-codex-")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def _make_session(self, thread_id, events=None, mtime=None, archived=False):
        if archived:
            path = os.path.join(
                self.home, ".codex", "archived_sessions",
                "rollout-2026-10-05T04-05-05-%s.jsonl" % thread_id)
        else:
            path = os.path.join(
                self.home, ".codex", "sessions", "2026", "10", "05",
                "rollout-2026-10-05T04-05-05-%s.jsonl" % thread_id)
        lines = [json.dumps(e) for e in (events or [])]
        _make_file(path, "\n".join(lines) + "\n")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_discovers_codex_session_with_timestamp_prefix(self):
        """Filename rollout-<ISO>-<uuid>.jsonl is parsed correctly."""
        tid = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        self._make_session(tid)
        sessions = _codex_sessions(self.home)
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s["harness"], "codex")
        self.assertEqual(s["kind"], "codex")
        self.assertEqual(s["native_id"], tid)
        self.assertEqual(s["address"], "codex:%s" % tid)

    def test_enriches_cwd_from_session_meta(self):
        tid = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        events = _real_codex_events(tid, cwd="/home/user/myproject")
        self._make_session(tid, events)
        sessions = _codex_sessions(self.home)
        self.assertEqual(sessions[0].get("cwd"), "/home/user/myproject")

    def test_enriches_title_from_turn_context(self):
        """Title comes from turn_context.input text, not fabricated event types."""
        tid = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        events = _real_codex_events(tid, user_prompt="Debug the flaky test")
        self._make_session(tid, events)
        sessions = _codex_sessions(self.home)
        self.assertEqual(sessions[0].get("title"), "Debug the flaky test")

    def test_discovers_archived_sessions(self):
        """Sessions in archived_sessions/ are also discovered."""
        tid = "01a07a1e-c33d-70b3-b0a0-d49304f192b4"
        self._make_session(tid, archived=True)
        sessions = _codex_sessions(self.home)
        self.assertTrue(any(s["native_id"] == tid for s in sessions))
        archived = next(s for s in sessions if s["native_id"] == tid)
        self.assertTrue(archived.get("archived"))

    def test_session_index_enriches_name(self):
        """session_index.jsonl provides thread_name without opening transcript."""
        tid = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        self._make_session(tid)
        index_path = os.path.join(self.home, ".codex", "session_index.jsonl")
        _make_file(index_path, json.dumps({
            "id": tid,
            "thread_name": "Review Francis PR",
            "updated_at": "2026-10-05T04:05:30.000Z",
        }) + "\n")
        sessions = _codex_sessions(self.home)
        self.assertEqual(sessions[0].get("title"), "Review Francis PR")

    def test_multiple_sessions_newest_first(self):
        older = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        newer = "ffffffff-eeee-4ddd-8ccc-bbbbbbbbbbbb"
        self._make_session(older, mtime=1000.0)
        self._make_session(newer, mtime=2000.0)
        sessions = _codex_sessions(self.home)
        self.assertEqual(sessions[0]["native_id"], newer)
        self.assertEqual(sessions[1]["native_id"], older)

    def test_no_sessions_returns_empty(self):
        sessions = _codex_sessions(self.home)
        self.assertEqual(sessions, [])

    def test_malformed_lines_skipped(self):
        tid = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        events = _real_codex_events(tid)
        self._make_session(tid, events)
        # Append corrupt line
        path = os.path.join(
            self.home, ".codex", "sessions", "2026", "10", "05",
            "rollout-2026-10-05T04-05-05-%s.jsonl" % tid)
        with open(path, "a") as f:
            f.write("{bad json line\n")
        sessions = _codex_sessions(self.home)
        self.assertEqual(len(sessions), 1)


class TestCodexHistory(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="agentctl-hist-codex-")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def _make_transcript(self, thread_id, events):
        path = os.path.join(
            self.home, ".codex", "sessions", "2026", "10", "05",
            "rollout-2026-10-05T04-05-05-%s.jsonl" % thread_id)
        _make_file(path, "\n".join(json.dumps(e) for e in events) + "\n")
        return path

    def test_history_extracts_user_and_assistant_messages(self):
        tid = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        events = _real_codex_events(tid, "User asks", "Assistant replies")
        self._make_transcript(tid, events)
        msgs = read_history("codex", tid, limit=50, home=self.home)
        roles = [m["role"] for m in msgs]
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)
        user_msg = next(m for m in msgs if m["role"] == "user")
        self.assertEqual(user_msg["content"], "User asks")
        asst_msg = next(m for m in msgs if m["role"] == "assistant")
        self.assertEqual(asst_msg["content"], "Assistant replies")

    def test_history_multi_turn(self):
        tid = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        events = []
        for i in range(3):
            events.extend([
                {"type": "event_msg", "payload": {
                    "type": "turn_context",
                    "turn_id": "turn-%d" % i,
                    "input": [{"type": "text", "text": "Question %d" % i}],
                }},
                {"type": "event_msg", "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-%d" % i,
                    "last_agent_message": "Answer %d" % i,
                }},
            ])
        self._make_transcript(tid, events)
        msgs = read_history("codex", tid, limit=50, home=self.home)
        self.assertEqual(len(msgs), 6)

    def test_limit_respected(self):
        tid = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        events = []
        for i in range(10):
            events.append({"type": "event_msg", "payload": {
                "type": "turn_context",
                "turn_id": "t%d" % i,
                "input": [{"type": "text", "text": "q%d" % i}],
            }})
        self._make_transcript(tid, events)
        msgs = read_history("codex", tid, limit=3, home=self.home)
        self.assertLessEqual(len(msgs), 3)

    def test_missing_session_returns_empty(self):
        msgs = read_history("codex", "00000000-0000-4000-8000-000000000000",
                            home=self.home)
        self.assertEqual(msgs, [])


class TestClaudeDiscovery(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="agentctl-disc-claude-")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def _make_session(self, session_id, events=None, project="default"):
        encoded = "-Users-test-%s" % project
        path = os.path.join(self.home, ".claude", "projects",
                            encoded, "%s.jsonl" % session_id)
        lines = [json.dumps(e) for e in (events or [])]
        _make_file(path, "\n".join(lines) + "\n")
        return path

    def _make_subagent(self, parent_id, child_id, events=None, project="default"):
        encoded = "-Users-test-%s" % project
        path = os.path.join(self.home, ".claude", "projects",
                            encoded, parent_id, "%s.jsonl" % child_id)
        lines = [json.dumps(e) for e in (events or [])]
        _make_file(path, "\n".join(lines) + "\n")
        return path

    def _real_claude_events(self, user_text="Fix the bug"):
        return [
            {"type": "user",
             "message": {"content": [{"type": "text", "text": user_text}]}},
            {"type": "assistant",
             "message": {"content": [{"type": "text",
                                       "text": "Fixed."}]}},
        ]

    def test_discovers_root_session_by_uuid_filename(self):
        sid = "12345678-1234-4123-8123-123456789abc"
        self._make_session(sid, self._real_claude_events())
        sessions = _claude_sessions(self.home)
        root = [s for s in sessions if not s.get("is_subagent")]
        self.assertEqual(len(root), 1)
        self.assertEqual(root[0]["native_id"], sid)
        self.assertEqual(root[0]["kind"], "claude")

    def test_enriches_title_from_first_user_message(self):
        sid = "12345678-1234-4123-8123-123456789abc"
        self._make_session(sid, self._real_claude_events("Review this PR"))
        sessions = _claude_sessions(self.home)
        root = [s for s in sessions if not s.get("is_subagent")]
        self.assertEqual(root[0].get("title"), "Review this PR")

    def test_subagent_classified_separately(self):
        """Subagent transcripts (under parent UUID dir) are classified as subagent."""
        parent_id = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
        child_id = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
        self._make_session(parent_id, self._real_claude_events("Parent task"))
        self._make_subagent(parent_id, child_id,
                            self._real_claude_events("Child task"))
        sessions = _claude_sessions(self.home)
        root = [s for s in sessions if not s.get("is_subagent")]
        subs = [s for s in sessions if s.get("is_subagent")]
        self.assertEqual(len(root), 1)
        self.assertEqual(len(subs), 1)
        self.assertEqual(root[0]["native_id"], parent_id)
        self.assertEqual(subs[0]["native_id"], child_id)
        self.assertEqual(subs[0]["kind"], "claude-subagent")
        self.assertEqual(subs[0].get("parent_session_id"), parent_id)

    def test_subagent_does_not_overwrite_root(self):
        """A subagent sharing parent's session_id in events doesn't corrupt root."""
        parent_id = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
        child_id = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
        self._make_session(parent_id, self._real_claude_events("Root task"))
        # Subagent transcript may reference parent's session ID internally
        self._make_subagent(parent_id, child_id, [
            {"type": "user",
             "message": {"content": [{"type": "text",
                                       "text": "Sub task"}]},
             "sessionId": parent_id},  # references parent
        ])
        sessions = _claude_sessions(self.home)
        root = [s for s in sessions if not s.get("is_subagent")]
        # Root session's title must be from the root transcript
        self.assertEqual(root[0].get("title"), "Root task")

    def test_no_sessions_empty(self):
        self.assertEqual(_claude_sessions(self.home), [])


class TestCursorDiscovery(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="agentctl-disc-cursor-")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def _make_cli_session(self, chat_id, blobs=None, meta_json=None,
                           meta_json_file=None):
        """Create a Cursor CLI session with a real SQLite store.db."""
        ws_hash = "abc123ws"
        chat_dir = os.path.join(self.home, ".cursor", "chats", ws_hash, chat_id)
        os.makedirs(chat_dir, exist_ok=True)

        db_path = os.path.join(chat_dir, "store.db")
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")

        # meta key=0: hex-encoded JSON matching real format
        meta_obj = meta_json or {
            "agentId": chat_id,
            "latestRootBlobId": "abc123",
            "name": "New Agent",
            "createdAt": 1791172356491,
        }
        meta_hex = json.dumps(meta_obj).encode().hex()
        con.execute("INSERT INTO meta VALUES ('0', ?)", (meta_hex,))

        for i, blob_obj in enumerate(blobs or []):
            blob_id = "blob%04d" % i
            con.execute("INSERT INTO blobs VALUES (?, ?)",
                        (blob_id, json.dumps(blob_obj)))
        con.commit()
        con.close()

        if meta_json_file:
            _make_file(os.path.join(chat_dir, "meta.json"),
                       json.dumps(meta_json_file))
        return chat_dir

    def test_discovers_cursor_cli_session(self):
        chat_id = "5a4f300a-61b1-4975-82e2-aa4be275aa49"
        self._make_cli_session(chat_id)
        sessions = _cursor_sessions(self.home)
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s["harness"], "cursor")
        self.assertEqual(s["kind"], "cursor-cli")
        self.assertEqual(s["native_id"], chat_id)

    def test_enriches_name_from_sqlite_meta(self):
        chat_id = "5a4f300a-61b1-4975-82e2-aa4be275aa49"
        meta = {
            "agentId": chat_id,
            "name": "Refactor auth module",
            "createdAt": 1791172356491,
        }
        self._make_cli_session(chat_id, meta_json=meta)
        sessions = _cursor_sessions(self.home)
        self.assertEqual(sessions[0].get("title"), "Refactor auth module")

    def test_enriches_title_from_first_user_blob(self):
        chat_id = "5a4f300a-61b1-4975-82e2-aa4be275aa49"
        blobs = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": [{"type": "text",
                                           "text": "Fix the login bug"}]},
            {"role": "assistant", "content": "Fixed!"},
        ]
        self._make_cli_session(chat_id, blobs=blobs)
        sessions = _cursor_sessions(self.home)
        self.assertIn(sessions[0].get("title"), [
            "Fix the login bug", "Refactor auth module", "New Agent",
        ])

    def test_meta_json_file_enriches_cwd(self):
        chat_id = "5a4f300a-61b1-4975-82e2-aa4be275aa49"
        self._make_cli_session(chat_id, meta_json_file={
            "workdir": "/home/user/myproject",
            "title": "Debug session",
        })
        sessions = _cursor_sessions(self.home)
        self.assertEqual(sessions[0].get("cwd"), "/home/user/myproject")

    def test_non_uuid_directories_skipped(self):
        ws_hash = "abc123ws"
        not_uuid = os.path.join(self.home, ".cursor", "chats", ws_hash, "not-a-uuid")
        os.makedirs(not_uuid, exist_ok=True)
        sessions = _cursor_sessions(self.home)
        self.assertEqual(len(sessions), 0)

    def test_is_ide_chat_false_for_cli_session(self):
        self.assertFalse(is_ide_chat("5a4f300a-61b1-4975-82e2-aa4be275aa49"))

    def test_no_sessions_empty(self):
        sessions = _cursor_sessions(self.home)
        self.assertEqual(sessions, [])


class TestCursorHistory(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="agentctl-hist-cursor-")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def _make_store(self, chat_id, blobs):
        ws_hash = "abc123ws"
        chat_dir = os.path.join(self.home, ".cursor", "chats", ws_hash, chat_id)
        os.makedirs(chat_dir, exist_ok=True)
        db_path = os.path.join(chat_dir, "store.db")
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        meta_hex = json.dumps({"agentId": chat_id}).encode().hex()
        con.execute("INSERT INTO meta VALUES ('0', ?)", (meta_hex,))
        for i, b in enumerate(blobs):
            con.execute("INSERT INTO blobs VALUES (?, ?)",
                        ("blob%04d" % i, json.dumps(b)))
        con.commit()
        con.close()

    def test_history_reads_sqlite_blobs(self):
        chat_id = "5a4f300a-61b1-4975-82e2-aa4be275aa49"
        self._make_store(chat_id, [
            {"role": "user", "content": "Hello Cursor"},
            {"role": "assistant", "content": "Hi!"},
        ])
        msgs = read_history("cursor", chat_id, limit=50, home=self.home)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["content"], "Hello Cursor")
        self.assertEqual(msgs[1]["role"], "assistant")

    def test_history_content_list_decoded(self):
        chat_id = "5a4f300a-61b1-4975-82e2-aa4be275aa49"
        self._make_store(chat_id, [
            {"role": "user",
             "content": [{"type": "text", "text": "Explain this"}]},
        ])
        msgs = read_history("cursor", chat_id, limit=50, home=self.home)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["content"], "Explain this")

    def test_missing_session_returns_fallback(self):
        chat_id = "5a4f300a-61b1-4975-82e2-aa4be275aa49"
        msgs = read_history("cursor", chat_id, limit=50, home=self.home)
        # Should return empty (no store.db), not raise
        self.assertIsInstance(msgs, list)


class TestFilterSessions(unittest.TestCase):
    def _sessions(self):
        return [
            {"address": "codex:thread1", "title": "Fix auth bug",
             "harness": "codex", "modified_at": 2000},
            {"address": "claude:session2", "title": "Write tests",
             "harness": "claude", "modified_at": 1500},
            {"address": "cursor:chat3", "cwd": "/home/user/project",
             "harness": "cursor", "modified_at": 1000},
        ]

    def test_filter_by_address(self):
        results = filter_sessions(self._sessions(), "codex")
        self.assertEqual(len(results), 1)

    def test_filter_by_title(self):
        results = filter_sessions(self._sessions(), "auth")
        self.assertEqual(len(results), 1)
        self.assertIn("auth", results[0]["title"])

    def test_filter_by_cwd(self):
        results = filter_sessions(self._sessions(), "project")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["harness"], "cursor")

    def test_filter_case_insensitive(self):
        results = filter_sessions(self._sessions(), "TESTS")
        self.assertEqual(len(results), 1)

    def test_no_match_returns_empty(self):
        self.assertEqual(filter_sessions(self._sessions(), "zzznomatch"), [])


class TestDiscoverIntegration(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="agentctl-disc-int-")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_discover_across_multiple_harnesses(self):
        codex_id = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        _make_file(os.path.join(
            self.home, ".codex", "sessions", "2026", "10", "05",
            "rollout-2026-10-05T04-00-00-%s.jsonl" % codex_id), "")

        claude_id = "12345678-1234-4123-8123-123456789abc"
        _make_file(os.path.join(
            self.home, ".claude", "projects", "-test",
            "%s.jsonl" % claude_id), "")

        sessions = discover(home=self.home)
        harnesses = {s["harness"] for s in sessions}
        self.assertIn("codex", harnesses)
        self.assertIn("claude", harnesses)

    def test_harness_filter(self):
        codex_id = "01a07a1e-c33d-70b3-b0a0-d49304f192b3"
        _make_file(os.path.join(
            self.home, ".codex", "sessions", "2026", "10", "05",
            "rollout-2026-10-05T04-00-00-%s.jsonl" % codex_id), "")
        claude_id = "12345678-1234-4123-8123-123456789abc"
        _make_file(os.path.join(
            self.home, ".claude", "projects", "-test",
            "%s.jsonl" % claude_id), "")

        sessions = discover(harnesses=["codex"], home=self.home)
        self.assertTrue(all(s["harness"] == "codex" for s in sessions))


if __name__ == "__main__":
    unittest.main(verbosity=2)
