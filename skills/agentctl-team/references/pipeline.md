# agentctl-team

`agentctl-team` runs configurable stages over a local backlog through agentctl's
public CLI. Stage prompts and scripts define the work performed; pools and
outcome routes control concurrency and progression.

Requires Python 3 and Git, using only the Python standard library.
Configuration files use JSON. Run it through `bin/agentctl-team`.

## Initialize a project

```sh
agentctl-team --config /path/to/pipeline.json init --repo /path/to/repository
agentctl-team --config /path/to/pipeline.json doctor
agentctl-team --config /path/to/pipeline.json start --notify codex:NATIVE-SESSION-ID
```

The config parent directory must exist. `init` creates the config and a sibling
`pipeline-stages/` directory, refusing to overwrite either. `--repo` defaults to the current directory;
`--model` defaults to `gpt-6-luna`. The checked-out branch becomes the explicit
base branch. Review generated prompts before starting: the starter uses four
shared slots, serial integration, low reasoning effort, 30-minute attempt/human
wait timeouts, and four executions per stage before human overflow. Starter human retry routes explicitly reset the destination-stage count for
that item, preserving history.

## Poem pipeline example

Run from this checkout:

```sh
python3 examples/poems/create.py /tmp/my-poem-pipeline
bin/agentctl-team --config /tmp/my-poem-pipeline/pipeline.json doctor
bin/agentctl-team --config /tmp/my-poem-pipeline/pipeline.json issue add \
  --id rain --title 'Rain poem' --body-file /tmp/my-poem-pipeline/rain.md
bin/agentctl-team --config /tmp/my-poem-pipeline/pipeline.json issue add \
  --id ocean --title 'Ocean poem' --body-file /tmp/my-poem-pipeline/ocean.md
bin/agentctl-team --config /tmp/my-poem-pipeline/pipeline.json start --notify codex:NATIVE-SESSION-ID
```

This starts real Codex workers through the installed agentctl. Each item gets
one branch and worktree. Writers leave poems uncommitted. New reviewer sessions
read those same files. Integration workers commit and merge into the disposable
main checkout, one at a time, following the integration brief.

Inspect the main checkout's `poems/`, the item worktrees, and
`repo/.git/agentctl-team/state.json`. Worktrees and branches are retained after
completion for inspection; cleanup is manual.

## Definition

Only `stages` is required at the top level. Each LLM stage requires `worker`
(with explicit `harness` and `model`), `outcomes`, and exactly one of `prompt`
or `files`. Stage `type` defaults to `llm`; set `type: human` for a human stage.

| Optional field | Default when absent |
|---|---|
| `repo` | Directory containing the config; must be a Git repository root |
| `base_branch` | Checked-out branch; detached HEAD requires an explicit branch |
| `agentctl` | Executable resolved from `PATH` |
| `worktrees` | `<git-common-dir>/agentctl-team/worktrees` |
| `poll_seconds` | `2` seconds |
| `pools` | `{"default": 4}` |
| Stage `pools` | `["default"]` for LLM stages |
| Stage `timeout_seconds` | `1800` seconds (30 minutes) per attempt |
| `entry` | First stage in JSON order |
| `launch_message` | `Read .agentctl-team/brief.md and follow its instructions.` |

Defaults apply only to absent fields. Empty, malformed, or unsupported values
fail validation. Custom pool definitions replace the default pool mapping;
stages must reference pools present in that mapping.

Notification destinations are supplied on each `start` with `--notify` or
`--no-notifications`. Paths are relative to the config file unless absolute.
`model: null` selects the harness's configured default model. Timeouts and
polling intervals are finite positive seconds; pool capacities are positive
integers.

This example overrides the defaults to serialize integration:

```json
{
  "repo": "/absolute/project",
  "base_branch": "main",
  "agentctl": "/absolute/bin/agentctl",
  "worktrees": "/absolute/worktrees",
  "poll_seconds": 1,
  "entry": "write",
  "launch_message": "Read .agentctl-team/brief.md and follow its instructions.",
  "pools": {"default": 4, "integration": 1},
  "stages": {
    "write": {
      "pools": ["default"],
      "timeout_seconds": 1800,
      "worker": {"harness": "codex", "model": null},
      "files": "stages/write",
      "outcomes": {"ready": "integration", "blocked": "hold"}
    },
    "integration": {
      "pools": ["default", "integration"],
      "timeout_seconds": 1800,
      "worker": {"harness": "codex", "model": null},
      "files": "stages/integration",
      "outcomes": {"complete": "done", "blocked": "hold"}
    }
  }
}
```

A pool is a named counting semaphore. A run acquires one slot from every named
pool atomically, and holds those slots until its worker terminates. Queued and
held items consume no slots. One item has at most one active run. Queues are
unbounded; pools constrain active runs. Items with more completed attempts are
dispatched first, then priority and arrival order. Review loops can route back
to the same stage and use per-stage execution limits. Startup rejects stages
with no route to `done`, `hold`, or a human stage, including closed cycles.
Configured overflow routes also count as exits. A review loop with an exit is
valid; static validation cannot prove that workers will choose it.

## Worker options

`worker.options` is optional. When absent, no explicit overrides are requested;
the harness's configuration applies. `{}` requests no overrides. Currently
Codex supports:

```json
{
  "harness": "codex",
  "model": "gpt-6-luna",
  "options": {"reasoning_effort": "low", "service_tier": "priority"}
}
```

`reasoning_effort` accepts `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, or
`max`; `service_tier` accepts `auto`, `default`, `flex`, or `priority`. The selected
Codex model/provider must also support the chosen value; a native rejection is
an execution error, never a fallback. Other harnesses currently reject nonempty
options explicitly. Options are passed through agentctl's public
`--options-json` interface and validated through its public `validate-options`
command. An old agentctl executable or daemon fails visibly rather than ignoring
requested options. Restart an older daemon separately before requesting them.

The recorded attempt contains the options used. Every LLM execution starts a
new session.

## Prepared files and prompts

Each LLM stage requires exactly one of `prompt` or `files`.

Use `prompt` for inline instructions, for example:

```json
"prompt": "Implement the task and run relevant checks. Report ready or blocked."
```

Inline prompts must be nonempty strings and support the template variables below.
The runner prepares a brief, report template, and executable task-reading and
report-submission scripts. The brief includes the stage's allowed outcomes and
instructions to submit the report and exit. The default `launch_message`
directs workers to read this brief.

Use `files` to supply your own brief and scripts:


Each stage directory must contain UTF-8 `brief.md`, `report.md`, executable
`read-issue.sh`, and executable `submit-report.sh`. Additional files and nested
directories are allowed. Symlinks are rejected. Source executable bits are
preserved. Everything is rendered into the item's `.agentctl-team/` before the
worker launches. `launch_message` is rendered from the configured template.

Templates use `{{ name }}` with these variables:

- `item_id`, `title`, `stage`, `attempt`, `worktree`, `branch` are literal values.
- `team_command` is the shell-quoted installed team entry point.
- `config_path` is the shell-quoted configuration path.

Unknown variables and malformed expressions fail validation. Literal values
are not automatically shell-escaped; use them as text, and do not interpolate
untrusted titles into shell commands. The generated IDs are restricted to
letters, digits, underscores and hyphens. The example uses only restricted IDs
in shell arguments.

A read script can be:

```sh
#!/bin/sh
set -eu
{{ team_command }} --config {{ config_path }} issue show {{ item_id }}
```

`issue show` returns the current local backlog item as JSON, including edits
made during a run.

A submission script can be:

```sh
#!/bin/sh
set -eu
{{ team_command }} --config {{ config_path }} report \
  --attempt {{ attempt }} --file .agentctl-team/report.md
```

The standard report template is:

```markdown
---
outcome: <replace-with-outcome>
---

Replace this text with your findings, work completed, and any blockers.
```

The front matter must contain exactly `outcome: NAME`, with an unquoted declared
outcome. Extra front-matter fields and unchanged placeholders fail validation.
Use the remaining Markdown for findings, completed work, and blockers.

Reports are durably recorded under a state lock. Identical repeat submissions
return the original receipt; conflicting submissions fail. Old attempt tokens
cannot submit for a newer attempt. Tokens identify attempts; they are not an
OS security boundary.

Submission does not cancel the worker or release slots. Routing happens only
after successful worker termination and a valid report. The configured brief
must tell workers to stop after submission. A failed run with a report still
fails.

## Lifecycle and commands

```sh
agentctl-team --config FILE start                 # run until settled
agentctl-team --config FILE start --daemon        # remain available for new items
agentctl-team --config FILE start --once          # one scheduler tick
agentctl-team --config FILE doctor
agentctl-team --config FILE status                # JSON
agentctl-team --config FILE pause
agentctl-team --config FILE resume
agentctl-team --config FILE stop --drain
agentctl-team --config FILE stop --cancel-active

agentctl-team --config FILE issue add --id ID --title TITLE --body-file FILE
agentctl-team --config FILE issue add --id ID --title TITLE --body-file FILE --dep OTHER
agentctl-team --config FILE issue list                 # compact table
agentctl-team --config FILE issue list --json          # full records
agentctl-team --config FILE issue show ID
agentctl-team --config FILE issue edit ID --body-file FILE
agentctl-team --config FILE issue edit ID --title TITLE --priority 1
agentctl-team --config FILE issue edit ID --dep OTHER --dep ANOTHER
agentctl-team --config FILE issue edit ID --clear-deps
agentctl-team --config FILE issue report ID --file REPORT.md
agentctl-team --config FILE issue retry ID
```

Optional add arguments: priority defaults to 2 (lower first); dependencies
default to none. Known unmet dependencies wait before worktree preparation.
Priorities range from 0 to 4. Dependencies must identify existing items.
Editing with `--dep` replaces the complete dependency list; `--clear-deps` removes
it. Cycles, missing dependencies, and duplicates are rejected atomically.
Dependencies can change only while an item is queued, waiting for a human,
held, or in error; the
runner does not retroactively apply them to active or completed work. Title,
body, and priority edits are live. At least one edit field is required.

Routing to `hold` sets the task state to `held`, retains its stage and
worktree, and notifies the manager when notifications are enabled.
Use an explicit human stage for discussion and resolution. `issue retry` can
rerun held or failed LLM work after the cause is resolved; it always starts a
new session and preserves the branch and worktree. Human stages cannot be
retried this way: submit a human report instead. The old `issue resume` and
answer-file mechanism have been removed. Existing pending legacy continuations
are rejected explicitly, not silently reinterpreted.

Operational errors (missing report, failed worker, malformed preparation) remain
as `error`, separately from routing to `hold`. No automatic
retry or substituted outcome is used. A timeout cancels the worker and waits
for its terminal status before releasing capacity and marking the item as an
error. Status failures stop the scheduler with a visible error while retaining
active ownership. Errors identify the item, stage, worktree, branch, and
recorded job/attempt where available. Preparation errors include paths to
inspect; nothing is silently cleaned up or repaired. Inspect the cause and
restart to reconcile.

A plain `stop` exits the scheduler while retaining active jobs for a later
restart. `--drain` observes existing runs without launching new ones.
`--cancel-active` cancels runs and observes their termination. SIGINT/SIGTERM
exit without killing workers. A subsequent `start` clears the stop request.
A paused runner remains paused until `resume`.

## Human stages and execution limits

A human stage has no worker, pool, files, or timeout:

```json
"manager": {
  "type": "human",
  "outcomes": {"clarified": "review", "needs_changes": "work"}
}
```

A worker can route any declared outcome to it, including `"blocked": "manager"`.
The name `blocked` has no special meaning to the runner. On entering a human
stage, the item becomes `waiting-human`, a stage attempt is recorded, and no
worker slot is acquired. A worktree is created only when the first LLM stage
runs. The runner stays alive while human input is pending and no LLM work is runnable
or active. `human_wait_timeout_seconds` defaults to 1800 (30 minutes); configure
a positive number to change it, or `null` to wait indefinitely. A report making
work runnable clears the idle timer. If the timeout elapses, the runner exits
and sends a stop alert explicitly stating that the human-wait timeout elapsed.
Items and reports remain intact. This timeout also applies to `--daemon`;
explicit stop/drain/cancel and execution errors take precedence.

Edit the live issue if needed, then submit the same Markdown report format:

```sh
agentctl-team --config FILE issue report ID --file clarification.md
```

```markdown
---
outcome: clarified
---

A title is not required. Keep exactly four nonempty lines.
```

The report command validates the current human stage's outcome, records the
report, and routes the item atomically. It requires the item to be waiting at a
human stage. An active runner, or the next `start`, dispatches the next stage.
Each LLM execution, including a return from a human stage,
starts fresh and reads the issue/report history. Human reports are included in
`issue show` just like worker reports. Repeating the command after the item has
left the human stage fails.

An optional limit must include an explicit overflow destination:

```json
"max": 3,
"overflow": "manager"
```

Limits count prepared executions per item, per stage, across all visits and
retries. Failed attempts count. Human-stage entries count too. An interrupted
launch recovered with the same attempt identifier does not count twice. Before
execution four, the runner records an overflow transition and routes to the
configured destination without launching a worker. The destination can be any
stage, `hold`, or `done`. Missing `max` means unlimited executions.
`max` without `overflow`, invalid limits/destinations, and
cycles consisting entirely of overflow routes fail validation.

Counts accumulate per item/stage until an explicit reset route is taken. String
routes never reset counts; returning to an exhausted stage via a string will
overflow again. Outcome routes can instead use an object:

```json
"manager": {
  "type": "human",
  "outcomes": {
    "review": {"to": "review", "reset_count": true},
    "implement": {"to": "work", "reset_count": true},
    "integrate": "integration",
    "cancel": "done"
  }
}
```

A `review` report resets only the current item's review-stage count, then queues
a fresh review. Other stages and items are untouched. Attempts/reports are never
deleted: the ledger records a counting boundary and a count-reset transition,
including the previous count. It survives restarts. The object requires exactly
`to` and boolean `reset_count`; false routes without resetting. Resetting `done`
or `hold` is invalid because they are not stages. Only outcome routes support
objects; `overflow` and `on_error` remain destination strings. An explicit reset
can allow repeated cycles beyond the original limit; use it deliberately.
The starter human stages expose reset-enabled retry/review/implement outcomes,
plus integrate/cancel. `issue retry` itself still never resets counts.

## Notifications

`start` prints manager guidance to stderr immediately after startup validation,
with flushing so it is visible even when output is captured. Its stdout remains
JSON. With notifications enabled, the guidance tells an LLM manager to end its
turn when it has no other work, rather than polling or waiting for queued
notifications. On receipt, read the issue/history, act, and end the turn again.
`start --daemon` also warns that it stays running and should be launched as a
background process or service, rather than awaited inside a manager turn.

Enable notifications for each runner invocation:

```sh
agentctl-team --config pipeline.json start --notify codex:NATIVE-SESSION-ID
```

Specify it again on restart; no previous destination is silently reused.
To intentionally disable new alerts use `start --no-notifications`. Omitting
both flags, or supplying both, fails before starting the runner. This also
applies to `--once` and `--daemon`. Identify the native session as documented
in the installed agentctl-team skill; do not opt out silently when lookup fails.
Old `notifications` config fields fail with a migration error. Already-recorded
notifications retain their original destination and delivery state.

The runner records an outgoing notification when an item enters a human stage,
routes to `hold`, or encounters an operational error. Normal stage completions
do not send alerts. Messages begin with `[Message from: 'agentctl-team STAGE']`, identifying the
stage that requested attention. Human handoffs quote that stage's report prose
without routing front matter, explain what to do next, and include inspection
and submission commands. Overflow alerts identify the stage that reached its
limit. Execution errors retain the original error and diagnostic context.
A native harness session ID is required; a T3 conversation ID is not assumed to
be one. Transport support and user-visible delivery depend on that harness.
A successful `agentctl notify` confirms transport acceptance, not a human read. Notification messages use a plain-text code block so report Markdown and YAML separators cannot become headings.

Every runner invocation records its start and stop under `runs` in the state
file. With notifications enabled it attempts a separate runner-stopped message
before exiting, including backlog completion, waiting for human or held work,
requested stop, completed drain/cancellation, diagnostic `--once` exit,
SIGINT/SIGTERM, and handled runtime or startup-validation errors. The message
identifies any workers still running and tells the manager to restart the
runner after resolving outstanding work. A second stop call for the same run
does not create another alert. SIGKILL, process/machine crashes, invalid config
that cannot be loaded, and failure to acquire the scheduler lock cannot reliably
send a stop notification. No external supervisor is installed.

Stop-alert failures are recorded and exit nonzero. An existing failed alert is
not retried merely to send the separate runner-stopped alert. If both the runner
and its stop notification fail, stderr preserves both errors.

State includes a durable notification outbox. Each message has a stable ID and
moves through pending → sending → sent. It is not resent on each status poll or
restart. A transport failure records `failed`; a crash during delivery records
`uncertain` on restart. Both stop the scheduler loudly because delivery may
already have happened. No automatic resend is attempted.

```sh
agentctl-team --config FILE notification list
agentctl-team --config FILE notification acknowledge NOTICE_ID \
  --note "Inspected recipient and agentctl delivery records; handled manually"
```

Acknowledgement records the operator's resolution; it does not resend the
message, resolve the item, or clear an execution error. Inspect the recipient
and transport records before manually sending anything again. Pending/sent
messages retain the target recorded when created even if config changes later.

## Human-handoff example

`examples/human-handoff/create.py NEW_DIRECTORY`
creates a Luna-only example. Add `rain` using its `pipeline.json` and `rain.md`;
add `limit` using its `overflow-pipeline.json`. Both definitions use the same
local backlog. The main definition includes every stage and runs both items.
The rain item follows write → review → manager → review → integration. The
limit item runs `loop` once, then overflows into `escalation`. Prepared manager
and overflow reports are supplied in the example directory. Run the scheduler with `start --notify HARNESS:NATIVE-ID`,
inspect the waiting items, submit each human report with `issue report`, then
start again. Use an explicitly isolated `AGENTCTL_HOME` for the test if needed;
do not restart the shared daemon implicitly.

## Validation and persistence

`start` checks stage files, routes, pools, required values, the Git base branch,
executable availability, reserved directory collisions, dependency cycles,
stages trapped without an exit, and the scheduler lock.
It configures `/.agentctl-team/` in the repository's Git exclude file.
`doctor` performs the same checks and exclusion setup without starting workers.
A tracked or existing `.agentctl-team` in the root checkout is an error.

The ledger is in the common Git directory's `agentctl-team/`:

- `state.json`: atomic state document under `state.lock`.
- `scheduler.lock`: exclusive runner ownership.
- `events.jsonl`: operational event log; the state document is authoritative.
- `attempts/<token>/`: recorded definition, rendered launch prompt and initial
  prepared files. The next attempt also archives the previous worker files.

Running attempts retain their recorded stage definition and routing. Edited
configurations are loaded on the next CLI invocation/restart; a resident runner
does not hot-reload them. Templates are rendered when a new attempt is prepared.
Do not remove pools/stages still referenced by unfinished items.

Launches use agentctl's public `--request-id`, stable across recovery. An
interrupted launch retries the same request ID; agentctl either returns the
existing job or reports an ambiguous launch explicitly. Interrupted worktree
or file preparation requires inspection and explicit retry; existing paths
are not overwritten silently. Existing legacy delivery-queue state is rejected
rather than reinterpreted. There is no automatic migration.

## Worker permissions and testing

Workers run with the user's permissions and can edit generated files. The
starter's integration script checks for tracked `.agentctl-team` files before
merging.

Run the scheduler tests:

```sh
python3 -m unittest discover -s tests -p test_team.py
```

Those tests use real Git and a controlled agentctl CLI dependency. The poems
example exercises the application with real LLM workers.

LLM stages may configure `"on_error": "manager"` (or another stage/`hold`).
Terminal failed/cancelled runs, missing reports, and preparation errors retain
the original error in attempt/item history and route there without inventing
a report. With no `on_error`, the item stays in error as before. Uncertain
launches or status failures retain active ownership and stop loudly; they do
not route while a worker may still be running.

## Manager notification templates

`init` writes editable `<config-stem>-notifications/*.md` files. Optional
`notification_templates` maps any of `human`, `error`, `held`, `runner-stopped`,
`runner-completed`, `runner-error`, `human-wait-timeout`, and `runner-lost` to a file relative to the config. Unspecified kinds use built-in
defaults; empty, inaccessible, or invalid files fail validation. Restart a running
runner to apply edits. Watchdogs retain the configuration captured at launch.

Each template can replace the entire message and use these literal variables:
`{{ context }}`, `{{ source_stage }}`, `{{ item_id }}`, `{{ title }}`,
`{{ stage }}`, `{{ outcomes }}`, `{{ issue_command }}`, `{{ report_command }}`,
`{{ status_command }}`, `{{ start_command }}`, `{{ reason }}`, `{{ error }}`.
`context` contains the factual handoff/report and action commands. Item-specific
fields are empty for runner notices. `reason` contains the runner's factual
context for runner notices. `error` contains the item's current error, if any.
Templates are single-pass: braces in report prose are data, not expressions.
All messages are fenced as plain text to avoid accidental Markdown headings.
