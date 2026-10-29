"""The notification message a parent session receives.

A compact machine-readable block inside an ordinary follow-up message. The
parent is told where to read the full result or status rather than being
handed an unbounded transcript.
"""

RESULT_LIMIT = 600


def child_handle(record):
    """``<harness>:<native-session-id>``, the child's identity in its harness."""
    native = record.get("native_session_id")
    if not native:
        return "%s:unknown" % record.get("harness")
    return "%s:%s" % (record.get("harness"), native)


def _short_result(record, result_text):
    if record.get("status") == "cancelled":
        return "cancelled before completion"
    if record.get("status") == "failed":
        return (record.get("error") or "no failure message reported").strip()
    text = (result_text or "").strip()
    if not text:
        return "no final message reported"
    single_line = " ".join(text.split())
    if len(single_line) <= RESULT_LIMIT:
        return single_line
    return single_line[:RESULT_LIMIT] + " [truncated]"


def build(record, result_text, event=None, wait=None):
    """Render a completion or timeout envelope for a job."""
    lines = [
        "[agentctl completion]",
        "job: %s" % record.get("job"),
        "child: %s" % child_handle(record),
        "harness: %s" % record.get("harness"),
        "status: %s" % record.get("status"),
    ]
    if event == "timeout":
        lines.extend([
            "event: timeout",
            "result: %s" % _timeout_result(record, result_text),
            "full: agentctl status %s" % record.get("job"),
        ])
        if wait:
            lines.append("next: %s" % _rewait_command(record, wait))
    else:
        lines.extend([
            "result: %s" % _short_result(record, result_text),
            "full: agentctl read %s" % record.get("job"),
        ])
    return "\n".join(lines)


def _timeout_result(record, result_text):
    if record.get("status") in ("pending", "running"):
        return "child is still in progress; asyncwait timeout elapsed"
    return _short_result(record, result_text)


def _rewait_command(record, wait):
    command = "agentctl asyncwait %s --notify %s" % (
        record.get("job"), wait.get("notify_raw"))
    timeout = wait.get("timeout")
    if timeout is not None:
        command += " --timeout %s" % timeout
    return command
