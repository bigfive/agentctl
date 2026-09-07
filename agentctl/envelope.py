"""The completion message a parent session receives.

A compact machine-readable block inside an ordinary follow-up message. The
parent is told where to read the full result rather than being handed an
unbounded transcript.
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


def build(record, result_text):
    """Render the completion envelope for one terminal job."""
    lines = [
        "[agentctl completion]",
        "job: %s" % record.get("job"),
        "child: %s" % child_handle(record),
        "harness: %s" % record.get("harness"),
        "status: %s" % record.get("status"),
        "result: %s" % _short_result(record, result_text),
        "full: agentctl read %s" % record.get("job"),
    ]
    return "\n".join(lines)
