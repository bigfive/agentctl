# Getting started with agentctl-team

`agentctl-team` lets you define a pipeline across Codex, Claude Code, Cursor,
and OpenCode, with a model and custom prompt for each stage. For example, Luna
can implement a task in Codex and Sonnet can review it in Claude Code.

Add tasks to the backlog. The runner launches a worker for each stage and uses
its reported outcome to choose the next stage. You configure the routes: pass
review, return for changes, or wait for a manager decision. Each task keeps its
branch, worktree, and reports as it moves between harnesses.

Pools limit concurrency, so several implementations can run together while
integration happens one task at a time. Attempts and active jobs are recorded
so the runner can continue after a restart.

For a minimal two-stage JSON configuration with inline prompts and four-worker
default concurrency, see
[the minimal pipeline](../TEAM.md#minimal-pipeline).

## Try one task

This example uses a four-line poem so you can inspect the complete workflow in
a disposable repository. It runs real Codex workers using the daemon's Codex
model and authentication configuration.

Requires Python 3, Git, and an authenticated Codex CLI. From this checkout,
install the commands and create the example in a new directory:

```sh
./install.sh --cli
python3 examples/poems/create.py /tmp/agentctl-team-example
agentctl-team --config /tmp/agentctl-team-example/pipeline.json doctor
```

The generator creates a Git repository, task files, and three stages:

- `write`: write a four-line poem and report `ready`.
- `review`: check it; report `passed`, or fix it and report `changes_made` to
  request another review.
- `integration`: commit the poem, merge it into the example repository's `main`
  branch, and report `complete`.

Read the generated briefs under `/tmp/agentctl-team-example/stages/`, then add
one task and run it:

```sh
agentctl-team --config /tmp/agentctl-team-example/pipeline.json issue add \
  --id rain --title 'Rain poem' \
  --body-file /tmp/agentctl-team-example/rain.md
agentctl-team --config /tmp/agentctl-team-example/pipeline.json start --no-notifications
```

This foreground run disables notifications and runs until the backlog settles.
A worker reporting `blocked` puts the item on hold for inspection. Check the
result and the recorded work:

```sh
agentctl-team --config /tmp/agentctl-team-example/pipeline.json status --summary
agentctl-team --config /tmp/agentctl-team-example/pipeline.json issue show rain
cat /tmp/agentctl-team-example/repo/poems/rain.md
git -C /tmp/agentctl-team-example/repo log --oneline
```

On successful completion, the item is `done` and the poem is committed on
`main`. `issue show` includes the stage reports. Add the generated `ocean.md`
task to try concurrent work; the example allows two workers and one integration
worker at a time.

## Use it in your project

Run `agentctl-team --config /path/to/pipeline.json init --repo /path/to/repo`
to generate a coding workflow. Review its stage briefs, model, limits, and merge
instructions, then add your tasks. Use `start --notify HARNESS:NATIVE-SESSION-ID`
to send handoffs and runner alerts to your manager session.

See [the configuration and command reference](agentctl-team-reference.md) for configuration, reports, human stages, notification
setup, and recovery commands.
