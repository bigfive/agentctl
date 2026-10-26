---
name: agentctl-team
description: Configure and operate agentctl-team backlog pipelines, launch workers, and respond to human-stage or runner notifications. Use for agentctl-team itself, not individual agentctl jobs.
---

# agentctl-team

The runner schedules; configured stages define delivery policy. Use the user's
chosen model and scope. Read [manager responsibilities](references/manager.md)
when launching or handling notifications. Read [pipeline configuration](references/pipeline.md)
when editing stages and [operations](references/operations.md) for session discovery or recovery.

## Define a pipeline

A minimal config needs only `stages`. Each LLM stage requires `worker` with an
explicit harness/model, inline `prompt` (or a `files` directory), and `outcomes`.
The config directory is the repository root by default. The first stage is the
entry point; the checked-out branch is the base. Defaults use `agentctl` from
`PATH`, four slots in the `default` pool, a 30-minute attempt timeout, and managed
worktrees under the Git common directory. Stage pools default to `["default"]`.
Override these fields when needed; invalid values fail rather than using defaults.
See pipeline configuration for all fields and prepared-file behavior.

## Start

```sh
agentctl-team --config FILE init --repo REPOSITORY
agentctl-team --config FILE doctor
agentctl-team --config FILE issue add --id ID --title TITLE --body-file BODY.md
agentctl-team --config FILE start --notify codex:VERIFIED_NATIVE_SESSION_ID
```

Review generated prompts, model, base branch, and integration policy. Put task
scope in issue bodies; shared stage prompts apply to every item. Each item
keeps one branch/worktree; stages start fresh sessions and interrupts resume the
current session. Keep concurrent manager
changes out of the integration checkout.

For Codex, inspect `CODEX_THREAD_ID` and verify it belongs to this conversation.
If absent, follow operations' session lookup; never assume the newest session
is yours or substitute an app GUID. Other harnesses have different delivery
limits. Failed discovery is a blocker; `--no-notifications` requires an explicit
user choice. Supply `--notify` on every restart.

With `--daemon`, the runner sends a completion alert when the backlog is fully
done and keeps running for new items. Each new batch can produce another
completion alert; idle polls and restarts preserve the recorded completion.

Launch detached with retained logs. Confirm startup and complete available
manager actions, then end your turn when waiting for workers or a necessary
user decision. Steering can arrive mid-turn; queued delivery needs a settled
turn. Do not poll to keep a turn open. Launching does not complete the request.

## Correct a running worker

Use `agentctl-team --config FILE issue interrupt ID "correction"` to stop and
resume the item's current worker in the same session. This preserves work and
tracks the replacement job. Use this command for team workers so scheduler
ownership stays correct. If the item remains `interrupting`, inspect summary
and repeat the same correction to reconcile its durable request.

## Respond

```sh
agentctl-team --config FILE status --summary
agentctl-team --config FILE issue show ID
agentctl-team --config FILE issue report ID --file REPORT.md
```

The built-in `hold` route pauses an item as `held` and sends an alert
when notifications are enabled. Resolve its blocker and use `issue retry ID`
to rerun the same stage with its existing worktree and history.

Inspect current state; alerts may be stale. Human stages require manager action,
not necessarily a user decision. Resolve authorized actions, submit Markdown
front matter `outcome: DECLARED_OUTCOME` between `---` lines plus the resolution,
and confirm the intended next state. The runner normally waits 30 minutes and
continues automatically after a report.

Preserve work/history. Never invent reports, edit ledger state, or duplicate
workers. Use configured human outcomes for item-specific recovery; `issue retry`
does not reset counts. Verify delivered behavior and checks, then communicate
the outcome and limitations. A `done` label alone is not proof.
