"""Filesystem layout and harness binary resolution.

Binary resolution order is fixed and documented. It is a lookup, not a
fallback between transports: if the named binary is not found, agentctl fails.
"""

import os
import shutil
import base64

from .errors import PrerequisiteMissingError

HARNESSES = ("codex", "claude", "cursor", "opencode")

# Known install locations that are not on PATH. Codex ships inside ChatGPT.app
# on macOS and is frequently absent from a non-login shell's PATH.
EXTRA_BINARY_PATHS = {
    "codex": (
        "/Applications/ChatGPT.app/Contents/Resources/codex",
        os.path.expanduser("~/.codex/bin/codex"),
    ),
    "claude": (os.path.expanduser("~/.local/bin/claude"),),
    "cursor": (
        os.path.expanduser("~/.local/bin/cursor-agent"),
        os.path.expanduser("~/.local/bin/agent"),
    ),
    "opencode": (os.path.expanduser("~/.opencode/bin/opencode"),),
}

# Candidate command names per harness, most specific first. Cursor's CLI is
# installed as both `cursor-agent` and `agent` by its own installer, while the
# Homebrew cask links only `cursor-agent`. The specific name is preferred
# because `agent` is generic enough to belong to something else entirely.
BINARY_NAMES = {
    "codex": ("codex",),
    "claude": ("claude",),
    "cursor": ("cursor-agent", "agent"),
    "opencode": ("opencode",),
}

ENV_OVERRIDES = {
    "codex": "AGENTCTL_CODEX_BIN",
    "claude": "AGENTCTL_CLAUDE_BIN",
    "cursor": "AGENTCTL_CURSOR_BIN",
    "opencode": "AGENTCTL_OPENCODE_BIN",
}

DEFAULT_OPENCODE_URL = "http://127.0.0.1:4096"


def home():
    """Root directory for the daemon socket, logs, and the job store."""
    return os.environ.get("AGENTCTL_HOME") or os.path.expanduser("~/.agentctl")


def jobs_dir():
    return os.path.join(home(), "jobs")


def job_dir(job_id):
    return os.path.join(jobs_dir(), job_id)


def socket_path():
    return os.path.join(home(), "daemon.sock")


def pid_path():
    return os.path.join(home(), "daemon.pid")


def daemon_log_path():
    return os.path.join(home(), "daemon.log")


def ensure_home():
    os.makedirs(jobs_dir(), exist_ok=True)
    return home()


def opencode_base_url():
    """Base URL of the OpenCode server agentctl talks to.

    agentctl never starts an OpenCode server. A running server is a documented
    prerequisite.
    """
    for var in ("AGENTCTL_OPENCODE_URL", "OPENCODE_SERVER", "OPENCODE_BASE_URL"):
        value = os.environ.get(var)
        if value:
            return value.rstrip("/")
    return DEFAULT_OPENCODE_URL


def opencode_credentials():
    """Return credentials for a user-owned OpenCode server."""
    username = os.environ.get(
        "AGENTCTL_OPENCODE_USERNAME",
        os.environ.get("OPENCODE_SERVER_USERNAME", "opencode"),
    )
    password = os.environ.get(
        "AGENTCTL_OPENCODE_PASSWORD",
        os.environ.get("OPENCODE_SERVER_PASSWORD", ""),
    )
    return username, password


def opencode_auth_header():
    username, password = opencode_credentials()
    if not password:
        return None
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return "Basic %s" % token


def resolve_binary(harness):
    """Return the absolute path of a harness binary, or fail loudly."""
    if harness not in BINARY_NAMES:
        raise PrerequisiteMissingError("unknown harness: %s" % harness)

    override = os.environ.get(ENV_OVERRIDES[harness])
    if override:
        if not (os.path.isfile(override) and os.access(override, os.X_OK)):
            raise PrerequisiteMissingError(
                "%s is set to %s, which is not an executable file"
                % (ENV_OVERRIDES[harness], override)
            )
        return override

    for name in BINARY_NAMES[harness]:
        found = shutil.which(name)
        if found:
            return found

    for candidate in EXTRA_BINARY_PATHS.get(harness, ()):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    raise PrerequisiteMissingError(
        "cannot find the %s binary (tried %s)"
        % (harness, ", ".join(BINARY_NAMES[harness])),
        hint="put it on PATH or set %s to its absolute path"
        % ENV_OVERRIDES[harness],
    )
