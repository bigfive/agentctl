---
name: agentctl-team
description: Configure and operate agentctl-team backlog pipelines, prepare stage prompts, launch workers, and respond to human-stage or runner notifications. Use for agentctl-team itself, not individual agentctl jobs or unrelated project work.
---

# agentctl-team

This is a separate application that invokes agentctl's public CLI. The runner
owns queues, capacity, attempts and routing. Configured prompts/scripts own
implementation, review, checks and integration policy. Do not reintroduce
mandatory commits, SHA gates or scheduler-owned merges.

Read [the pipeline reference](references/pipeline.md) when configuring stages,
initializing a project, or changing execution policy. Use the installed CLI's
help to confirm the available commands. Missing tools and invalid configuration
must fail visibly; do not substitute another scheduler or edit ledger state to
conceal a failure.

## Prepare and start

```sh
agentctl-team --config /absolute/pipeline.json init --repo /absolute/repository
agentctl-team --config /absolute/pipeline.json doctor
agentctl-team --config /absolute/pipeline.json issue add --id ID --title TITLE --body-file FILE
agentctl-team --config /absolute/pipeline.json start --notify codex:NATIVE-SESSION-ID
```

`init` refuses overwrites. Review the generated prompts, model, permissions,
base branch, pool sizes and integration policy against the user's requested
scope. It does not start work. Use the user's chosen model; the starter uses
Luna with low reasoning effort. Each item retains one branch/worktree across
fresh LLM sessions. Human stages launch no worker and use no pool slot.

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
retained. A long-lived foreground tool call is not reliable persistence. Keep
the daemon's authentication environment explicit: a T3 shadow `CODEX_HOME`
previously caused missing-credential failures. Do not copy credentials, silently
switch providers, or restart a shared daemon. An isolated `AGENTCTL_HOME` can be
used for an authorized experiment; use that same value for its worker backend.

**After confirming startup, end the manager turn when there is no other work.**
Codex notifications queue until the turn settles. Do not keep the turn open by
polling or waiting for notifications. Harnesses differ: transport acceptance
alone is not proof of visible delivery. Test the intended destination when new.

## Handle incoming notifications

Notifications identify the originating stage. Read the current issue/history
before acting: queued messages may describe a state superseded by an earlier
response or a restarted runner. Never restart solely because an old stop alert
arrived. Do not infer success from worker chat prose; accepted reports and final
job state control transitions.

```sh
agentctl-team --config FILE issue show ID
agentctl-team --config FILE issue report ID --file REPORT.md
```

For a waiting human stage, submit the same Markdown format as workers:

```markdown
---
outcome: DECLARED_OUTCOME
---

Your resolution, findings or instructions.
```

Choose an outcome declared for the current stage. Preserve the user's scope;
ask for genuinely missing decisions rather than inventing requirements. A
human report routes the item; a running scheduler dispatches the next stage
with a new session. It waits up to 30 minutes when human input is the only
remaining work (`human_wait_timeout_seconds`, null for indefinite). Once the
report is submitted, end the turn again unless other work is needed.

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
item. `issue retry` does not reset counts. Manual stage moves are not implemented.

Git ignore rules do not prevent force-added or already tracked runtime files.
Keep the starter's integration check for tracked `.agentctl-team` files before
landing; preserve contents before removing any from the index.

```sh
agentctl-team --config FILE stop --drain
agentctl-team --config FILE stop --cancel-active
agentctl-team --config FILE notification list
agentctl-team --config FILE notification acknowledge NOTICE_ID --note "Inspected and handled manually"
```

Runner exits attempt a separate stop notification; SIGKILL/machine crashes
cannot guarantee one. Failed/uncertain deliveries stop loudly and are never
automatically resent. Inspect recipient and transport records before resolving.
Acknowledgement does not resend or resolve an item.

## Verification and handoff

Verify observable delivered behavior and required checks, not just a `done`
label. Report limitations and pending work. Stop only daemons/processes created
for this task when no longer needed; preserve unrelated sessions. Link the
config, logs and results for a successor. The local app reference is
`references/pipeline.md`; regenerate it from the maintained TEAM.md when the
CLI changes.
