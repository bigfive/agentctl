"""Cursor IDE fixtures matching the native composer/header/bubble schema."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentctl import discover
from agentctl.errors import UnsupportedOperationError
from agentctl.notify.cursor import CursorNotify


CHAT = "12345678-1234-1234-1234-123456789abc"
CHILD = "87654321-1234-1234-1234-123456789abc"


class CursorIdeSQLiteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.storage = self.home / "Library/Application Support/Cursor/User/globalStorage"
        self.storage.mkdir(parents=True)
        self.path = self.storage / "state.vscdb"
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB);
            CREATE TABLE composerHeaders (composerId TEXT PRIMARY KEY,
                lastUpdatedAt INTEGER, isArchived INTEGER, isSubagent INTEGER, value TEXT);
        """)

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO cursorDiskKV VALUES (?,?)",
                        (key, json.dumps(value)))
        self.db.commit()

    def chat(self, cid=CHAT, headers=None, **extra):
        metadata = {"name": "IDE task", "lastUpdatedAt": 1791262000123,
                    "isArchived": True,
                    "workspaceIdentifier": {"uri": {"fsPath": "/work/project"}}}
        metadata.update(extra)
        self.db.execute("INSERT OR REPLACE INTO composerHeaders VALUES (?,?,?,?,?)",
                        (cid, metadata["lastUpdatedAt"], metadata.get("isArchived", False),
                         metadata.get("isSubagent", False), json.dumps(metadata)))
        self.put("composerData:" + cid,
                 dict(metadata, fullConversationHeadersOnly=headers or []))

    def bubble(self, bid, type_, text, cid=CHAT, **extra):
        self.put("bubbleId:" + cid + ":" + bid,
                 dict(type=type_, text=text, bubbleId=bid, **extra))

    def test_discovers_native_metadata_archived_and_subagents_excludes_drafts(self):
        self.chat()
        self.chat(CHILD, isSubagent=True)
        self.chat("draft-" + CHAT, isDraft=True)
        rows = discover.discover(["cursor"], str(self.home))
        self.assertEqual(len(rows), 2)
        root = next(r for r in rows if r["native_id"] == CHAT)
        self.assertEqual(root["kind"], "cursor-ide")
        self.assertEqual(root["cwd"], "/work/project")
        self.assertAlmostEqual(root["modified_at"], 1791262000.123)
        self.assertTrue(root["archived"])
        self.assertTrue(root["capability"]["read"])
        self.assertFalse(root["capability"]["send"])
        self.assertFalse(root["capability"]["resume"])
        self.assertTrue(next(r for r in rows if r["native_id"] == CHILD)["is_subagent"])

    def test_history_uses_header_order_pagination_and_full_text(self):
        self.chat(headers=[{"bubbleId": b} for b in ["first", "second", "third"]])
        self.bubble("third", 1, "follow-up")
        self.bubble("orphan", 2, "abandoned branch")
        self.bubble("second", 2, "reply" * 2000)
        self.bubble("first", 1, "original prompt")
        history = discover.read_history("cursor", CHAT, 20, str(self.home))
        self.assertEqual([m["role"] for m in history], ["user", "assistant", "user"])
        self.assertEqual(history[1]["content"], "reply" * 2000)
        self.assertEqual(discover.read_history("cursor", CHAT, 1, str(self.home), offset=1), [history[1]])

    def test_raw_preserves_tool_bubbles_and_missing_messages(self):
        self.chat(headers=[{"bubbleId": "tool"}, {"bubbleId": "missing"}])
        self.bubble("tool", 2, "", toolFormerData={"name": "read_file"})
        records = discover.read_raw_history("cursor", CHAT, str(self.home))
        self.assertEqual(records[1]["value"]["toolFormerData"]["name"], "read_file")
        self.assertIn("missing", records[2])
        history = discover.read_history("cursor", CHAT, 20, str(self.home))
        self.assertEqual(history[0]["bubble_id"], "missing")

    def test_inline_conversation_map(self):
        self.chat()
        self.put("composerData:" + CHAT, {
            "fullConversationHeadersOnly": [{"bubbleId": "inline"}],
            "conversationMap": {"inline": {"type": 1, "text": "in-memory persisted message"}}})
        self.assertEqual(discover.read_history("cursor", CHAT, 1, str(self.home))[0]["content"],
                         "in-memory persisted message")

    def test_legacy_without_header_table(self):
        self.db.execute("DROP TABLE composerHeaders")
        self.put("composerData:" + CHAT, {"name": "Legacy IDE", "createdAt": 1000,
            "conversation": [{"type": 1, "text": "old prompt"}, {"type": 2, "text": "old reply"}]})
        rows = discover.discover(["cursor"], str(self.home))
        self.assertEqual(rows[0]["title"], "Legacy IDE")
        self.assertEqual([m["content"] for m in discover.read_history("cursor", CHAT, 5, str(self.home))],
                         ["old prompt", "old reply"])

    def test_unknown_order_is_labelled(self):
        self.put("composerData:" + CHAT, {"name": "Unknown ordering"})
        self.bubble("a", 1, "prompt")
        self.assertEqual(discover.read_history("cursor", CHAT, 1, str(self.home))[0]["ordering"], "unknown")

    def test_sqlite_ide_wins_over_cli_copy(self):
        self.chat(headers=[{"bubbleId": "ide"}])
        self.bubble("ide", 1, "real IDE conversation")
        cli = self.home / ".cursor/chats/workspace" / CHAT
        cli.mkdir(parents=True)
        (cli / "meta.json").write_text(json.dumps({"title": "CLI copy"}))
        rows = discover.discover(["cursor"], str(self.home))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "cursor-ide")
        self.assertEqual(discover.read_history("cursor", CHAT, 1, str(self.home))[0]["content"],
                         "real IDE conversation")

    def test_uncached_ide_does_not_return_a_separate_cli_transcript(self):
        self.chat()
        self.db.execute("DELETE FROM cursorDiskKV")
        self.db.commit()
        cli = self.home / ".cursor/chats/workspace" / CHAT
        cli.mkdir(parents=True)
        with mock.patch("agentctl.discover._cursor_store_messages", return_value=[
                {"role": "assistant", "content": "unrelated CLI copy"}]) as reader:
            self.assertEqual(discover.read_history("cursor", CHAT, 20, str(self.home)), [])
            reader.assert_not_called()

    def test_index_only_records_are_explicitly_unreadable(self):
        db = sqlite3.connect(self.storage / "conversation-search.db")
        db.execute("CREATE TABLE conversations(source TEXT,id TEXT,title TEXT,updated_at INTEGER,is_archived INTEGER)")
        db.executemany("INSERT INTO conversations VALUES (?,?,?,?,?)", [
            ("local", CHAT, "Cold local chat", 1791262000000, 0),
            ("cloud-cache", "bc-cloud", "Cloud chat", 1791262000000, 1)])
        db.commit()
        db.close()
        rows = discover.discover(["cursor"], str(self.home))
        self.assertEqual({r["kind"] for r in rows}, {"cursor-ide", "cursor-cloud"})
        self.assertTrue(all(not r["capability"]["read"] for r in rows))
        self.assertTrue(discover.is_ide_chat(CHAT, home=str(self.home)))
        self.assertFalse(discover.read_raw_history("cursor", CHAT, str(self.home))[0]["history_available"])

    def test_recognises_sqlite_ide_for_send_and_notification_guards(self):
        self.chat()
        self.assertTrue(discover.is_ide_chat(CHAT, home=str(self.home)))
        with mock.patch("agentctl.discover._cursor_ide_databases", return_value=[str(self.path)]):
            with self.assertRaises(UnsupportedOperationError):
                CursorNotify().validate(CHAT)

    def test_reads_live_wal_and_does_not_modify_source_database(self):
        self.db.execute("PRAGMA journal_mode=WAL")
        self.chat(headers=[{"bubbleId": "wal"}])
        self.bubble("wal", 1, "latest uncheckpointed message")
        before = {p: p.read_bytes() for p in [self.path, Path(str(self.path) + "-wal")]}
        self.assertEqual(discover.read_history("cursor", CHAT, 1, str(self.home))[0]["content"],
                         "latest uncheckpointed message")
        discover.discover(["cursor"], str(self.home))
        self.assertEqual(before, {p: p.read_bytes() for p in before})


if __name__ == "__main__":
    unittest.main()
