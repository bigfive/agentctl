"""Persistent local conversation search. Source stores are always read-only."""
import contextlib
import hashlib
import json
import os
import sqlite3
import threading
import time

from . import config, discover
from .errors import UsageError

REFRESH_SECONDS = 60
INDEX_FORMAT = 1


class SearchIndex:
    def __init__(self, path=None, source_home=None):
        self.path = path or os.path.join(config.home(), "search.sqlite3")
        self.source_home = source_home
        self.lock = threading.Lock()
        self.refreshing = False
        self.last_error = None
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    address TEXT PRIMARY KEY, harness TEXT, metadata TEXT,
                    fingerprint TEXT, state TEXT, error TEXT, indexed_at REAL);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, address TEXT, ordinal INTEGER,
                    role TEXT, timestamp TEXT, content TEXT);
                CREATE INDEX IF NOT EXISTS messages_address ON messages(address);
                CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
                    content, content='messages', content_rowid='id');
                CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                    INSERT INTO message_fts(rowid,content) VALUES(new.id,new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                    INSERT INTO message_fts(message_fts,rowid,content)
                        VALUES('delete',old.id,old.content);
                END;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS sources (
                    harness TEXT PRIMARY KEY, scanned_at REAL, error TEXT);
            """)
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def fingerprint(session):
        evidence = [INDEX_FORMAT, session]
        path = session.get("transcript_path") or session.get("source_path")
        if path:
            paths = [path, path + "-wal"]
            if os.path.isdir(path):
                paths += [os.path.join(path, f) for f in os.listdir(path)
                          if f.endswith((".db", ".md", ".json")) or f.endswith("-wal")]
            for candidate in paths:
                try:
                    stat = os.stat(candidate)
                    evidence.append((candidate, stat.st_size, stat.st_mtime_ns))
                except OSError:
                    evidence.append((candidate, "missing"))
        return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()

    def refresh(self, force=False, include_tools=None):
        """Atomically replace changed conversations, preserving old data on errors."""
        with self.lock:
            self.refreshing = True
            try:
                with self.db() as db:
                    setting = db.execute("SELECT value FROM settings WHERE key='include_tools'").fetchone()
                    old_tools = bool(setting and setting[0] == "true")
                tools = old_tools if include_tools is None else include_tools
                force = force or tools != old_tools
                changed = 0
                for harness in discover.DISCOVERY_SOURCES:
                    try:
                        self.probe_source(harness)
                        rows = discover.discover([harness], home=self.source_home)
                    except Exception as exc:
                        with self.db() as db:
                            db.execute("INSERT OR REPLACE INTO sources VALUES (?,?,?)",
                                       (harness, time.time(), str(exc)))
                        continue
                    seen = set()
                    for session in rows:
                        address = session["address"]
                        seen.add(address)
                        fingerprint = self.fingerprint(session)
                        with self.db() as db:
                            previous = db.execute("SELECT fingerprint,state FROM sessions WHERE address=?",
                                                  (address,)).fetchone()
                        if not force and previous and previous[0] == fingerprint and previous[1] not in ("error", "partial"):
                            continue
                        metadata = json.dumps(session)
                        state, error, messages = "indexed", None, []
                        try:
                            if not session.get("capability", {}).get("read"):
                                state = "unreadable"
                                error = session.get("capability", {}).get("read_reason", "no local transcript available")
                            else:
                                path = session.get("transcript_path")
                                if path and os.path.isfile(path) and path.endswith(".jsonl"):
                                    with open(path, "rb") as stream:
                                        stream.read(1)
                                messages = discover.read_history(harness, session["native_id"],
                                    limit=1000000000, home=self.source_home)
                                if any((m.get("bubble_id") and m.get("role") == "system") or
                                       m.get("content", "").startswith("[raw] store.db found but could not be read") for m in messages):
                                    state, error = "partial", "some native message bodies are not available locally"
                                if tools:
                                    messages += self.tool_messages(session)
                        except Exception as exc:
                            state, error = "error", str(exc)
                        with self.db() as db:
                            # Reader failures preserve searchable old history, explicitly stale.
                            if state != "error":
                                db.execute("DELETE FROM messages WHERE address=?", (address,))
                                docs = [(-1, {"role": "title", "content": session.get("title") or ""})]
                                docs += list(enumerate(messages))
                                db.executemany("INSERT INTO messages(address,ordinal,role,timestamp,content) VALUES (?,?,?,?,?)",
                                    [(address, ordinal, m.get("role", "unknown"), m.get("timestamp"), m["content"])
                                     for ordinal, m in docs if isinstance(m.get("content"), str) and m["content"]
                                     and (m.get("role") in ("user", "assistant", "title")
                                          or (tools and m.get("role") == "tool")
                                          or m["content"].startswith("[raw markdown]"))])
                            db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?)",
                                       (address, harness, metadata, fingerprint, state, error, time.time()))
                        changed += 1
                    with self.db() as db:
                        vanished = [r[0] for r in db.execute("SELECT address FROM sessions WHERE harness=?", (harness,))
                                    if r[0] not in seen]
                        for address in vanished:
                            db.execute("DELETE FROM messages WHERE address=?", (address,))
                            db.execute("DELETE FROM sessions WHERE address=?", (address,))
                        db.execute("INSERT OR REPLACE INTO sources VALUES (?,?,NULL)", (harness, time.time()))
                with self.db() as db:
                    db.execute("INSERT OR REPLACE INTO settings VALUES ('include_tools',?)", (json.dumps(tools),))
                    db.execute("INSERT OR REPLACE INTO settings VALUES ('last_refresh',?)", (str(time.time()),))
                self.last_error = None
                self.refreshing = False
                return {"changed_sessions": changed, "coverage": self.status()}
            except Exception as exc:
                self.last_error = str(exc)
                raise
            finally:
                self.refreshing = False

    def tool_messages(self, session):
        path = session.get("transcript_path")
        if path and path.endswith(".jsonl"):
            records = []
            with open(path, errors="replace") as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        elif session.get("kind") == "cursor-ide":
            records = discover._cursor_ide_records(session["native_id"], self.source_home) or []
        else:
            records = discover.read_raw_history(session["harness"], session["native_id"], home=self.source_home)
        messages = []
        for record in records:
            payload = record.get("payload") or {}
            if record.get("type") == "response_item" and payload.get("type") in (
                    "function_call_output", "custom_tool_call_output"):
                content = payload.get("output")
                if content:
                    messages.append({"role": "tool", "content": content if isinstance(content, str) else json.dumps(content),
                                     "timestamp": record.get("timestamp")})
            blocks = (record.get("message") or {}).get("content", [])
            if isinstance(blocks, list):
                for block in blocks:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        messages.append({"role": "tool", "content": discover._extract_text(block.get("content", ""))})
            bubble = record.get("value") or {}
            if bubble.get("toolFormerData"):
                messages.append({"role": "tool", "content": json.dumps(bubble["toolFormerData"]),
                                 "timestamp": bubble.get("createdAt")})
            for part in record.get("parts", []):
                if part.get("type") == "tool" and (part.get("state") or {}).get("output"):
                    messages.append({"role": "tool", "content": part["state"]["output"]})
        return messages

    def probe_source(self, harness):
        # Adapters may tolerate absent/broken stores. Search must report known
        # database failures instead of silently treating them as deletions.
        if harness == "cursor":
            paths = discover._cursor_ide_databases(self.source_home)
            paths += discover._cursor_ide_databases(self.source_home, "conversation-search.db")
            # CLI stores have a reader-owned database/WAL snapshot fallback;
            # a failed direct open alone is not an unavailable source.
            for path in paths:
                with discover._cursor_db(path) as db:
                    discover._cursor_tables(db)
        elif harness == "opencode":
            with discover._opencode_db(self.source_home) as db:
                if db is not None:
                    db.execute("SELECT count(*) FROM session").fetchone()

    def status(self):
        with self.db() as db:
            settings = {r[0]: r[1] for r in db.execute("SELECT key,value FROM settings")}
            sources = []
            for harness in discover.DISCOVERY_SOURCES:
                counts = {r[0]: r[1] for r in db.execute(
                    "SELECT state,count(*) FROM sessions WHERE harness=? GROUP BY state", (harness,))}
                source = db.execute("SELECT scanned_at,error FROM sources WHERE harness=?", (harness,)).fetchone()
                sources.append({"harness": harness, "counts": counts,
                    "scanned_at": source[0] if source else None, "error": source[1] if source else None})
            unavailable = [dict(r) for r in db.execute(
                "SELECT address,state,error FROM sessions WHERE state!='indexed'")]
            last = float(settings.get("last_refresh", 0)) or None
            return {"path": self.path, "last_refresh": last,
                "age_seconds": time.time() - last if last else None,
                "refreshing": self.refreshing, "error": self.last_error,
                "include_tools": settings.get("include_tools") == "true",
                "message_count": db.execute("SELECT count(*) FROM messages WHERE ordinal>=0").fetchone()[0],
                "sources": sources, "unavailable": unavailable,
                "incomplete": bool(unavailable or self.last_error or any(s["error"] or not s["scanned_at"] for s in sources))}

    def search(self, query, mode="literal", harness=None, limit=20, include_tools=False):
        if not isinstance(query, str) or not query.strip():
            raise UsageError("search query must not be empty")
        if mode not in ("literal", "full-text") or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise UsageError("search mode must be literal or full-text; limit must be between 1 and 1000")
        if harness and harness not in discover.DISCOVERY_SOURCES:
            raise UsageError("unknown search harness: %s" % harness)
        clauses, params = [], []
        if harness:
            clauses.append("s.harness=?")
            params.append(harness)
        if not include_tools:
            clauses.append("m.role!='tool'")
        if mode == "literal":
            clauses.append("instr(lower(m.content),lower(?))>0")
            params.append(query)
            source = "messages m"
            score = "0.0"
        else:
            # Treat input as words/phrases, not executable FTS query syntax.
            tokens = query.split()
            expression = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
            clauses.append("message_fts MATCH ?")
            params.append(expression)
            source = "message_fts JOIN messages m ON m.id=message_fts.rowid"
            score = "bm25(message_fts)"
        where = " AND ".join(clauses)
        # Fetch ranked hits then group by session; each result is a conversation.
        with self.db() as db:
            hits = db.execute("SELECT m.*,s.metadata,s.state," + score + " AS score FROM " + source +
                              " JOIN sessions s ON s.address=m.address WHERE " + where +
                              " ORDER BY CASE m.role WHEN 'user' THEN 0 WHEN 'title' THEN 1 WHEN 'assistant' THEN 2 ELSE 3 END,score,m.ordinal",
                              params).fetchall()
        results = {}
        for hit in hits:
            address = hit["address"]
            if address in results:
                results[address]["matching_messages"] += 1
                continue
            session = json.loads(hit["metadata"])
            content = hit["content"]
            pos = content.lower().find(query.lower())
            if pos < 0:
                pos = next((content.lower().find(t.lower()) for t in query.split() if t.lower() in content.lower()), 0)
            start = max(0, pos - 140)
            result = {"address": address, "title": session.get("title"), "harness": session["harness"],
                "kind": session.get("kind"), "is_subagent": session.get("is_subagent", False),
                "modified_at": session.get("modified_at"), "state": hit["state"],
                "role": hit["role"], "message_index": hit["ordinal"], "timestamp": hit["timestamp"],
                "excerpt": content[start:start + 500], "matching_messages": 1}
            if hit["role"] == "tool":
                result["read_command"] = "agentctl sessions history %s --raw" % address
            elif hit["ordinal"] >= 0:
                result["read_command"] = "agentctl sessions history %s --limit 5 --offset %s" % (
                    address, self.history_offset(address, hit["ordinal"]))
            results[address] = result
        coverage = self.status()
        return {"query": query, "mode": mode, "total_sessions": len(results),
                "results": list(results.values())[:limit], "coverage": coverage}

    def history_offset(self, address, ordinal):
        with self.db() as db:
            last = db.execute("SELECT max(ordinal) FROM messages WHERE address=? AND role!='tool' AND ordinal>=0",
                              (address,)).fetchone()[0]
        return max(0, (last or 0) - ordinal - 2)

    def maintain(self, stopping):
        while not stopping.is_set():
            try:
                self.refresh()
            except Exception:
                pass  # Exposed by search-status; never stops other daemon services.
            stopping.wait(REFRESH_SECONDS)
