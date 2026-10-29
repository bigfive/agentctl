import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentctl.search import SearchIndex
from agentctl.errors import UsageError
from agentctl import ipc, cli


class SearchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.index = SearchIndex(str(self.home / "search.db"), source_home=str(self.home))
        self.rows = []
        self.histories = {}
        self.discovery = mock.patch("agentctl.search.discover.discover", side_effect=lambda hs, home=None:
                                    [r for r in self.rows if r["harness"] in hs])
        self.reader = mock.patch("agentctl.search.discover.read_history", side_effect=lambda h, sid, **kw:
                                 self.histories.get(h + ":" + sid, []))
        self.discovery.start()
        self.reader_mock = self.reader.start()
        self.addCleanup(self.discovery.stop)
        self.addCleanup(self.reader.stop)

    def add(self, address, text, role="user", **extra):
        h, sid = address.split(":", 1)
        row = {"address": address, "harness": h, "native_id": sid, "kind": h,
               "title": "A task", "modified_at": 1, "capability": {"read": True}}
        row.update(extra)
        self.rows.append(row)
        self.histories[address] = [{"role": role, "content": text}]
        return row

    def test_literal_urls_are_exact_and_user_prompts_rank_above_mentions(self):
        url = "https://github.com/coreplan/coreplan-web/pull/5100"
        self.add("codex:list", "Open PRs: " + url, role="assistant")
        self.add("cursor:review", "Please review " + url)
        self.add("claude:other", url.replace("5100", "5101"))
        self.index.refresh()
        result = self.index.search(url)
        self.assertEqual(result["results"][0]["address"], "cursor:review")
        self.assertIn(url, result["results"][0]["excerpt"])
        self.assertFalse(result["coverage"]["incomplete"])

    def test_full_text_works_across_harnesses_with_filter(self):
        self.add("claude:one", "review the upload of CSV data")
        self.add("codex:two", "review CSV upload")
        self.index.refresh()
        result = self.index.search("review CSV upload", mode="full-text", harness="claude")
        self.assertEqual(result["total_sessions"], 1)
        self.assertEqual(result["results"][0]["address"], "claude:one")

    def test_refresh_updates_only_changed_conversations_and_removes_deleted(self):
        row = self.add("codex:one", "old branch")
        self.add("cursor:two", "untouched")
        self.index.refresh()
        self.reader_mock.reset_mock()
        self.assertEqual(self.index.refresh()["changed_sessions"], 0)
        self.reader_mock.assert_not_called()
        row["modified_at"] = 2
        self.histories["codex:one"] = [{"role": "user", "content": "new branch"}]
        self.assertEqual(self.index.refresh()["changed_sessions"], 1)
        self.assertEqual(self.index.search("old branch")["total_sessions"], 0)
        self.rows.clear()
        self.index.refresh()
        self.assertEqual(self.index.search("new branch")["total_sessions"], 0)

    def test_unreadable_titles_are_searchable_with_explicit_incomplete_coverage(self):
        self.add("cursor:bc-cloud", "", title="Review PR 5100", capability={"read": False}, kind="cursor-cloud")
        self.index.refresh()
        result = self.index.search("5100")
        self.assertEqual(result["results"][0]["role"], "title")
        self.assertTrue(result["coverage"]["incomplete"])
        self.assertEqual(result["coverage"]["unavailable"][0]["state"], "unreadable")

    def test_failed_reader_preserves_old_data_and_reports_error(self):
        row = self.add("codex:one", "previous searchable result")
        self.index.refresh()
        row["modified_at"] = 2
        self.reader_mock.side_effect = OSError("source unavailable")
        self.index.refresh()
        result = self.index.search("searchable")
        self.assertEqual(result["total_sessions"], 1)
        self.assertTrue(result["coverage"]["incomplete"])
        self.assertEqual(result["results"][0]["state"], "error")

    def test_discovery_failure_is_reported_without_deleting_old_sessions(self):
        self.add("codex:one", "preserved")
        self.index.refresh()
        with mock.patch("agentctl.search.discover.discover", side_effect=OSError("locked")):
            self.index.refresh()
        result = self.index.search("preserved")
        self.assertEqual(result["total_sessions"], 1)
        self.assertTrue(all(s["error"] == "locked" for s in result["coverage"]["sources"]))

    def test_index_survives_restart_and_supports_grouped_results(self):
        self.add("codex:one", "needle")
        self.histories["codex:one"].append({"role": "assistant", "content": "needle again"})
        self.index.refresh()
        restarted = SearchIndex(self.index.path)
        result = restarted.search("needle")
        self.assertEqual(result["total_sessions"], 1)
        self.assertEqual(result["results"][0]["matching_messages"], 2)
        self.assertIn("--offset 0", result["results"][0]["read_command"])

    def test_native_tool_output_is_opt_in(self):
        self.add("codex:one", "normal prompt")
        raw = [{"type": "response_item", "payload": {"type": "custom_tool_call_output", "output": "tool-only needle"}}]
        with mock.patch("agentctl.search.discover.read_raw_history", return_value=raw):
            self.index.refresh(include_tools=True)
        self.assertEqual(self.index.search("tool-only")["total_sessions"], 0)
        result = self.index.search("tool-only", include_tools=True)
        self.assertEqual(result["results"][0]["role"], "tool")
        self.assertIn("--raw", result["results"][0]["read_command"])

    def test_source_file_changes_invalidate_unchanged_session_metadata(self):
        source = self.home / "session.jsonl"
        source.write_text("{}\n")
        self.add("codex:one", "old", transcript_path=str(source))
        self.index.refresh()
        source.write_text("{}\n{}\n")
        self.histories["codex:one"] = [{"role": "user", "content": "new appended prompt"}]
        self.assertEqual(self.index.refresh()["changed_sessions"], 1)
        self.assertEqual(self.index.search("new appended")["total_sessions"], 1)

    def test_missing_native_messages_report_partial_coverage(self):
        self.add("cursor:one", "available")
        self.histories["cursor:one"].append({"role": "system", "bubble_id": "missing", "content": "unavailable"})
        self.index.refresh()
        self.assertEqual(self.index.status()["unavailable"][0]["state"], "partial")

    def test_system_instructions_do_not_pollute_conversation_search(self):
        self.add("codex:one", "actual prompt")
        self.histories["codex:one"].append({"role": "developer", "content": "hidden instructions needle"})
        self.index.refresh()
        self.assertEqual(self.index.search("hidden instructions")["total_sessions"], 0)

    def test_real_codex_and_opencode_stores_are_indexed(self):
        import sqlite3
        self.discovery.stop()
        self.reader.stop()
        sid = "12345678-1234-1234-1234-123456789abc"
        path = self.home / ".codex/sessions/2026/10/06" / ("rollout-2026-10-06T01-00-00-" + sid + ".jsonl")
        path.parent.mkdir(parents=True)
        events = [{"type": "session_meta", "payload": {"id": sid, "cwd": "/repo"}},
                  {"type": "response_item", "payload": {"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": "review PR 5100"}]}}]
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        directory = self.home / ".local/share/opencode"
        directory.mkdir(parents=True)
        with sqlite3.connect(directory / "opencode.db") as db:
            db.executescript("""
                CREATE TABLE session(id TEXT,title TEXT,directory TEXT,time_updated INTEGER);
                CREATE TABLE message(id TEXT,session_id TEXT,data TEXT,time_created INTEGER);
                CREATE TABLE part(message_id TEXT,data TEXT,time_created INTEGER,id TEXT);
            """)
            db.execute("INSERT INTO session VALUES ('ses_native','Native session','/repo',1000)")
            db.execute("INSERT INTO message VALUES ('msg_native','ses_native',?,1000)", (json.dumps({"role": "user"}),))
            db.execute("INSERT INTO part VALUES ('msg_native',?,1000,'part_native')", (json.dumps({"type": "text", "text": "review PR 5100"}),))
        with mock.patch("agentctl.discover._opencode_sessions_http", return_value=[]), mock.patch(
                "agentctl.discover._opencode_history_http", return_value=None):
            self.index.refresh()
        result = self.index.search("5100")
        self.assertEqual({r["harness"] for r in result["results"]}, {"codex", "opencode"})
        self.assertFalse(result["coverage"]["incomplete"])

    def test_validation_and_punctuation_do_not_expose_fts_syntax(self):
        self.index.refresh()
        for query in ["", " "]:
            with self.assertRaises(UsageError):
                self.index.search(query)
        with self.assertRaises(UsageError):
            self.index.search("x", limit=-1)
        self.assertEqual(self.index.search('" OR *', mode="full-text")["total_sessions"], 0)

    def test_cli_and_ipc_search_commands(self):
        # Parse the actual public API and verify forwarding, rather than shell internals.
        parser = cli._build_parser()
        args = parser.parse_args(["sessions", "search", "5100", "--harness", "cursor", "--json"])
        self.assertEqual(args.query, "5100")
        daemon = mock.Mock()
        daemon.search_sessions.return_value = {"results": []}
        result = ipc.dispatch(daemon, {"op": "session_search", "query": "5100", "harness": "cursor"})
        self.assertTrue(result["ok"])
        daemon.search_sessions.assert_called_once_with(query="5100", mode="literal", harness="cursor", limit=20, include_tools=False)


if __name__ == "__main__":
    unittest.main()
