# Session discovery and troubleshooting

## Find the manager session before starting

Notifications are expected. `start` requires either `--notify HARNESS:NATIVE-ID`
or `--no-notifications`. Do not use the opt-out merely because finding the ID
is inconvenient. If identification fails, stop and explain the blocker; obtain
an explicit user choice before running without notifications.

For Codex, first inspect `CODEX_THREAD_ID` in the current execution environment.
If present, locate the matching rollout file under `${CODEX_HOME:-$HOME/.codex}/sessions`
and verify its session metadata/current conversation against this task. Do not
assume a T3 app conversation GUID is a native Codex ID.

If the environment ID is absent, follow the agentctl skill's session-file lookup:

```sh
ls -t "${CODEX_HOME:-$HOME/.codex}"/sessions/*/*/*/rollout-*.jsonl | head -1
```

The filename ends in the native session UUID. Inspect that file's session
metadata and recent user messages to confirm it belongs to the CURRENT manager
conversation. With concurrent sessions the newest file can belong to another
agent; if it does, inspect other recent candidates. Never pick the newest UUID
without confirmation, and do not print credentials or full unrelated transcripts.
Then launch with `--notify codex:VERIFIED_UUID`.

For other harnesses, read the installed `agentctl` skill's matching harness
section for discovery and push-delivery limitations. Claude does not support
arbitrary push and Cursor IDE rejects it; do not invent a target or claim wakeup
support. Resolve that limitation with the user before choosing an explicit
`--no-notifications` run. OpenCode needs a reachable server owning the session.

`--notify` is per start invocation and must be supplied again on restart. There
is no silent reuse or automatic guess of a manager ID. Existing outbox messages
retain their recorded targets; `--no-notifications` disables new messages, not
previously queued deliveries. Do not redirect old alerts silently.

Launch the runner as a detached process or through a service manager with logs
retained. A long-lived foreground tool call is not reliable persistence. Ensure
the daemon's environment provides authentication for the configured harness.
For isolated experiments, use the same `AGENTCTL_HOME` for the runner and its
worker backend.

**After confirming startup and completing available manager actions, end the turn
when waiting for workers or the user.** Experimental Desktop steering can deliver
mid-turn; queue fallback needs the turn to settle. Do not keep the turn open by
polling or waiting for notifications. Harnesses differ: transport acceptance
alone is not proof of visible delivery. Test the intended destination when new.


## Failures and stopping

`on_error` routes terminal execution/preparation failures to a configured
stage, retaining the original error and history. Without it, the item remains
in error. Unknown launch/status state retains ownership: do not launch a second
worker until reconciled. Preserve worktrees and original errors; do not reset,
stash, delete, or silently repair user work.

Stage `max` counts executions per item/stage since its last explicit reset;
`overflow` routes before another execution. String outcome routes preserve counts.
`{"to":"review","reset_count":true}` resets only this item's destination-stage
count and preserves all history. Choose a configured reset-enabled human outcome
when another attempt is appropriate, rather than changing shared policy for one
item. `issue retry` preserves counts.

Git ignore rules do not prevent force-added or already tracked runtime files.
Keep the starter's integration check for tracked `.agentctl-team` files before
landing; preserve contents before removing any from the index.

```sh
agentctl-team --config FILE stop --drain
agentctl-team --config FILE stop --cancel-active
agentctl-team --config FILE notification list
agentctl-team --config FILE notification acknowledge NOTICE_ID --note "Inspected and handled manually"
```

Runner exits attempt a separate stop notification. An independent watchdog
reports runner disappearance after SIGKILL or another abrupt exit while the
watchdog and machine remain alive. A machine crash or killing both processes
cannot guarantee an alert. It never restarts or cancels work. Failed/uncertain deliveries stop loudly and are never
automatically resent. Inspect recipient and transport records before resolving.
Acknowledgement does not resend or resolve an item.


## Status and watchdog

`status --summary` reads current scheduler-lock ownership, unfinished stage
counts, manager handoffs, active job status/elapsed time, and recent/unresolved
notification transport results. Missing transport metadata in older agentctl
results is shown as unavailable. It does not change jobs or workflow state.

With notifications enabled, normal `start` arms a detached watchdog before
launching workers. `--once` is diagnostic and does not arm it;
`--no-notifications` explicitly disables alerts and the watchdog. Startup fails
if watchdog arming cannot be confirmed. `watchdog.interval_seconds` is optional
and defaults to 5 seconds; invalid values fail validation.

The watchdog records an unexpected exit only after the scheduler lock is free
and no normal stop was recorded. It queues one durable `runner-lost` notice and
never retries ambiguous delivery. Normal stops disarm it. A restarted runner
supersedes an older run record rather than allowing its old monitor to report
the new run as lost. Per-run watchdog config snapshots and logs remain under
the Git common directory's `agentctl-team/` folder for diagnosis.
