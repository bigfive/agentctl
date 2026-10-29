"""Editable manager prompts around factual notification context."""
from .ports import TeamError

GUIDANCE = {
    'human': """Manager action required at stage {{ stage }}; this does not necessarily require a user decision.
Inspect the current issue and status first; this alert may be stale.
Resolve the obstacle within existing authority. If a user decision is necessary, explain the concrete problem, options, and your recommendation.
Submit a report using a declared outcome, then confirm the item has resumed or reached its intended destination.
Do not end your turn after merely describing an action you can perform.
""",
    'error': """Inspect current state and the failed execution before choosing recovery. Preserve surviving jobs and work.
Resolve the cause within existing authority and retry or use the configured human-stage route. Confirm continuation.
If a user decision is required, explain the concrete blocker, options, and your recommendation.
""",
    'held': """Inspect current state and why this item is held. Resolve what you can within existing authority.
Do not change shared pipeline policy to bypass a limit for this one item. Use configured human outcomes when available.
If the hold requires a user decision, explain the choices and recommend one. Confirm continuation after recovery.
""",
    'runner-completed': """Inspect current status and delivery results; this alert may be stale.
Complete outstanding promised verification, then report what was delivered, checks performed, and remaining limitations to the user. A done label alone is insufficient evidence.
""",
    'runner-error': """Inspect current status, the failure, and surviving jobs before acting; this alert may be stale.
Resolve available manager actions within existing authority and restart when appropriate. Preserve surviving work and avoid duplicate launches.
If a user decision is needed, explain the concrete blocker, options, and your recommendation. Do not end your turn merely describing an action you can perform.
""",
    'human-wait-timeout': """The human-wait timeout elapsed. Inspect current pending handoffs; this alert may be stale.
Resolve what you can within existing authority, submit the required reports, and restart when appropriate. Confirm continuation.
If the user must decide, explain the concrete question, options, and your recommendation.
""",
    'runner-stopped': """Inspect current status before acting; a newer run may already have resumed work.
Respect intentional stop, drain, and cancellation requests. For unresolved holds or dependencies, handle authorized manager actions or explain the required decision.
Do not restart against the user's instruction.
""",
    'runner-lost': """The runner exited without recording a normal stop. Inspect current status and surviving jobs before acting; this notification may be stale.
Recover within existing authority or explain the concrete blocker to the user. Do not cancel, duplicate, or discard surviving work.
Confirm the pipeline resumed after recovery. Do not merely announce that it needs restarting.
""",
}
DEFAULTS = {kind: '{{ context }}\n\n' + text + '\nCurrent status:\n{{ status_command }}\n'
            for kind, text in GUIDANCE.items()}
VARIABLES = {'context', 'source_stage', 'item_id', 'title', 'stage', 'outcomes',
             'issue_command', 'report_command', 'status_command', 'start_command', 'reason', 'error'}


def load_templates(value, base, render):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise TeamError('notification_templates must map supported notification kinds to template files')
    result = dict(DEFAULTS)
    for kind, name in value.items():
        if not isinstance(name, str) or not name.strip():
            raise TeamError('notification template path must be nonempty: ' + kind)
        path = base / name
        if path.is_symlink() or not path.is_file():
            raise TeamError('notification template must be a regular file: ' + str(path))
        text = path.read_text()
        if not text.strip():
            raise TeamError('notification template is empty: ' + kind)
        render(text, {key: 'validation' for key in VARIABLES})
        result[kind] = text
    return result
