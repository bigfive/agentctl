---
name: agentctl-team
description: Configure and operate agentctl-team backlog pipelines, launch workers, and respond to human-stage or runner notifications. Use for agentctl-team itself, not individual agentctl jobs.
---

# agentctl-team

The runner schedules work; configured prompts/scripts define delivery policy.
Use the user's chosen model and scope. Read [pipeline configuration](references/pipeline.md)
when initializing or editing stages; read [operations](references/operations.md)
for detailed session discovery, harness limitations, or failure recovery.

## Start

```sh
agentctl-team --config FILE init --repo REPOSITORY
agentctl-team --config FILE doctor
agentctl-team --config FILE issue add --id ID --title TITLE --body-file BODY.md
agentctl-team --config FILE start --notify codex:VERIFIED_NATIVE_SESSION_ID
```

Review generated prompts, model, base branch and integration policy before
launching. Each item keeps one branch/worktree; every LLM execution is fresh.

For Codex, inspect `CODEX_THREAD_ID` and verify it against the matching rollout's
metadata/current conversation. If absent, inspect recent files under
`${CODEX_HOME:-$HOME/.codex}/sessions/*/*/*/rollout-*.jsonl`; the filename ends
with the session UUID. Never assume the newest file is yours or use an app
conversation GUID. See operations for other harnesses. If discovery fails,
report the blocker; do not silently opt out. `--no-notifications` requires an
explicit user choice. Supply `--notify` again on every restart.

Launch detached with retained logs. Confirm startup, then **end your turn** when
there is no other work: queued notifications arrive after the turn settles.
Do not keep the turn open by polling or waiting for notifications.

## Respond

Read current state before acting; queued alerts may be stale:

```sh
agentctl-team --config FILE issue show ID
agentctl-team --config FILE issue report ID --file REPORT.md
```

For a human stage, submit Markdown front matter `outcome: DECLARED_OUTCOME`
between `---` lines, followed by the resolution. The active runner continues
automatically; it normally waits up to 30 minutes for human input. End the turn
again after responding. Never restart solely because an old stop alert arrived.

Fail visibly and preserve work/history. Don't invent reports, edit ledger state,
or start duplicate workers to bypass errors. Use configured human outcomes for
item-specific resolution; reset-enabled routes reset only the destination-stage
count for that item. `issue retry` does not reset counts. Verify delivered
behavior and checks before claiming completion, not just a `done` label.
