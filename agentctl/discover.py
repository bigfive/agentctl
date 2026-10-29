"""Session discovery across supported harnesses.

Read-only: discovers sessions from local filesystem stores and CLI tools.
No harness binary is invoked for discovery except OpenCode's local CLI when
no HTTP server is reachable. No integration setup is required.

Codex filename format (real): rollout-<ISO-timestamp>-<uuid>.jsonl
Event types observed: session_meta, event_msg (with payload.type in
  task_started, task_complete, turn_context), response_item, world_state.
Session ID: session_meta.payload.id
Title: task_complete.payload.last_agent_message[:80] (first completed turn)
User prompt: turn_context.payload.input (list of content items)
Archived sessions: ~/.codex/archived_sessions/  (same filename format)
Session index: ~/.codex/session_index.jsonl  (id, thread_name, updated_at)

Cursor CLI store: ~/.cursor/chats/<ws-hash>/<chat-id>/store.db (SQLite)
  Tables: blobs(id TEXT PRIMARY KEY, data BLOB), meta(key TEXT PRIMARY KEY, value TEXT)
  meta row key=0: hex-encoded JSON with agentId, name, createdAt, latestRootBlobId
  blobs: JSON messages with role, content fields
Cursor IDE AgentStore (macOS): ~/Library/Application Support/Cursor/AgentStores/
  cursor_agent_stores/<uuid>/ - may contain Markdown transcripts
Cursor IDE SQLite: Cursor/User/globalStorage/state.vscdb
  composerHeaders: conversation metadata; cursorDiskKV: composerData:<id> and
  bubbleId:<composer-id>:<bubble-id>. fullConversationHeadersOnly orders bubbles.
  conversation-search.db contains additional local/cloud index references.

Claude: ~/.claude/projects/<encoded>/<session-uuid>.jsonl
  Root sessions are UUID-named files directly under a project directory.
  Subagent sessions: UUID file under a UUID-named subdirectory (parent session dir).
  Root events: {type: "user"|"assistant"|"result", message: {content: [...]}}

OpenCode (no HTTP server): opencode session list -> JSONL {id, thread_name, updated_at}
  opencode export <id> -> JSON with full message history
OpenCode HTTP (when available): GET /session, GET /session/:id/message (singular)
  Message format: {info: {role: ...}, parts: [{type: "text", text: "..."}]}
  Session time field: {updated: <epoch_ms>} dict
"""

from pathlib import Path
import glob
import json
import os
import re
import subprocess
import contextlib
import sqlite3
import urllib.parse


from . import config


UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

UUID_FULL_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# Codex rollout filename: rollout-<ISO-timestamp>-<uuid>.jsonl
# The UUID is always 36 chars at the end before .jsonl
CODEX_ROLLOUT_RE = re.compile(
    r"rollout-.*?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$")


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

def _codex_sessions(home=None):
    base = home or os.path.expanduser("~")
    sessions = {}

    # Active sessions
    active_pattern = os.path.join(base, ".codex", "sessions", "*", "*", "*",
                                   "rollout-*.jsonl")
    _scan_codex_files(glob.glob(active_pattern), sessions, archived=False)

    # Archived sessions (same filename format, different directory)
    archive_pattern = os.path.join(base, ".codex", "archived_sessions",
                                   "rollout-*.jsonl")
    _scan_codex_files(glob.glob(archive_pattern), sessions, archived=True)

    # Enrich from session_index.jsonl for metadata without opening transcripts
    _enrich_from_session_index(sessions, base)

    results = sorted(sessions.values(), key=lambda s: s["modified_at"],
                     reverse=True)
    for s in results:
        _enrich_codex(s)
    return results


def _scan_codex_files(paths, sessions, archived):
    for path in paths:
        m = CODEX_ROLLOUT_RE.search(os.path.basename(path))
        if not m:
            continue
        thread_id = m.group(1)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0
        existing = sessions.get(thread_id)
        if existing is None or mtime > existing["modified_at"]:
            sessions[thread_id] = {
                "harness": "codex",
                "kind": "codex",
                "native_id": thread_id,
                "address": "codex:%s" % thread_id,
                "transcript_path": path,
                "modified_at": mtime,
                "archived": archived,
            }


def _enrich_from_session_index(sessions, base):
    """Enrich discovered sessions with metadata from session_index.jsonl."""
    index_path = os.path.join(base, ".codex", "session_index.jsonl")
    try:
        with open(index_path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                sid = entry.get("id")
                if not sid or sid not in sessions:
                    continue
                name = entry.get("thread_name")
                if name:
                    sessions[sid].setdefault("title", name)
                updated = entry.get("updated_at")
                if updated and isinstance(updated, str):
                    try:
                        import datetime
                        dt = datetime.datetime.fromisoformat(
                            updated.replace("Z", "+00:00"))
                        sessions[sid].setdefault("modified_at",
                                                 dt.timestamp())
                    except (ValueError, TypeError):
                        pass
    except IOError:
        pass


def _enrich_codex(session):
    """Enrich a Codex session from its JSONL transcript.

    Reads session_meta for cwd, then task_complete for the first assistant
    message (title), and turn_context for the user prompt.
    """
    path = session.get("transcript_path")
    if not path:
        return
    user_prompt = None
    assistant_msg = None
    try:
        with open(path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                t = ev.get("type")
                payload = ev.get("payload") if isinstance(ev.get("payload"),
                                                           dict) else {}

                if t == "session_meta":
                    cwd = payload.get("cwd")
                    if cwd and "cwd" not in session:
                        session["cwd"] = cwd

                elif t == "response_item" and payload.get("type") == "message" and payload.get("role") == "user" and not user_prompt:
                    user_prompt = "\n".join(_extract_text_from_content_item(item)
                                             for item in payload.get("content", []) if isinstance(item, dict))
                elif t == "event_msg":
                    pt = payload.get("type", "")
                    if pt == "turn_context" and not user_prompt:
                        inp = payload.get("input")
                        if isinstance(inp, list):
                            for item in inp:
                                if isinstance(item, dict):
                                    txt = _extract_text_from_content_item(item)
                                    if txt:
                                        user_prompt = txt
                                        break
                        elif isinstance(inp, str) and inp.strip():
                            user_prompt = inp.strip()

                    elif pt == "task_complete" and not assistant_msg:
                        msg = payload.get("last_agent_message")
                        if isinstance(msg, str) and msg.strip():
                            assistant_msg = msg.strip()

                if user_prompt and assistant_msg:
                    break
    except IOError:
        pass

    if user_prompt:
        session.setdefault("title", user_prompt[:80])


def _extract_text_from_content_item(item):
    """Extract text from a Codex/Claude content item dict."""
    t = item.get("type", "")
    if t == "text":
        return (item.get("text") or "").strip()
    # Codex also has input_text type
    if t in ("input_text", "output_text"):
        return (item.get("text") or "").strip()
    return ""


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

def _claude_sessions(home=None):
    base = home or os.path.expanduser("~")
    root = os.path.join(base, ".claude", "projects")
    if not os.path.isdir(root):
        return []

    sessions = {}
    # Walk two levels: projects/<encoded>/<uuid>.jsonl
    for project_dir in os.listdir(root):
        pdir = os.path.join(root, project_dir)
        if not os.path.isdir(pdir):
            continue
        for fname in os.listdir(pdir):
            fpath = os.path.join(pdir, fname)
            # Root session: UUID-named .jsonl directly under project dir
            if fname.endswith(".jsonl") and UUID_FULL_RE.match(fname[:-6]):
                session_id = fname[:-6]
                try:
                    mtime = os.path.getmtime(fpath)
                except OSError:
                    mtime = 0
                existing = sessions.get(session_id)
                if existing is None or mtime > existing["modified_at"]:
                    sessions[session_id] = {
                        "harness": "claude",
                        "kind": "claude",
                        "native_id": session_id,
                        "address": "claude:%s" % session_id,
                        "transcript_path": fpath,
                        "modified_at": mtime,
                        "is_subagent": False,
                    }
            # Subagent sessions live under a UUID-named subdirectory:
            # projects/<encoded>/<parent-uuid>/<child-uuid>.jsonl
            elif os.path.isdir(fpath) and UUID_FULL_RE.match(fname):
                for sub_fname in os.listdir(fpath):
                    if not sub_fname.endswith(".jsonl"):
                        continue
                    sub_id = sub_fname[:-6]
                    if not UUID_FULL_RE.match(sub_id):
                        continue
                    sub_path = os.path.join(fpath, sub_fname)
                    try:
                        mtime = os.path.getmtime(sub_path)
                    except OSError:
                        mtime = 0
                    if sub_id not in sessions:
                        sessions[sub_id] = {
                            "harness": "claude",
                            "kind": "claude-subagent",
                            "native_id": sub_id,
                            "address": "claude:%s" % sub_id,
                            "transcript_path": sub_path,
                            "modified_at": mtime,
                            "is_subagent": True,
                            "parent_session_id": fname,
                        }

    results = sorted(sessions.values(), key=lambda s: s["modified_at"],
                     reverse=True)
    for s in results:
        _enrich_claude(s)
    return results


def _enrich_claude(session):
    path = session.get("transcript_path")
    if not path:
        return
    try:
        with open(path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("cwd"):
                    session.setdefault("cwd", ev["cwd"])
                role = ev.get("type") or ev.get("role") or ""
                if role not in ("user", "human"):
                    continue
                msg = ev.get("message") or ev
                content = msg.get("content", "")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            text = (part.get("text") or "").strip()
                            if text:
                                session.setdefault("title", text[:80])
                                return
                elif isinstance(content, str) and content.strip():
                    session.setdefault("title", content.strip()[:80])
                    return
    except IOError:
        pass


# ---------------------------------------------------------------------------
# Cursor  (CLI = SQLite store; IDE = AgentStores directory)
# ---------------------------------------------------------------------------

_CURSOR_IDE_STORE = os.path.expanduser(
    "~/Library/Application Support/Cursor/AgentStores/cursor_agent_stores")


def is_ide_chat(chat_id, ide_store_root=None, home=None):
    """True when this chat id belongs to the Cursor IDE, not the CLI."""
    root = ide_store_root or _CURSOR_IDE_STORE
    if os.path.isdir(os.path.join(root, chat_id)):
        return True
    for db_path in _cursor_ide_databases(home):
        try:
            with _cursor_db(db_path) as db:
                tables = _cursor_tables(db)
                if "composerHeaders" in tables and db.execute(
                        "SELECT 1 FROM composerHeaders WHERE composerId=?", (chat_id,)).fetchone():
                    return True
                if "cursorDiskKV" in tables and db.execute(
                        "SELECT 1 FROM cursorDiskKV WHERE key=?",
                        ("composerData:" + chat_id,)).fetchone():
                    return True
        except (sqlite3.Error, OSError):
            continue
    for db_path in _cursor_ide_databases(home, "conversation-search.db"):
        try:
            with _cursor_db(db_path) as db:
                if db.execute("SELECT 1 FROM conversations WHERE id=? AND source='local'",
                              (chat_id,)).fetchone():
                    return True
        except (sqlite3.Error, OSError):
            continue
    return False


def _cursor_ide_databases(home=None, filename="state.vscdb"):
    base = home or os.path.expanduser("~")
    roots = [os.path.join(base, "Library", "Application Support", "Cursor"),
             os.path.join(base, ".config", "Cursor"),
             os.path.join(base, "AppData", "Roaming", "Cursor")]
    return [os.path.join(root, "User", "globalStorage", filename)
            for root in roots if os.path.isfile(os.path.join(root, "User", "globalStorage", filename))]


@contextlib.contextmanager
def _cursor_db(path):
    db = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        db.execute("PRAGMA query_only=ON")
        yield db
    finally:
        db.close()


def _cursor_tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _cursor_json(value):
    try:
        obj = json.loads(value)
        return obj if isinstance(obj, dict) else {}
    except (ValueError, TypeError, UnicodeError):
        return {}


def _cursor_ide_entry(chat_id, metadata, path, readable):
    subagent = metadata.get("subagentInfo") or {}
    entry = {"harness": "cursor", "kind": "cursor-ide", "native_id": chat_id,
             "address": "cursor:" + chat_id, "title": metadata.get("name") or metadata.get("title"),
             "modified_at": (metadata.get("lastUpdatedAt") or metadata.get("createdAt") or 0) / 1000,
             "archived": bool(metadata.get("isArchived")),
             "is_subagent": bool(metadata.get("isSubagent") or subagent),
             "transcript_path": path,
             "capability": {"read": readable, "send": False, "resume": False,
                            "send_reason": "Cursor IDE chats cannot be addressed through the CLI; CLI resume writes a separate transcript"}}
    if not readable:
        entry["capability"]["read_reason"] = "conversation metadata is indexed, but its message history is not stored locally"
    if subagent.get("parentComposerId"):
        entry["parent_session_id"] = subagent["parentComposerId"]
    workspace = metadata.get("workspaceIdentifier") or {}
    uri = workspace.get("uri") or {}
    if isinstance(uri, dict) and uri.get("fsPath"):
        entry["cwd"] = uri["fsPath"]
    elif metadata.get("trackedGitRepos"):
        entry["cwd"] = metadata["trackedGitRepos"][0].get("repoPath")
    return entry


def _cursor_ide_sqlite_sessions(home=None):
    sessions = {}
    for path in _cursor_ide_databases(home):
        try:
            with _cursor_db(path) as db:
                tables = _cursor_tables(db)
                ids = {r[0][len("composerData:"):] for r in db.execute(
                    "SELECT key FROM cursorDiskKV WHERE key GLOB 'composerData:*'")} if "cursorDiskKV" in tables else set()
                if "composerHeaders" in tables:
                    for chat_id, updated, archived, subagent, value in db.execute(
                            "SELECT composerId,lastUpdatedAt,isArchived,isSubagent,value FROM composerHeaders"):
                        metadata = _cursor_json(value)
                        if chat_id.startswith("draft-") or metadata.get("isDraft"):
                            continue
                        metadata.update(lastUpdatedAt=updated, isArchived=archived, isSubagent=subagent)
                        sessions[chat_id] = _cursor_ide_entry(chat_id, metadata, path, chat_id in ids)
                # Older versions can persist composers without a separate header table.
                for chat_id in ids - sessions.keys():
                    row = db.execute("SELECT value FROM cursorDiskKV WHERE key=?", ("composerData:" + chat_id,)).fetchone()
                    metadata = _cursor_json(row[0])
                    if chat_id.startswith("draft-") or metadata.get("isDraft"):
                        continue
                    sessions[chat_id] = _cursor_ide_entry(chat_id, metadata, path, True)
        except (sqlite3.Error, OSError):
            continue
    for path in _cursor_ide_databases(home, "conversation-search.db"):
        try:
            with _cursor_db(path) as db:
                for source, chat_id, title, updated, archived in db.execute(
                        "SELECT source,id,title,updated_at,is_archived FROM conversations"):
                    if chat_id in sessions or chat_id.startswith("draft-"):
                        continue
                    entry = _cursor_ide_entry(chat_id, {"title": title, "lastUpdatedAt": updated,
                                                       "isArchived": archived}, path, False)
                    if source == "cloud-cache" or chat_id.startswith("bc-"):
                        entry["kind"] = "cursor-cloud"
                    sessions[chat_id] = entry
        except (sqlite3.Error, OSError):
            continue
    return list(sessions.values())


def _cursor_sessions(home=None):
    sessions = {}

    # ---- CLI sessions: ~/.cursor/chats/<ws-hash>/<chat-id>/store.db -------
    cli_root = (os.path.join(home, ".cursor", "chats") if home
                else os.path.expanduser("~/.cursor/chats"))
    if os.path.isdir(cli_root):
        for ws_hash in os.listdir(cli_root):
            ws_dir = os.path.join(cli_root, ws_hash)
            if not os.path.isdir(ws_dir):
                continue
            for chat_id in os.listdir(ws_dir):
                if not UUID_FULL_RE.match(chat_id):
                    # bc- cloud agents: note them as unsupported
                    if chat_id.startswith("bc-"):
                        sessions.setdefault(chat_id, {
                            "harness": "cursor",
                            "kind": "cursor-cloud",
                            "native_id": chat_id,
                            "address": "cursor:%s" % chat_id,
                            "modified_at": 0,
                            "capability": {
                                "send": False,
                                "send_reason": "cloud agent ids (bc-) are not supported by this adapter",
                            },
                        })
                    continue
                chat_dir = os.path.join(ws_dir, chat_id)
                if not os.path.isdir(chat_dir):
                    continue
                try:
                    mtime = os.path.getmtime(chat_dir)
                except OSError:
                    mtime = 0
                entry = {
                    "harness": "cursor",
                    "kind": "cursor-cli",
                    "native_id": chat_id,
                    "address": "cursor:%s" % chat_id,
                    "transcript_path": chat_dir,
                    "modified_at": mtime,
                }
                _enrich_cursor_sqlite(entry, chat_dir)
                existing = sessions.get(chat_id)
                if existing is None or mtime > existing["modified_at"]:
                    sessions[chat_id] = entry

    # ---- IDE sessions: macOS AgentStores Markdown transcripts -------------
    ide_root = (_CURSOR_IDE_STORE if home is None else os.path.join(home, "Library", "Application Support", "Cursor", "AgentStores", "cursor_agent_stores"))
    if ide_root and os.path.isdir(ide_root):
        for chat_id in os.listdir(ide_root):
            if not UUID_FULL_RE.match(chat_id):
                continue
            chat_dir = os.path.join(ide_root, chat_id)
            if not os.path.isdir(chat_dir):
                continue
            try:
                mtime = os.path.getmtime(chat_dir)
            except OSError:
                mtime = 0
            existing = sessions.get(chat_id)
            if existing is not None:
                # Upgrade: chat exists in both CLI store and IDE store
                existing["kind"] = "cursor-ide"
                existing["capability"] = {
                    "send": False,
                    "send_reason": (
                        "cursor-ide: resuming from the CLI writes a separate "
                        "transcript the IDE never shows"),
                }
            else:
                entry = {
                    "harness": "cursor",
                    "kind": "cursor-ide",
                    "native_id": chat_id,
                    "address": "cursor:%s" % chat_id,
                    "transcript_path": chat_dir,
                    "modified_at": mtime,
                    "capability": {
                        "send": False,
                        "send_reason": (
                            "cursor-ide: resuming from the CLI writes a "
                            "separate transcript the IDE never shows"),
                    },
                }
                _enrich_cursor_ide(entry, chat_dir)
                sessions[chat_id] = entry

    for entry in _cursor_ide_sqlite_sessions(home):
        existing = sessions.get(entry["native_id"])
        if (not entry["capability"]["read"] and existing
                and existing["kind"] == "cursor-ide"
                and existing.get("transcript_path")):
            entry["transcript_path"] = existing["transcript_path"]
            entry["capability"]["read"] = True
            entry["capability"].pop("read_reason", None)
        sessions[entry["native_id"]] = entry
    results = sorted(sessions.values(), key=lambda s: s["modified_at"],
                     reverse=True)
    return results


def _enrich_cursor_sqlite(entry, chat_dir):
    """Enrich a cursor-cli session by reading its SQLite store.db."""
    db_path = os.path.join(chat_dir, "store.db")
    if not os.path.isfile(db_path):
        return
    try:
        import sqlite3
        con = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(db_path, safe="/"), uri=True,
                              check_same_thread=False)
        try:
            # Read meta: key=0 contains hex-encoded JSON
            for row in con.execute("SELECT value FROM meta WHERE key='0'"):
                try:
                    meta = json.loads(bytes.fromhex(row[0]))
                    name = meta.get("name")
                    if name and name != "New Agent":
                        entry.setdefault("title", name)
                    created = meta.get("createdAt")
                    if isinstance(created, (int, float)):
                        # epoch milliseconds
                        entry.setdefault("modified_at", created / 1000.0)
                except (ValueError, TypeError):
                    pass
            # Also try meta.json next to the db
            _enrich_cursor_meta_json(entry, chat_dir)
            # Find first meaningful user message for title.
            # Cursor injects system context as role=user with <user_info> or
            # <timestamp> wrappers; skip those and prefer <user_query> content.
            if "title" not in entry:
                for row in con.execute("SELECT data FROM blobs"):
                    try:
                        obj = json.loads(row[0])
                        if obj.get("role") == "user":
                            text = _cursor_message_text(obj)
                            title = _extract_cursor_user_query(text)
                            if title:
                                entry["title"] = title[:80]
                                break
                    except (ValueError, TypeError):
                        continue
        finally:
            con.close()
    except Exception:
        # SQLite not available or DB locked/corrupt; fall back gracefully
        _enrich_cursor_meta_json(entry, chat_dir)


def _enrich_cursor_meta_json(entry, chat_dir):
    """Enrich from meta.json if it exists alongside store.db."""
    meta_path = os.path.join(chat_dir, "meta.json")
    try:
        with open(meta_path) as f:
            meta = json.load(f)
        for key in ("workdir", "cwd", "directory"):
            val = meta.get(key)
            if val:
                entry.setdefault("cwd", val)
                break
        for key in ("title", "name", "description"):
            val = meta.get(key)
            if val:
                entry.setdefault("title", str(val)[:80])
                break
    except (IOError, ValueError):
        pass


def _cursor_message_text(obj):
    """Extract text from a Cursor blob message object."""
    content = obj.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text", ""))
            elif isinstance(p, str):
                parts.append(p)
        return " ".join(p for p in parts if p).strip()
    return ""


def _extract_cursor_user_query(text):
    """Return the actual user query text, skipping Cursor's system injections.

    Cursor injects context as role=user messages with <user_info>, <timestamp>,
    <user_query> wrappers.  Extract the <user_query> body when present; skip
    messages that are purely system context.
    """
    if not text:
        return ""
    # Extract <user_query>...</user_query> if present
    m = re.search(r"<user_query>(.*?)</user_query>", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Skip messages that are purely system-injected context
    stripped = text.strip()
    if stripped.startswith("<user_info>") or stripped.startswith("<timestamp>"):
        return ""
    # Plain user message with no wrappers
    return stripped


def _enrich_cursor_ide(entry, chat_dir):
    """Enrich an IDE session from any Markdown transcripts in its directory."""
    try:
        for fname in sorted(os.listdir(chat_dir)):
            if fname.endswith(".md"):
                fpath = os.path.join(chat_dir, fname)
                try:
                    with open(fpath, errors="replace") as f:
                        text = f.read(500).strip()
                    if text:
                        entry.setdefault("title", text[:80])
                        break
                except IOError:
                    continue
    except OSError:
        pass


# ---------------------------------------------------------------------------
# OpenCode
# ---------------------------------------------------------------------------

def _opencode_sessions(base_url=None, home=None):
    """Combine persisted local sessions with sessions visible on the server."""
    rows = {r["native_id"]: r for r in _opencode_sessions_db(home)}
    for row in (_opencode_sessions_http(base_url) or []):
        rows[row["native_id"]] = dict(rows.get(row["native_id"], {}), **row)
    if not rows:
        for row in _opencode_sessions_cli():
            rows[row["native_id"]] = row
    return sorted(rows.values(), key=lambda r: r.get("modified_at", 0), reverse=True)


def _opencode_sessions_http(base_url=None):
    """Try HTTP API; return list or None if server unreachable."""
    from . import httpc
    base = base_url or config.opencode_base_url()
    try:
        status, body = httpc.request("GET", "%s/api/session" % base, timeout=5,
                                     auth_header=config.opencode_auth_header())
        if isinstance(body, dict) and "data" in body:
            body = body["data"]
        if status != 200 or not isinstance(body, list):
            return None
    except Exception:
        return None

    sessions = []
    for s in body:
        if not isinstance(s, dict):
            continue
        sid = s.get("id") or s.get("sessionID") or s.get("session_id")
        if not sid:
            continue
        # time field may be a dict {"updated": <epoch_ms>} or a scalar
        time_field = s.get("time") or s.get("updated_at") or 0
        if isinstance(time_field, dict):
            raw_ms = time_field.get("updated", 0)
            mtime = raw_ms / 1000.0 if raw_ms else 0
        elif isinstance(time_field, (int, float)):
            mtime = float(time_field)
        else:
            mtime = 0
        sessions.append({
            "harness": "opencode",
            "kind": "opencode",
            "native_id": sid,
            "address": "opencode:%s" % sid,
            "title": (s.get("title") or s.get("name") or "")[:80] or None,
            "modified_at": mtime,
            "server_url": base,
            "capability": {"read": True},
        })
    sessions.sort(key=lambda s: s["modified_at"], reverse=True)
    return sessions


def _opencode_sessions_cli():
    """Discover OpenCode sessions via 'opencode session list' (JSONL output)."""
    try:
        binary = config.resolve_binary("opencode")
    except Exception:
        return []
    try:
        result = subprocess.run(
            [binary, "session", "list", "--format", "json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return []

    sessions = []
    raw = (result.stdout or b"").decode("utf-8", "replace")
    try:
        decoded = json.loads(raw)
        lines = [json.dumps(r) for r in decoded] if isinstance(decoded, list) else [raw]
    except ValueError:
        lines = raw.splitlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        sid = entry.get("id")
        if not sid:
            continue
        name = entry.get("title") or entry.get("thread_name") or ""
        updated = entry.get("updated_at") or ""
        mtime = 0
        if updated:
            try:
                import datetime
                dt = datetime.datetime.fromisoformat(
                    updated.replace("Z", "+00:00"))
                mtime = dt.timestamp()
            except (ValueError, TypeError):
                pass
        sessions.append({
            "harness": "opencode",
            "kind": "opencode",
            "native_id": sid,
            "address": "opencode:%s" % sid,
            "title": name[:80] if name else None,
            "modified_at": mtime,
            "server_url": None,
        })
    sessions.sort(key=lambda s: s["modified_at"], reverse=True)
    return sessions


# ---------------------------------------------------------------------------
# History reading
# ---------------------------------------------------------------------------

def read_history(harness, native_id, limit=50, home=None, offset=0):
    """Read a page of full message text; offset counts backward from the newest."""
    from .errors import UsageError
    if not isinstance(limit, int) or not isinstance(offset, int) or limit < 0 or offset < 0:
        raise UsageError("history limit and offset must be non-negative integers")
    if limit == 0:
        return []
    readers = {"codex": _codex_history, "claude": _claude_history,
               "cursor": _cursor_history, "cursor-cli": _cursor_history,
               "cursor-ide": _cursor_history}
    if harness in readers:
        messages = readers[harness](native_id, limit + offset, home)
    elif harness == "opencode":
        messages = _opencode_history(native_id, limit + offset, home=home)
    else:
        raise UsageError("unknown history harness: %s" % harness)
    if offset:
        messages = messages[:-offset]
    return messages[-limit:]


def _codex_history(thread_id, limit, home):
    base = home or os.path.expanduser("~")
    patterns = [os.path.join(base, ".codex", name, "**", "rollout-*%s*.jsonl" % thread_id)
                for name in ("sessions", "archived_sessions")]
    candidates = [p for pattern in patterns for p in glob.glob(pattern, recursive=True)]
    if not candidates:
        return []
    messages, fallback = [], []
    turn_id = None
    with open(max(candidates, key=os.path.getmtime), errors="replace") as f:
        for line in f:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = event.get("type")
            pt = payload.get("type")
            if kind == "turn_context" or (kind == "event_msg" and pt == "task_started"):
                turn_id = payload.get("turn_id") or turn_id
            if kind == "response_item" and pt == "message":
                text = "\n".join(_extract_text_from_content_item(p) for p in payload.get("content", [])
                                 if isinstance(p, dict))
                if text:
                    messages.append({"role": payload.get("role", "system"), "content": text,
                                     "turn_id": turn_id, "timestamp": event.get("timestamp")})
            elif kind == "event_msg":
                text, role = None, None
                if pt == "user_message":
                    text, role = payload.get("message"), "user"
                elif pt == "turn_context":
                    inp = payload.get("input", [])
                    text = inp if isinstance(inp, str) else "\n".join(
                        _extract_text_from_content_item(p) for p in inp if isinstance(p, dict))
                    role = "user"
                elif pt == "task_complete":
                    text, role = payload.get("last_agent_message"), "assistant"
                if text:
                    fallback.append({"role": role, "content": text,
                                     "turn_id": payload.get("turn_id") or turn_id,
                                     "timestamp": event.get("timestamp")})
    return (messages or fallback)[-limit:]


def _claude_history(session_id, limit, home):
    base = home or os.path.expanduser("~")
    root = os.path.join(base, ".claude", "projects")
    # Search recursively for the session file
    pattern = os.path.join(root, "**", "%s.jsonl" % session_id)
    candidates = sorted(glob.glob(pattern, recursive=True),
                        key=os.path.getmtime, reverse=True)
    if not candidates:
        return []
    messages = []
    try:
        with open(candidates[0], errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                msg = _claude_event_to_message(ev)
                if msg:
                    messages.append(msg)
    except IOError:
        pass
    return messages[-limit:]


def _claude_event_to_message(event):
    role = event.get("type") or event.get("role") or ""
    if role in ("user", "human"):
        msg = event.get("message") or event
        content = msg.get("content", "")
        text = _extract_text(content)
        if text:
            return {"role": "user", "content": text}
    if role == "assistant":
        msg = event.get("message") or event
        content = msg.get("content", "")
        text = _extract_text(content)
        if text:
            return {"role": "assistant", "content": text}
    if role == "result":
        result = event.get("result", "")
        if isinstance(result, str) and result.strip():
            return {"role": "assistant", "content": result.strip()}
    return None


def _extract_text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return " ".join(p for p in parts if p).strip()
    return ""


def _cursor_root_ids(data):
    """Decode the observed protobuf root's repeated message hashes (field 1)."""
    def varint(pos):
        value = shift = 0
        while pos < len(data) and shift < 70:
            byte = data[pos]; pos += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value, pos
            shift += 7
        raise ValueError("Invalid Cursor root varint")
    result = []; pos = 0
    while pos < len(data):
        tag, pos = varint(pos); wire = tag & 7
        if wire == 2:
            size, pos = varint(pos)
            if pos + size > len(data):
                raise ValueError("Truncated Cursor root")
            value = data[pos:pos + size]; pos += size
            if tag >> 3 == 1 and size == 32:
                result.append(value.hex())
        elif wire == 0:
            _, pos = varint(pos)
        elif wire in (1, 5):
            pos += 8 if wire == 1 else 4
        else:
            raise ValueError("Unknown Cursor root wire type")
    return result


def _cursor_store_messages(db_path):
    con = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(db_path, safe="/"), uri=True)
    try:
        blobs = dict(con.execute("SELECT id,data FROM blobs"))
        ids = list(blobs); ordering = "unknown"
        try:
            row = con.execute("SELECT value FROM meta WHERE key='0'").fetchone()
            raw = row[0]
            if isinstance(raw, bytes):
                raw = raw.decode()
            try:
                meta = json.loads(raw)
            except ValueError:
                meta = json.loads(bytes.fromhex(raw))
            root = blobs[meta["latestRootBlobId"]]
            ordered = _cursor_root_ids(root)
            if ordered and all(key in blobs for key in ordered):
                ids = ordered; ordering = "native-root"
        except (sqlite3.Error, ValueError, KeyError, TypeError, UnicodeError):
            pass
        messages = []
        for key in ids:
            try:
                obj = json.loads(blobs[key])
                role = obj.get("role", "")
                if role not in ("user", "assistant", "system"):
                    continue
                text = _cursor_message_text(obj)
                if text:
                    messages.append({"role": role, "content": text,
                                     "message_id": key, "ordering": ordering})
            except (ValueError, TypeError, AttributeError):
                continue
        return messages
    finally:
        con.close()


def _cursor_history(chat_id, limit, home):
    """Read the current Cursor branch; label unordered legacy blob fallbacks."""
    ide_records = _cursor_ide_records(chat_id, home)
    if ide_records is not None:
        return _cursor_ide_messages(ide_records)[-limit:]
    if is_ide_chat(chat_id, home=home):
        return _cursor_ide_history(chat_id, limit, home)
    cli_root = (os.path.join(home, ".cursor", "chats") if home
                else os.path.expanduser("~/.cursor/chats"))
    candidates = glob.glob(os.path.join(cli_root, "*", chat_id))
    if not candidates:
        return _cursor_ide_history(chat_id, limit, home)
    chat_dir = candidates[0]; db_path = os.path.join(chat_dir, "store.db")
    if not os.path.isfile(db_path):
        return _cursor_raw_fallback(chat_dir, limit)
    try:
        messages = _cursor_store_messages(db_path)
    except sqlite3.Error:
        # Some stores require a WAL index write even for a read-only connection.
        # Copy the database and WAL together; never let SQLite touch the source.
        import tempfile
        import shutil
        try:
            with tempfile.TemporaryDirectory(prefix="agentctl-cursor-read-") as tmp:
                copy_path = os.path.join(tmp, "store.db")
                for suffix in ("", "-wal", "-shm"):
                    if os.path.isfile(db_path + suffix):
                        shutil.copyfile(db_path + suffix, copy_path + suffix)
                messages = _cursor_store_messages(copy_path)
                for message in messages:
                    message["snapshot"] = "copied-local-store"
        except (sqlite3.Error, OSError):
            return _cursor_raw_fallback(chat_dir, limit)
    return messages[-limit:]


def _cursor_raw_fallback(chat_dir, limit):
    """Return raw access note when structured parsing is unavailable."""
    db_path = os.path.join(chat_dir, "store.db")
    if os.path.isfile(db_path):
        return [{
            "role": "system",
            "content": "[raw] store.db found but could not be read "
                       "(sqlite3 unavailable or DB locked). "
                       "Path: %s" % db_path,
        }]
    return []


def _cursor_ide_records(chat_id, home=None):
    """Return the composer and its ordered bubbles; never sort by storage order."""
    for path in _cursor_ide_databases(home):
        try:
            with _cursor_db(path) as db:
                if "cursorDiskKV" not in _cursor_tables(db):
                    continue
                row = db.execute("SELECT value FROM cursorDiskKV WHERE key=?",
                                 ("composerData:" + chat_id,)).fetchone()
                if not row:
                    continue
                composer = _cursor_json(row[0])
                records = [{"key": "composerData:" + chat_id, "value": composer}]
                headers = composer.get("fullConversationHeadersOnly")
                inline = composer.get("conversationMap") or {}
                if not isinstance(headers, list):
                    conversation = composer.get("conversation")
                    if isinstance(conversation, list):
                        return records + [{"value": b} for b in conversation if isinstance(b, dict)]
                    # Preserve evidence without pretending an unordered store is a transcript.
                    for key, value in db.execute("SELECT key,value FROM cursorDiskKV WHERE key GLOB ?",
                                                ("bubbleId:" + chat_id + ":*",)):
                        records.append({"key": key, "value": _cursor_json(value), "ordering": "unknown"})
                    return records
                for header in headers:
                    if not isinstance(header, dict) or not header.get("bubbleId"):
                        continue
                    bubble_id = header["bubbleId"]
                    key = "bubbleId:" + chat_id + ":" + bubble_id
                    row = db.execute("SELECT value FROM cursorDiskKV WHERE key=?", (key,)).fetchone()
                    bubble = _cursor_json(row[0]) if row else inline.get(bubble_id)
                    if not isinstance(bubble, dict) or not bubble:
                        records.append({"key": key, "header": header,
                                        "missing": "message body is not stored locally"})
                    else:
                        records.append({"key": key, "value": bubble})
                return records
        except (sqlite3.Error, OSError):
            continue
    return None


def _cursor_ide_messages(records):
    messages = []
    for record in records[1:]:
        if record.get("missing"):
            messages.append({"role": "system", "content": "[unavailable message] " + record["missing"],
                             "bubble_id": record["header"]["bubbleId"]})
            continue
        bubble = record["value"]
        text = bubble.get("text")
        if not isinstance(text, str) or not text:
            continue
        role = {1: "user", 2: "assistant"}.get(bubble.get("type"), bubble.get("role", "system"))
        message = {"role": role, "content": text}
        if record.get("ordering"):
            message["ordering"] = record["ordering"]
        messages.append(message)
    return messages


def _cursor_ide_history(chat_id, limit, home=None):
    """Read IDE chat history from AgentStores Markdown transcripts."""
    root = (_CURSOR_IDE_STORE if home is None else os.path.join(home, "Library", "Application Support", "Cursor", "AgentStores", "cursor_agent_stores"))
    ide_dir = os.path.join(root, chat_id)
    if not os.path.isdir(ide_dir):
        return []
    messages = []
    try:
        for fname in sorted(os.listdir(ide_dir)):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(ide_dir, fname)
            try:
                with open(fpath, errors="replace") as f:
                    text = f.read().strip()
                if text:
                    messages.append({
                        "role": "system",
                        "content": "[raw markdown] %s" % text,
                    })
            except IOError:
                continue
    except OSError:
        pass
    return messages[-limit:]


def _opencode_history(session_id, limit, base_url=None, home=None):
    """Read OpenCode message history from HTTP API or CLI export."""
    http = _opencode_history_http(session_id, limit, base_url)
    if http is not None:
        return http
    local = _opencode_history_db(session_id, limit, home)
    return local if local is not None else _opencode_history_cli(session_id, limit)


def _opencode_history_http(session_id, limit, base_url=None):
    """HTTP: GET /session/:id/message (singular endpoint)."""
    from . import httpc
    base = base_url or config.opencode_base_url()
    try:
        # Correct endpoint is /session/:id/message (singular, not /messages)
        status, body = httpc.request(
            "GET", "%s/api/session/%s/message" % (base, session_id), timeout=10,
            auth_header=config.opencode_auth_header())
        if isinstance(body, dict) and "data" in body:
            body = body["data"]
        if status != 200 or not isinstance(body, list):
            return None
    except Exception:
        return None

    messages = []
    for msg in body:
        if not isinstance(msg, dict):
            continue
        info = msg.get("info") or {}
        role = info.get("role") or msg.get("role") or ""
        if role not in ("user", "assistant", "system"):
            continue
        parts = msg.get("parts") or msg.get("content") or []
        text = ""
        if isinstance(parts, str):
            text = parts.strip()
        elif isinstance(parts, list):
            texts = []
            for p in parts:
                if isinstance(p, dict) and p.get("type") == "text":
                    texts.append(p.get("text", ""))
                elif isinstance(p, str):
                    texts.append(p)
            text = " ".join(texts).strip()
        if text:
            messages.append({"role": role, "content": text})
    return messages[-limit:]


def _opencode_history_cli(session_id, limit):
    """CLI fallback: opencode export <sessionID> -> JSON."""
    try:
        binary = config.resolve_binary("opencode")
    except Exception:
        return []
    try:
        result = subprocess.run(
            [binary, "export", session_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []

    raw = (result.stdout or b"").decode("utf-8", "replace").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []

    messages = []
    for msg in (data if isinstance(data, list) else
                data.get("messages", []) if isinstance(data, dict) else []):
        if not isinstance(msg, dict):
            continue
        role = (msg.get("info") or {}).get("role") or msg.get("role") or ""
        if role not in ("user", "assistant", "system"):
            continue
        parts = msg.get("parts") or msg.get("content") or []
        text = ""
        if isinstance(parts, str):
            text = parts.strip()
        elif isinstance(parts, list):
            texts = []
            for p in parts:
                if isinstance(p, dict) and p.get("type") == "text":
                    texts.append(p.get("text", ""))
                elif isinstance(p, str):
                    texts.append(p)
            text = " ".join(texts).strip()
        if text:
            messages.append({"role": role, "content": text})
    return messages[-limit:]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

DISCOVERY_SOURCES = ("codex", "claude", "cursor", "opencode")


def discover(harnesses=None, home=None):
    """Return a list of session dicts across all requested harnesses.

    Sorted newest-modified first.
    """
    if harnesses is None:
        harnesses = DISCOVERY_SOURCES
    result = []
    for h in harnesses:
        if h == "codex":
            result.extend(_codex_sessions(home))
        elif h == "claude":
            result.extend(_claude_sessions(home))
        elif h == "cursor":
            result.extend(_cursor_sessions(home))
        elif h == "opencode":
            result.extend(_opencode_sessions(home=home))
    for entry in result:
        kind = entry.get("kind") or entry["harness"]
        cap = entry.setdefault("capability", {})
        cap.setdefault("read", bool(entry.get("transcript_path")))
        cap.setdefault("send", entry["harness"] in ("codex", "cursor", "opencode"))
        cap.setdefault("resume", kind not in ("claude-subagent", "cursor-ide", "cursor-cloud"))
        if entry["harness"] == "claude":
            cap.setdefault("send_reason", "external injection into an interactive Claude client is unsupported; use explicit managed resume for a root session")
        if kind == "claude-subagent":
            cap["resume"] = False
            cap["resume_reason"] = "subagent transcript is not an independently resumable native user session"
        if entry["harness"] == "opencode":
            cap["requires_server"] = True
        cap["completion_watch"] = (entry["harness"] in ("codex", "claude")
                                   or (kind == "cursor-ide" and cap.get("read"))
                                   or (entry["harness"] == "opencode" and bool(entry.get("source_path"))))
        cap["completion_watch_mechanism"] = "transcript-polling" if cap["completion_watch"] else None
        cap["completion_watch_note"] = ("explicit native turn markers are required; notifications follow the next 60-second scan"
                                        if cap["completion_watch"] else "this local format has no supported completion marker; managed jobs still support process-exit completion")
    result.sort(key=lambda s: s.get("modified_at") or 0, reverse=True)
    return result


def filter_sessions(sessions, query):
    """Return sessions whose address, title, or cwd contain ``query``."""
    q = query.lower()
    out = []
    for s in sessions:
        haystack = " ".join(filter(None, [
            s.get("address", ""),
            s.get("title", ""),
            s.get("cwd", ""),
        ])).lower()
        if q in haystack:
            out.append(s)
    return out


@contextlib.contextmanager
def _opencode_db(home=None):
    base = home or os.path.expanduser("~")
    directory = os.environ.get("AGENTCTL_OPENCODE_DATA_DIR") or os.path.join(base, ".local", "share", "opencode")
    paths = glob.glob(os.path.join(directory, "*.db"))
    connection = None
    try:
        if paths:
            connection = sqlite3.connect("file:" + urllib.parse.quote(paths[0], safe="/") + "?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
        yield connection
    finally:
        if connection is not None:
            connection.close()


def _opencode_sessions_db(home=None):
    try:
        with _opencode_db(home) as db:
            if db is None:
                return []
            rows = db.execute("SELECT * FROM session ORDER BY time_updated DESC").fetchall()
            source_path = db.execute("PRAGMA database_list").fetchone()[2]
            return [{"harness": "opencode", "kind": "opencode", "native_id": r["id"],
                     "address": "opencode:" + r["id"], "title": r["title"],
                     "cwd": r["directory"], "modified_at": r["time_updated"] / 1000,
                     "archived": bool(r["time_archived"]) if "time_archived" in r.keys() else False,
                     "server_url": None, "source_path": source_path,
                     "capability": {"read": True}} for r in rows]
    except (sqlite3.Error, OSError):
        return []


def _opencode_history_db(session_id, limit, home=None):
    try:
        with _opencode_db(home) as db:
            if db is None:
                return None
            records = db.execute("SELECT id,data,time_created FROM message WHERE session_id=? ORDER BY time_created,id",
                                 (session_id,)).fetchall()
            out = []
            for record in records:
                obj = json.loads(record["data"])
                parts = db.execute("SELECT data FROM part WHERE message_id=? ORDER BY time_created,id", (record["id"],)).fetchall()
                text = "\n".join(p.get("text", "") for p in (json.loads(r["data"]) for r in parts)
                                 if p.get("type") == "text")
                if text:
                    out.append({"role": obj.get("role", "system"), "content": text,
                                "timestamp": record["time_created"] / 1000, "message_id": record["id"]})
            return out[-limit:]
    except (sqlite3.Error, OSError, ValueError):
        return None


def _cursor_raw_store(db_path):
    """Preserve binary message-graph records as base64 rather than lossy text."""
    import base64
    def read(path):
        with sqlite3.connect("file:" + urllib.parse.quote(path, safe="/") + "?mode=ro", uri=True) as db:
            records = [{"table": "meta", "key": row[0], "value": row[1]}
                       for row in db.execute("SELECT key,value FROM meta")]
            for key, data in db.execute("SELECT id,data FROM blobs"):
                try:
                    value = json.loads(data)
                    records.append({"blob_id": key, "encoding": "json", "data": value})
                except (ValueError, TypeError):
                    binary = data if isinstance(data, bytes) else str(data).encode()
                    records.append({"blob_id": key, "encoding": "base64",
                                    "data": base64.b64encode(binary).decode()})
            return records
    try:
        return read(db_path)
    except sqlite3.Error:
        import tempfile
        import shutil
        with tempfile.TemporaryDirectory(prefix="agentctl-cursor-raw-") as tmp:
            copy_path = os.path.join(tmp, "store.db")
            for suffix in ("", "-wal", "-shm"):
                if os.path.isfile(db_path + suffix):
                    shutil.copyfile(db_path + suffix, copy_path + suffix)
            records = read(copy_path)
            for record in records:
                record["snapshot"] = "copied-local-store"
            return records


def read_raw_history(harness, native_id, home=None):
    """Return native transcript records without discarding tools or message text."""
    rows = discover([harness], home)
    session = next((r for r in rows if r["native_id"] == native_id), None)
    if session is None:
        from .errors import UnknownHandleError
        raise UnknownHandleError("unknown session: %s:%s" % (harness, native_id))
    if harness == "opencode":
        with _opencode_db(home) as db:
            if db is not None:
                records = db.execute("SELECT id,data FROM message WHERE session_id=? ORDER BY time_created,id", (native_id,)).fetchall()
                return [{"info": json.loads(record["data"]), "parts": [json.loads(p[0]) for p in db.execute(
                    "SELECT data FROM part WHERE message_id=? ORDER BY time_created,id", (record["id"],))]} for record in records]
    if harness == "cursor" and session["kind"] in ("cursor-ide", "cursor-cloud"):
        records = _cursor_ide_records(native_id, home)
        if records is not None:
            return records
        if not session.get("capability", {}).get("read"):
            return [{"metadata": session, "history_available": False}]
    path = session.get("transcript_path")
    if path and os.path.isfile(path):
        records = []
        with open(path, errors="replace") as f:
            for number, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    records.append({"line": number, "raw": line.rstrip("\n"),
                                    "parse_error": "invalid or incomplete JSON record"})
        return records
    if path and os.path.isdir(path):
        if harness == "cursor" and os.path.isfile(os.path.join(path, "store.db")):
            return _cursor_raw_store(os.path.join(path, "store.db"))
        return [{"file": p, "content": Path(p).read_text(errors="replace")}
                for p in glob.glob(os.path.join(path, "*.md"))]
    return read_history(harness, native_id, limit=1000000, home=home)
