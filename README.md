# agentctl

One small CLI so an agent in one harness can start work in another, be told
when it finishes, and read the result.

Supported harnesses: **codex**, **claude**, **cursor**, **opencode**.

## Install

No dependencies beyond Python 3.

```bash
./install.sh
```

That puts `agentctl` on your `PATH` and installs the matching skill into every
harness it finds, so each agent knows how to use it without being told. Both
are symlinks, so `git pull` updates them.

```bash
./install.sh --cli        # the command only
./install.sh --skills     # the skills only
./install.sh --uninstall  # remove both, leaving the job store alone
```

Confirm it worked:

```bash
agentctl daemon status
```

## The path this is built for

An agent starts a child, registers a wait, and keeps working. When the child
reaches a terminal state, the completion is delivered into the parent's live
session.

```bash
a=$(agentctl start claude "Write the migration and run the tests")
agentctl asyncwait "$a" --notify codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3
```

`asyncwait` hands the wait to a local daemon and returns immediately. The wait
does not depend on the parent's background shell staying alive, and it does not
depend on the daemon staying alive either.

```text
parent agent
  |
  | starts child, registers the wait
  v
agentctl CLI  --->  local daemon  --->  child process
                         |
                         | terminal state
                         v
                    notify adapter
                         |
                         v
                 parent harness session
```

## Commands

```text
agentctl start <harness> <prompt> [--cwd DIR] [--model M]
agentctl asyncwait <job> --notify <harness>:<session-id>
agentctl wait <job>... [--rewake] [--timeout SECONDS]
agentctl status <job>
agentctl read <job> [--stream | --stderr]
agentctl followup <job> <prompt>
agentctl cancel <job>
agentctl list
agentctl daemon status|start|stop|run
```

`start` prints only the job handle, so `a=$(agentctl start ...)` captures it
exactly. Add `--json` to most commands for the full record.

Three kinds of identifier exist and are never interchangeable: the agentctl job
handle (`job_...`), the harness's own session id, and the wait handle
(`wait_...`).

## The completion message

```text
[agentctl completion]
job: job_9f2c11a7e4d0
child: claude:6b1f9c8e-...
harness: claude
status: succeeded
result: Migration written; 24 tests pass.
full: agentctl read job_9f2c11a7e4d0
```

The parent is told where to read the full result rather than being handed an
unbounded transcript.

## How each harness is reached

| Harness | Child launch | Completion delivery |
|---|---|---|
| codex | `codex exec --json` | `codex queue --thread` — a durable queue, delivered when the thread goes idle |
| claude | `claude -p --output-format stream-json` | none; Claude's own wake, via `agentctl wait` or an `asyncRewake` hook |
| cursor | `cursor-agent -p` on a chat from `create-chat` | `cursor-agent -p --resume` — no queue, so a completion can land beside a turn already running |
| opencode | `opencode run --attach` on a pre-created session | `POST /session/<id>/prompt_async` on the running server |

One path per harness. There is no fallback between transports: if the chosen
interface is unavailable, the command fails and says so.

## Teaching each harness to use it

Each harness gets a skill, installed by `install.sh` into the directory that
harness reads. The skill is what makes an agent reach for `agentctl` on its
own, and it carries the one thing that genuinely differs per harness: how an
agent finds **its own** session id in order to be notified.

| Harness | Skill | Installed to |
|---|---|---|
| Codex | [skills/codex](skills/codex/SKILL.md) | `~/.codex/skills/agentctl/` |
| Claude Code | [skills/claude](skills/claude/SKILL.md) | `~/.claude/skills/agentctl/` |
| Cursor | [skills/cursor](skills/cursor/SKILL.md) | `~/.cursor/skills/agentctl/` |
| OpenCode | [skills/opencode](skills/opencode/SKILL.md) | `~/.config/opencode/skills/agentctl/` |

All four harnesses use the same skill format — a directory holding `SKILL.md`
with `name` and `description` frontmatter — so the four skills differ only in
content, not in shape. OpenCode also scans `~/.claude/skills` by default, so it
may see the Claude skill as well; its own skill is the one written for it.

Read them directly if you want the per-harness detail: they are plain
markdown, and they are the only copy of these instructions.

## What this does not do

- **No permission or approval handling.** Every child runs fully permissive
  (`--dangerously-bypass-approvals-and-sandbox`, `--dangerously-skip-permissions`,
  `--force`, `--auto`). An unwatched child that stops to ask a question would
  hang forever, so agentctl removes the question instead of answering it. That
  is a real risk and you are accepting it: children can run any command in the
  working directory you give them.
- **No silent retries.** A failed delivery stays failed. Retrying could
  duplicate a follow-up in a parent's context, which is worse than a missing
  one — and the result is always still readable with `agentctl read`.
- **No fallback between transports or CLIs.**
- **No universal transcript format.** `agentctl read --stream` gives you the
  child's raw events, in that harness's own shape.

## Failure semantics

Every failure is terminal and visible, with its own exit code:

| Code | Meaning |
|---|---|
| 0 | success |
| 2 | reserved for Claude's `asyncRewake` contract (`wait --rewake`) |
| 64 | usage error |
| 65 | unknown job handle |
| 66 | invalid notification target |
| 67 | unsupported operation |
| 68 | delivery failed |
| 69 | daemon unavailable |
| 70 | the child failed |
| 71 | the child was cancelled |
| 75 | a harness binary or prerequisite is missing |

Delivery is exactly once. Each wait carries a notification id derived from the
wait, the job, and the terminal status, so a repeated terminal event or a
daemon restart cannot create a second follow-up.

## Environment

| Variable | Purpose |
|---|---|
| `AGENTCTL_HOME` | store, socket, and log location (default `~/.agentctl`) |
| `AGENTCTL_CODEX_BIN` etc. | override a harness binary path |
| `AGENTCTL_OPENCODE_URL` | the running OpenCode server (default `http://127.0.0.1:4096`) |

`codex` is often absent from `PATH`; on macOS agentctl also looks in
`/Applications/ChatGPT.app/Contents/Resources/codex`. That bundled copy is a
separate build from a Homebrew or npm install and updates on the app's own
schedule, so the two can drift apart in version. `PATH` is always preferred,
and the bundle is only reached when no `codex` is on `PATH` at all.

The daemon inherits the environment of whichever command first started it. If
you change one of these variables, restart it with `agentctl daemon stop &&
agentctl daemon start`.

## Layout

```text
~/.agentctl/
  daemon.sock, daemon.pid, daemon.log
  jobs/<job-id>/job.json      the job record
  jobs/<job-id>/stream.jsonl  the child's raw events
  jobs/<job-id>/stderr.log    the child's stderr
  jobs/<job-id>/result.txt    the child's final message
```

## Tests

```bash
python3 -m unittest discover -s tests
```

23 tests drive the real CLI, the real socket, and the real job store. Only the
harness binaries are substituted, through the documented `AGENTCTL_*_BIN`
overrides, so the suite spends no agent credits.
