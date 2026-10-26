# agentctl-team

Define a pipeline that orchestrates work across Codex, Claude Code, Cursor, and
OpenCode. Each stage has its own harness, model, custom prompt, and outcome
routes. Add tasks to the backlog and the runner passes each one through your
pipeline.

A simple pipeline could use Luna to implement and Sonnet to review:

```text
Implement                  Review
Codex / Luna        →      Claude Code / Sonnet
```

Or give each part of delivery its own stage:

```text
Check task readiness → Implement    → Review             → QA     → Integrate
Codex / Sol            Codex / Luna   Claude Code / Opus   Cursor   Claude Code / Sonnet
```

Write prompts for what each worker should do and reports for the outcomes it
can return. A review can pass the task forward, send it back for changes, or
request a manager decision. Configure the model IDs supported by your installed
harnesses.

Each task keeps its branch, worktree, and report history across stages. Pools
control concurrency: run several implementations at once while allowing one
integration worker at a time. The runner records progress so it can continue
after a restart.

## Minimal pipeline

Save as `pipeline.json` in the root of your Git repository, with `agentctl` on
`PATH`:

```json
{
  "stages": {
    "implement": {
      "worker": {"harness": "codex", "model": "gpt-6-luna"},
      "prompt": "Implement the task and run relevant checks.",
      "outcomes": {"ready": "review", "blocked": "hold"}
    },
    "review": {
      "worker": {"harness": "claude", "model": "sonnet"},
      "prompt": "Review the changes against the task and report your findings.",
      "outcomes": {"passed": "done", "needs_changes": "implement", "blocked": "hold"}
    }
  }
}
```

The first stage runs first. Defaults allow four concurrent workers, with a
30-minute timeout per attempt. Each inline prompt gets task-reading and
report-submission tools automatically.
`passed` completes this pipeline with the changes in the task's worktree.
`hold` pauses the task and notifies the manager; use `issue retry ID` after
resolving its blocker.

Add a task and start the pipeline:

```sh
agentctl-team --config pipeline.json issue add --id fix --title 'Fix the bug' --body-file task.md
agentctl-team --config pipeline.json start --notify codex:NATIVE-SESSION-ID
```

Correct a running task with:

```sh
agentctl-team --config pipeline.json issue interrupt fix "Use the existing parser instead."
```

Use model IDs supported by your harness installations. See the
[reference](docs/agentctl-team-reference.md) for stage files, pools, notifications,
and recovery, or [the walkthrough](docs/agentctl-team-getting-started.md).
