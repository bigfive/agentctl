"""Completion snapshots from explicit native markers, never inactivity."""
import json
from . import discover


def _text(content):
    return discover._extract_text(content)


def snapshot(harness, native_id, home=None):
    session = next((r for r in discover.discover([harness], home=home)
                    if r["native_id"] == native_id), None)
    if session is None:
        return {"supported": False, "reason": "session not found"}
    path = session.get("transcript_path")
    active, turns = None, {}
    if harness in ("codex", "claude") and path and path.endswith(".jsonl"):
        records = []
        with open(path, errors="replace") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # A partially written final record is retried next scan.
                if isinstance(record, dict):
                    records.append(record)
        for record in records:
            if harness == "codex":
                payload = record.get("payload") or {}
                kind = payload.get("type") if record.get("type") == "event_msg" else None
                if kind == "task_started" and payload.get("turn_id"):
                    active = payload["turn_id"]
                    turns.setdefault(active, {"state": "running"})
                elif kind in ("task_complete", "turn_aborted", "task_failed"):
                    tid = payload.get("turn_id") or active
                    if tid:
                        turns[tid] = {"state": "completed" if kind == "task_complete" else
                                      ("interrupted" if kind == "turn_aborted" else "failed"),
                                      "result": payload.get("last_agent_message") or ""}
                        if tid == active:
                            active = None
            else:
                message = record.get("message") or {}
                if record.get("type") == "user" and record.get("uuid"):
                    content = message.get("content")
                    # Tool-result records are user-role messages, not new user turns.
                    if isinstance(content, str) or (isinstance(content, list) and any(
                            isinstance(p, dict) and p.get("type") == "text" for p in content)):
                        active = record["uuid"]
                        turns[active] = {"state": "running"}
                elif record.get("type") == "assistant" and active:
                    reason = message.get("stop_reason")
                    if reason in ("end_turn", "refusal"):
                        turns[active] = {"state": "completed", "result": _text(message.get("content"))}
                        active = None
                elif record.get("type") == "result" and active:
                    turns[active] = {"state": "failed" if record.get("is_error") else "completed",
                                     "result": record.get("result") or ""}
                    active = None
        return {"supported": True, "active_turn": active, "turns": turns, "path": path}
    if harness == "cursor" and session.get("kind") == "cursor-ide":
        records = discover._cursor_ide_records(native_id, home)
        if records:
            composer = records[0]["value"]
            headers = composer.get("fullConversationHeadersOnly", [])
            user = next((h.get("bubbleId") for h in reversed(headers) if h.get("type") == 1), None)
            status = composer.get("status")
            if user and status in ("completed", "aborted", "generating", "running", "inProgress"):
                terminal = status in ("completed", "aborted")
                texts = []
                collecting = False
                for record in records[1:]:
                    bubble = record.get("value") or {}
                    if bubble.get("bubbleId") == user:
                        collecting = True
                    elif collecting and bubble.get("type") == 2 and bubble.get("text"):
                        texts.append(bubble["text"])
                return {"supported": True, "active_turn": None if terminal else user,
                        "turns": {user: {"state": ("completed" if status == "completed" else "interrupted")
                                         if terminal else "running", "result": "\n".join(texts)}}, "path": path}
    if harness == "opencode":
        with discover._opencode_db(home) as db:
            if db is not None:
                for row in db.execute("SELECT id,data FROM message WHERE session_id=? ORDER BY time_created,id", (native_id,)):
                    info = json.loads(row["data"])
                    if info.get("role") == "user":
                        active = row["id"]
                        turns.setdefault(active, {"state": "running"})
                    elif info.get("role") == "assistant":
                        parent = info.get("parentID")
                        if parent and (info.get("error") or ((info.get("time") or {}).get("completed")
                                      and info.get("finish") not in ("tool-calls", "unknown"))):
                            parts = [json.loads(r[0]) for r in db.execute(
                                "SELECT data FROM part WHERE message_id=? ORDER BY time_created,id", (row["id"],))]
                            turns[parent] = {"state": "failed" if info.get("error") else "completed",
                                             "result": "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")}
                            if active == parent:
                                active = None
                return {"supported": True, "active_turn": active, "turns": turns}
    return {"supported": False, "reason": "no supported native completion marker in this session's local format"}
