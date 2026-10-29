"""Inspect and remove legacy agentctl hooks, preserving unrelated settings.

Installation APIs are retained as non-mutating retirement errors for compatibility.
The daemon observes native transcript completion markers without client hooks.
"""

import copy
import fcntl
import json
import os
import shlex
import sys
import tempfile
import time

from .errors import UsageError

MARKER_KEY = "_agentctl_owned"  # Recognise v1 installs for migration only.
MARKER_VALUE = "v1"
HOOK_COMMAND = "agentctl _hook complete"
CLAUDE_SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")
CURSOR_HOOKS_PATH = os.path.expanduser("~/.cursor/hooks.json")
CODEX_CONFIG_PATH = os.path.expanduser("~/.codex/config.toml")
CODEX_HOOKS_PATH = os.path.expanduser("~/.codex/hooks.json")


def _command(action, harness):
    return "agentctl _hook %s --harness %s" % (action, harness)


def _owned(entry, harness):
    if not isinstance(entry, dict):
        return False
    if entry.get(MARKER_KEY) == MARKER_VALUE:
        return True
    return entry.get("command") in [_command(a, harness)
                                    for a in ("start", "begin", "complete", "receive")]


def _definitions(harness):
    actions = [("SessionStart", "start"), ("UserPromptSubmit", "begin"),
               ("Stop", "complete")]
    if harness == "cursor":
        return {"sessionStart": [{"command": _command("start", harness)}],
                "beforeSubmitPrompt": [{"command": _command("begin", harness)}],
                "stop": [{"command": _command("complete", harness)}]}
    result = {event: [{"hooks": [{"type": "command",
                                  "command": _command(action, harness)}]}]
              for event, action in actions}
    if harness == "claude":
        result["Stop"][0]["hooks"].append(
            {"type": "command", "command": _command("receive", harness),
             "asyncRewake": True, "timeout": 600})
        result["StopFailure"] = [{"hooks": [
            {"type": "command", "command": _command("complete", harness)}]}]
    if harness == "codex":
        result["Interrupt"] = [{"hooks": [
            {"type": "command", "command": _command("complete", harness), "timeout": 3}]}]
    return result


def _without_owned(hooks, harness):
    clean = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise UsageError("hook event %s must contain an array" % event)
        kept = []
        for group in groups:
            if not isinstance(group, dict):
                raise UsageError("hook entries must be objects")
            if harness == "cursor":
                if not _owned(group, harness):
                    kept.append(copy.deepcopy(group))
            else:
                inner = group.get("hooks", [])
                if not isinstance(inner, list):
                    raise UsageError("hook handlers must contain an array")
                rest = [e for e in inner if not _owned(e, harness)]
                if len(rest) == len(inner):
                    kept.append(copy.deepcopy(group))
                elif rest:
                    new = copy.deepcopy(group)
                    new["hooks"] = rest
                    kept.append(new)
        clean[event] = kept
    return clean


def _read_json(path):
    try:
        with open(path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise UsageError("cannot parse %s: %s" % (path, exc))
    if not isinstance(cfg, dict) or not isinstance(cfg.get("hooks", {}), dict):
        raise UsageError("hook configuration must be an object with an object hooks field")
    return cfg


def _write_json(path, payload):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".agentctl-hooks-", dir=parent)
    try:
        if os.path.exists(path):
            os.fchmod(fd, os.stat(path).st_mode & 0o777)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _change(harness, path, dry_run=False, uninstall=False):
    if not uninstall:
        return False, {"ok": False, "changed": False,
                       "error": "hook installation is retired; the daemon polls transcript completion markers",
                       "capabilities": _capabilities(harness)}
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(os.path.dirname(path), exist_ok=True) if not dry_run else None
    # Serialise concurrent setup/uninstall without locking a replaced inode.
    lock = None
    try:
        if not dry_run:
            lock = open(path + ".agentctl.lock", "a")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        old = _read_json(path)
        if uninstall and not old:
            return False, {"ok": True, "changed": False, "path": path}
        cfg = copy.deepcopy(old)
        hooks = _without_owned(cfg.get("hooks", {}), harness)
        if not uninstall:
            for event, entries in _definitions(harness).items():
                hooks.setdefault(event, []).extend(entries)
        cfg["hooks"] = hooks
        if harness == "cursor" and not uninstall:
            cfg.setdefault("version", 1)
        changed = cfg != old
        if changed and not dry_run:
            _write_json(path, cfg)
        return changed, {"ok": True, "changed": changed, "path": path,
                         "dry_run": dry_run, "capabilities": _capabilities(harness)}
    except (OSError, UsageError) as exc:
        return False, {"ok": False, "changed": False, "path": path,
                       "error": str(exc)}
    finally:
        if lock:
            lock.close()


def _inspect(harness, path):
    try:
        cfg = _read_json(path)
        definitions = _definitions(harness)
        hooks = cfg.get("hooks", {})
        commands = []
        for event in hooks:
            for group in hooks[event]:
                entries = [group] if harness == "cursor" else group.get("hooks", [])
                commands.extend(e.get("command") for e in entries if _owned(e, harness))
        expected = []
        for groups in definitions.values():
            for group in groups:
                expected.extend(e["command"] for e in
                                ([group] if harness == "cursor" else group["hooks"]))
        return {"ok": True, "installed": bool(commands),
                "complete": all(c in commands for c in expected), "path": path,
                "commands": commands, "capabilities": _capabilities(harness)}
    except (OSError, UsageError, TypeError) as exc:
        return {"ok": False, "installed": False, "path": path, "error": str(exc)}


def _capabilities(harness):
    caps = {"session_identity": "explicit sender context or native environment variables",
            "completion_detection": "transcript polling in the daemon",
            "hook_installation": "retired; inspect/uninstall are retained for cleanup"}
    if harness == "claude":
        caps.update(callback_receive="unsupported; use stored results or agentctl wait")
    elif harness == "cursor":
        caps.update(callback_receive_cli="CLI resume", callback_receive_ide="unsupported without an external wake route")
    elif harness == "codex":
        caps.update(callback_receive="codex queue", queue=True)
    elif harness == "opencode":
        caps.update(completion_detection="server events and correlated message completion",
                    callback_receive="v2 queued prompt on the server owning the session",
                    limitation="requires a configured reachable server")
    return caps


def _claude_capabilities(): return _capabilities("claude")
def _cursor_capabilities(): return _capabilities("cursor")
def _codex_capabilities(): return _capabilities("codex")
def _opencode_capabilities(): return _capabilities("opencode")


def setup_claude(settings_path=None, dry_run=False):
    return _change("claude", settings_path or CLAUDE_SETTINGS_PATH, dry_run)
def inspect_claude(settings_path=None):
    return _inspect("claude", settings_path or CLAUDE_SETTINGS_PATH)
def uninstall_claude(settings_path=None, dry_run=False):
    return _change("claude", settings_path or CLAUDE_SETTINGS_PATH, dry_run, True)
def setup_cursor(hooks_path=None, dry_run=False):
    return _change("cursor", hooks_path or CURSOR_HOOKS_PATH, dry_run)
def inspect_cursor(hooks_path=None):
    return _inspect("cursor", hooks_path or CURSOR_HOOKS_PATH)
def uninstall_cursor(hooks_path=None, dry_run=False):
    return _change("cursor", hooks_path or CURSOR_HOOKS_PATH, dry_run, True)
def setup_codex(config_path=None, dry_run=True):
    # config_path is retained as the legacy explicit path argument; it now names
    # the standalone hooks JSON file, leaving config.toml unchanged.
    changed, info = _change("codex", config_path or CODEX_HOOKS_PATH, dry_run)
    return changed, info
def inspect_codex(hooks_path=None):
    return _inspect("codex", hooks_path or CODEX_HOOKS_PATH)
def uninstall_codex(hooks_path=None, dry_run=False):
    return _change("codex", hooks_path or CODEX_HOOKS_PATH, dry_run, True)
def setup_opencode(dry_run=False):
    return False, {"ok": True, "changed": False, "capabilities": _capabilities("opencode"),
                   "note": "set AGENTCTL_OPENCODE_URL to the server owning the session"}


def _selected(harnesses):
    selected = list(harnesses) if harnesses is not None else ["claude", "cursor", "codex", "opencode"]
    if any(h not in ("claude", "cursor", "codex", "opencode") for h in selected):
        raise UsageError("unknown setup harness")
    return selected


def _paths(settings_path, cursor_hooks_path, codex_hooks_path):
    # Explicit temporary configuration paths keep API-driven tests isolated.
    if codex_hooks_path is None and (settings_path or cursor_hooks_path):
        codex_hooks_path = os.path.join(os.path.dirname(os.path.abspath(settings_path or cursor_hooks_path)),
                                       "codex-hooks.json")
    return {"claude": settings_path or CLAUDE_SETTINGS_PATH,
            "cursor": cursor_hooks_path or CURSOR_HOOKS_PATH,
            "codex": codex_hooks_path or CODEX_HOOKS_PATH}


def setup_all(dry_run=False, settings_path=None, cursor_hooks_path=None,
              harnesses=None, codex_hooks_path=None):
    paths = _paths(settings_path, cursor_hooks_path, codex_hooks_path)
    result = {}
    for h in _selected(harnesses):
        result[h] = (setup_opencode(dry_run)[1] if h == "opencode"
                     else _change(h, paths[h], dry_run)[1])
    return result


def inspect_all(settings_path=None, cursor_hooks_path=None, harnesses=None,
                codex_hooks_path=None):
    paths = _paths(settings_path, cursor_hooks_path, codex_hooks_path)
    return {h: ({"ok": True, "capabilities": _capabilities(h)} if h == "opencode"
                else _inspect(h, paths[h])) for h in _selected(harnesses)}


def uninstall_all(dry_run=False, settings_path=None, cursor_hooks_path=None,
                  harnesses=None, codex_hooks_path=None):
    paths = _paths(settings_path, cursor_hooks_path, codex_hooks_path)
    return {h: ({"ok": True, "changed": False} if h == "opencode"
                else _change(h, paths[h], dry_run, True)[1]) for h in _selected(harnesses)}
