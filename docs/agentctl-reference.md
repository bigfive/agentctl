# agentctl reference

## Commands

```text
agentctl start <harness> <prompt> [--cwd DIR] [--model M] [--request-id ID]
agentctl asyncwait <job> --notify <harness>:<session-id>
agentctl wait <job>... [--rewake] [--timeout SECONDS] [--json]
agentctl status <job>
agentctl read <job> [--stream | --stderr]
agentctl followup <job> <prompt> [--request-id ID]
agentctl cancel <job>
agentctl list
agentctl daemon status|start|stop|run
agentctl notify <harness>:<session-id> (--message TEXT | --message-file FILE) [--id ID] [--json]
agentctl-team --config FILE start
```

`start` prints only the job handle, so `a=$(agentctl start ...)` captures it
exactly. Add `--json` to most commands for the full record.

`start` and `followup` accept `--options-json` for explicit harness options.
For Codex, `{"reasoning_effort":"low","service_tier":"priority"}` maps to native
configuration overrides. Unsupported keys, values, or harnesses fail before
launch. `agentctl validate-options codex --options-json '{"reasoning_effort":"low"}'`
validates without launching. Follow-up inherits the previous job's options when
omitted; `{}` clears them. Older running daemons must be restarted before using
this interface; the CLI rejects them explicitly.

`notify` targets an existing native session directly. It tracks the transport
acceptance as a `session_delivery` job; `status` reports `succeeded` when the
harness accepts the message, not when the recipient responds. Supplying `--id`
makes identical retries return the same job and rejects changed message content.
The receiver may still deliver ambiguously on transport failure, so agentctl
never retries a failed or ambiguous send automatically. `--message-file` reads
UTF-8 text; `--message` handles short messages.

Three identifiers exist and are never interchangeable: the agentctl job handle
(`job_...`), the wait handle (`wait_...`), and the harness's own session id.

## Optional OpenCode tool

The optional plugin installs one model-facing tool named `agentctl` into
OpenCode. It wraps the CLI rather than asking the model to construct shell
commands. The tool supports `start`, `status`, `read`, `followup`, `cancel`,
and `list`. `start` and `followup` return a job handle immediately; the plugin
waits on agentctl's durable job and wakes the originating OpenCode session with
the authenticated plugin SDK client when it finishes.

Install it explicitly:

```bash
./install.sh --opencode-plugin
```

The plugin runs inside the OpenCode server, so it uses that server's own
authenticated client and current session ID. It does not depend on the
OpenCode desktop app's private random-port server. The installer places a
symlink to the CLI at `~/.config/opencode/plugins/agentctl/bin/agentctl`, and
the plugin prefers that plugin-relative path. `AGENTCTL_BIN` remains available
as an explicit override. The completion callback is best-effort if the server
itself is restarted; the job remains durable and is still available through
`status` and `read`.

## Harness notes

| Harness | Child runs as | Completion delivered by |
|---|---|---|
| codex | `codex exec --json` | Experimental Desktop IPC steering; queue when idle or steering fails |
| claude | `claude -p --output-format stream-json` | Claude's own wake: `agentctl wait`, or `wait --rewake` from an `asyncRewake` hook |
| cursor | `cursor-agent -p` on a new chat | `cursor-agent -p --resume` — terminal (`cursor-agent`) chats only |
| opencode | `opencode run --server` on a new session | `POST /api/session/<id>/prompt` on a running server, or the optional plugin's authenticated SDK callback |

Things to know per harness:

- **Cursor IDE chats can't be notified.** A message delivered to an IDE chat
  from the command line is not shown in the IDE, so `asyncwait` refuses IDE
  chat ids. From an IDE session, use `agentctl wait`. Push delivery works from
  `cursor-agent` in a terminal.
- **OpenCode needs a reachable v2 API server.** Run `opencode serve`, set
  `AGENTCTL_OPENCODE_URL`, and provide
  `AGENTCTL_OPENCODE_USERNAME` / `AGENTCTL_OPENCODE_PASSWORD` when the server
  uses Basic auth. The OpenCode plugin inherits the server's authenticated SDK
  client; use `agentctl wait` if the server is unavailable.
- **Codex** is often not on `PATH`. On macOS agentctl falls back to the copy
  bundled in `ChatGPT.app`, which may differ in version from a Homebrew or npm
  install. `PATH` always wins when present.

`asyncwait` checks that the target can actually receive a delivery *before*
storing the wait, so an unreachable target fails immediately rather than
silently at delivery time.

## One skill for all harnesses

`skills/agentctl/SKILL.md` is a single skill with a section per harness;
`install.sh` symlinks it into each harness's skills directory:

| Harness | Skills directory |
|---|---|
| Codex | `~/.codex/skills` |
| Claude Code | `~/.claude/skills` |
| Cursor | `~/.cursor/skills` |
| OpenCode | `~/.config/opencode/skills` (also reads `~/.claude/skills`) |

It is one skill rather than four because some harnesses read each other's
directories. OpenCode may report `duplicate skill name`; that is harmless, as
both paths lead to the same file.

## Design choices

- **Children run fully permissive.** Every child is started with its
  harness's skip-approvals flag, because an unwatched child that stops to ask
  a question would hang forever. Children can run any command in the working
  directory you give them — be deliberate about `--cwd`.
- **Exactly-once delivery, no silent retries.** Each wait carries a
  notification id derived from the wait, job, and terminal status, so a
  repeated event or daemon restart can't produce a second message. A failed
  delivery stays failed; the result is still readable with `agentctl read`.
- **One path per harness, no fallbacks.** If the chosen interface is
  unavailable the command fails and says so.
- **No universal transcript format.** `agentctl read --stream` returns the
  child's raw events in that harness's own shape.

## Exit codes

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
| 70 | child failed |
| 71 | child cancelled |
| 75 | harness binary or prerequisite missing |

## Configuration

| Variable | Purpose |
|---|---|
| `AGENTCTL_HOME` | store, socket, and log location (default `~/.agentctl`) |
| `AGENTCTL_<HARNESS>_BIN` | override a harness binary, e.g. `AGENTCTL_CODEX_BIN` |
| `AGENTCTL_OPENCODE_URL` | the OpenCode server (default `http://127.0.0.1:4096`) |
| `AGENTCTL_OPENCODE_USERNAME` | Basic-auth username (default `opencode`) |
| `AGENTCTL_OPENCODE_PASSWORD` | Basic-auth password for the user-owned server |
| `AGENTCTL_BIN` | absolute path to the agentctl executable used by the OpenCode plugin |

The daemon inherits the environment of whichever command first started it.
After changing a variable, run `agentctl daemon stop && agentctl daemon start`.
For OpenCode children, agentctl maps its configured credentials to
`OPENCODE_SERVER_USERNAME` and `OPENCODE_SERVER_PASSWORD` for `opencode run
--server`. The OpenCode child and notification adapters use the v2 `/api`
routes, verified with OpenCode 2.0.32.

On-disk layout:

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

The suite drives the real CLI, socket, and job store. Harness binaries are
replaced via the `AGENTCTL_*_BIN` overrides, so no agent credits are spent.

## Experimental Codex notification steering

UUID notification targets first try the running Desktop window through
`$CODEX_HOME/ipc/ipc.sock` (or `~/.codex/ipc/ipc.sock` when `CODEX_HOME` is absent).
The internal, versioned Desktop follower protocol forwards to `turn/steer`,
including active-turn selection and turn-id race handling. Exact session-name
targets retain CLI queue delivery. `AGENTCTL_CODEX_IPC_SOCKET` explicitly selects
an alternative Desktop IPC socket, including for isolated tests.

An idle-turn rejection uses `codex queue`. Any experimental steering error or
invalid response also falls back to queue, with a warning on stderr/daemon logs
and `steer_warning` in the delivery result. If the steer was accepted but its
reply was lost, fallback can deliver a duplicate. This is deliberate best-effort
behavior; successful steering is never also queued. Queue failures remain errors.
The Desktop IPC interface is internal and may change independently of the public
app-server API.
