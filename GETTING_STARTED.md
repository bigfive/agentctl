# Running a backlog with agentctl-team

A coding task often takes several agent runs: implementation, review, fixes,
and integration. With several tasks in progress, someone must track which run
finished, pass its work to the next agent, limit concurrency, and handle blocked
items.

`agentctl-team` manages those handoffs. You add tasks to a local backlog and
configure stages with instructions and named outcomes. The runner starts each
stage through agentctl, reads the worker's report, and routes the task to its
next stage. Each task keeps its branch and worktree throughout, so a reviewer
receives the implementation files and report history.

For example, three bug fixes can each follow this workflow:

```text
implement → review → integration → done
               ↑          │
               └──────────┘ changes requiring another review
```

An agent pool limits how many workers run together. An integration pool with one
slot lets implementation and review proceed concurrently while merges happen
one at a time. A human stage pauses an item for a manager to inspect a blocker
and submit a decision. The runner records attempts, reports, and active jobs so
it can continue after a restart.

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

See [TEAM.md](TEAM.md) for configuration, reports, human stages, notification
setup, and recovery commands.
