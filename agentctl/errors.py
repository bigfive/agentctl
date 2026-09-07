"""Error types and exit codes.

agentctl fails loudly. Every failure maps to a distinct exit code so a calling
agent can branch on it without parsing text.
"""


class ExitCode:
    OK = 0
    # 2 is reserved for Claude's asyncRewake contract (see notify/claude.py).
    REWAKE = 2
    USAGE = 64
    UNKNOWN_HANDLE = 65
    INVALID_TARGET = 66
    UNSUPPORTED = 67
    DELIVERY_FAILED = 68
    DAEMON_UNAVAILABLE = 69
    CHILD_FAILED = 70
    CHILD_CANCELLED = 71
    PREREQUISITE_MISSING = 75


class AgentctlError(Exception):
    """Base class for every deliberate agentctl failure."""

    exit_code = ExitCode.USAGE

    def __init__(self, message, hint=None):
        super().__init__(message)
        self.message = message
        self.hint = hint


class UsageError(AgentctlError):
    exit_code = ExitCode.USAGE


class UnknownHandleError(AgentctlError):
    exit_code = ExitCode.UNKNOWN_HANDLE


class InvalidTargetError(AgentctlError):
    exit_code = ExitCode.INVALID_TARGET


class UnsupportedOperationError(AgentctlError):
    exit_code = ExitCode.UNSUPPORTED


class DeliveryError(AgentctlError):
    exit_code = ExitCode.DELIVERY_FAILED


class DaemonUnavailableError(AgentctlError):
    exit_code = ExitCode.DAEMON_UNAVAILABLE


class PrerequisiteMissingError(AgentctlError):
    exit_code = ExitCode.PREREQUISITE_MISSING
