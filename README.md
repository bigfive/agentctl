# agentctl

Discover local agent sessions, start work, address existing sessions, and observe
completion across Codex, Claude Code, Cursor, and OpenCode. Python 3; standard
library only.

For configurable multi-worker backlogs, see [agentctl-team](TEAM.md) and
[the getting-started guide](docs/agentctl-team-getting-started.md).

## Install and upgrade

```sh
./install.sh                   # CLI and shared harness skill, installed as symlinks
./install.sh --cli
./install.sh --skills
agentctl daemon status
```

The daemon keeps imported code in memory. After upgrading, finish active jobs,
then run `agentctl daemon stop` and `agentctl daemon start`. New session operations
reject an incompatible daemon before dispatching work. Stopping the daemon does
not cancel its children; recovery reads their persisted records.

## Find and read sessions

Discovery includes sessions created outside agentctl. It reads Codex active and
archived JSONL, Claude project transcripts, Cursor CLI SQLite, Cursor IDE
`User/globalStorage/state.vscdb` composers and bubbles (including archived chats
and labelled subagents), legacy IDE transcripts, and OpenCode SQLite or the
configured server. Cursor's conversation search index also exposes metadata for
locally indexed cloud or uncached chats; those entries have `capability.read:
false` when no local history is available. No hook setup is needed.

```sh
agentctl sessions list --json
agentctl sessions list --harness claude --filter "migration"
agentctl sessions history codex:<thread-id> --limit 30
agentctl sessions history claude:<session-id> --limit 30 --offset 30
agentctl sessions history cursor:<chat-id> --raw --json
```

Addresses are `harness:native-id`. A discovered session's `kind` distinguishes
Claude roots/subagents and Cursor CLI/IDE/cloud records. Readability and sending
are separate capabilities. Normal history preserves full message text; `--raw`
includes native records and tool activity. Cursor CLI history follows the native
root's ordered message references when recognised. Cursor IDE history follows
`fullConversationHeadersOnly`, excludes orphaned bubbles, and preserves native
tool bubbles in raw output. IDE chats remain unsupported for CLI send/resume.
Legacy blob fallback records
carry `ordering: unknown`; copied SQLite snapshots are labelled.

### Search conversation contents

```sh
agentctl sessions search 'coreplan-web/pull/5100'
agentctl sessions search 'review CSV upload' --mode full-text
agentctl sessions search '5100' --harness cursor --json
agentctl sessions search '5100' --include-tools
agentctl sessions search-status --json
agentctl sessions reindex
agentctl sessions reindex --include-tools
```

The daemon maintains a persistent SQLite FTS5 index at
`$AGENTCTL_HOME/search.sqlite3` (normally `~/.agentctl/search.sqlite3`). The first
search builds it; subsequent daemon starts resume maintenance. Every 60 seconds
the daemon discovers sessions, compares native metadata and file/WAL fingerprints,
and replaces only changed conversations. Replacing a conversation reconciles
edits and changed branches as well as appended messages. Removed sessions are
removed from the index; failed reads retain old history with an explicit error.

Literal search (the default) matches substrings, including punctuation in URLs
and identifiers. Full-text search matches all supplied words using FTS5.
Results group matches by native session and prefer matches in user prompts,
then titles, then assistant text. Each result includes an excerpt, role, message
position, and a command to read nearby history. Indexed cloud references can
match titles even when their history is unavailable.

Every search reports freshness and per-harness coverage. An incomplete result
can mean unreadable histories, missing native messages, or a source/read error;
`search-status --json` includes the affected addresses and reasons. Search does
not contact a provider to fetch missing cloud transcripts. Tool output is
excluded by default. `--include-tools` adds supported native tool output to the
index and includes it in that search; `reindex` without that flag returns to a
conversation-text-only index. Search indexing does not alter native stores.

## Start, send, and resume

```sh
job=$(agentctl start claude "Implement the migration and run the checks" --model sonnet --cwd /repo)
handle=$(agentctl send codex:<thread-id> "Investigate the failing tests")
job=$(agentctl resume claude:<session-id> "Continue the implementation" --cwd /repo)
agentctl start codex "Review the change" --from codex:<caller-thread-id>
agentctl send opencode:<session-id> "Run the tests" --no-notify
agentctl start claude "Quick check" --require-callback
```

`start` creates a session. `send` uses a supported existing-client transport;
`resume` runs a managed child against an existing stored session. Claude resume
opens a headless client and does not inject into an existing interactive client.
Addressing an unknown session fails rather than creating one.

Requests have durable result handles even when caller identity is unavailable
or `--no-notify` is supplied. Automatic one-shot subscriptions are created
before managed children launch or messages are dispatched. `--require-callback`
rejects before dispatch if a callback route cannot be verified. It cannot be
combined with `--no-notify`.

| Target | Dispatch |
|---|---|
| Codex | Native queue; correlate the request marker with its native turn |
| Claude | External push unsupported; explicit managed `resume` is available |
| Cursor CLI | Managed CLI resume, with busy checks and stored full result |
| Cursor IDE/cloud | External sending unsupported |
| OpenCode | Queued v2 prompt on the server owning that session; correlate message IDs |

A busy target without a durable queue is rejected. Opting out of notifications
does not bypass busy checks. Unsupported, missing configuration, and an
unreachable route are reported separately. An ambiguous acceptance is recorded
as unknown and never automatically resent.

Codex preflight checks the app-server control route when available, or the
Desktop queue IPC listener and locally stored target. The control socket is
not required for queue delivery. These read-only checks submit no prompt;
actual callback acceptance is recorded only when the queue command succeeds.

## Results and listeners

```sh
agentctl status <job> --json
agentctl read <job>
agentctl wait <job>
agentctl sub read <sub-id> --json
agentctl sub create codex:<thread-id> --notify codex:<caller-thread-id> --timeout 120
agentctl sub list codex:<thread-id> --json
agentctl listeners
agentctl listeners claude:<session-id> --json
```

Explicit subscriptions observe a known current turn. An idle session has no
current turn to subscribe to. Automatic subscriptions follow the turn caused by
the request; an unrelated Stop must not consume a listener. Completion means the
requested turn ended, rather than a judgment that its broader task is finished.
Callbacks do not create return subscriptions.

`listeners` includes pending subscriptions and classic `asyncwait` registrations.
Results and callback states are separate: a stored result is readable even when
callback delivery is unsupported, queued, failed, or unknown. Legacy queued inbox entries are not proof of delivery; hook-dependent receivers
are no longer supported.

## Transcript completion polling and callbacks

The daemon checks explicitly subscribed external sessions every 60 seconds,
independently of search indexing. Subscriptions bind a native active turn and
persist across daemon restarts. Completion requires a native terminal marker;
inactivity, a quiet transcript, or a tool-call boundary is not completion.
Managed jobs still notify immediately on process exit. Correlated Codex sends
and OpenCode server requests retain their existing request observers.

Supported saved markers are Codex task completion/abort events, Claude assistant
terminal stop reasons, Cursor IDE composer completed/aborted state for its current
user turn, and OpenCode local assistant completion linked to a parent user message.
Cursor IDE only persists the current composer's terminal state: if a newer turn
replaces it before a scan, the older outcome remains unknown rather than guessed.
Cursor CLI and cloud-reference formats without a verified marker cannot support
external transcript subscriptions. Their managed agentctl jobs remain watchable.

Discovery exposes `capability.completion_watch` and its mechanism. An idle or
unknown turn cannot be subscribed. Poll errors appear as `observation_error` on
the subscription; timeout expiry remains distinct from completion.

| Listener | Callback delivery without hooks |
|---|---|
| Codex | Native queue into the existing chat |
| Cursor CLI | Existing CLI session resume, subject to busy checks |
| OpenCode | Reachable server owning the session |
| Claude / Cursor IDE | Unsupported external reception; read stored results or use `agentctl wait` |

No client hooks are installed or consumed. Legacy hook inspection and selective
cleanup remain available:

```sh
agentctl setup --inspect
agentctl setup --uninstall
```

Hook installation and the `_hook` command are retired. Cleanup preserves
unrelated user hooks and settings.

## Classic job operations

```sh
agentctl asyncwait <job> --notify codex:<thread-id> --timeout 300
agentctl followup <job> "Continue"
agentctl cancel <job>
agentctl list
agentctl read <job> --stream
```

Codex UUID targets first use Desktop IPC steering for an active turn, then
fall back to CLI queue delivery for idle turns or steering errors. Transport
results retain steering warnings; a lost steering response followed by queue
delivery can duplicate a message. A queued notification arrives when the current turn ends. Register the
callback and end the turn to receive it. From a client without a usable callback
route, use `wait` or read the persisted result.

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

## Caller identity

Resolution order: explicit `--from`, `AGENTCTL_FROM`, native Claude/Cursor/OpenCode
session variables, then `CODEX_THREAD_ID`. Identity is never guessed from the
newest transcript. Children have inherited caller identities removed before
their own session identity is injected.

## Configuration and failures

| Variable | Purpose |
|---|---|
| `AGENTCTL_HOME` | Store and daemon socket; default `~/.agentctl` |
| `AGENTCTL_<HARNESS>_BIN` | Override a native executable |
| `AGENTCTL_OPENCODE_URL` | Server owning the target; default `http://127.0.0.1:4096` |
| `AGENTCTL_OPENCODE_USERNAME` | Basic-auth username; default `opencode` |
| `AGENTCTL_OPENCODE_PASSWORD` | Basic-auth password for the owning server |
| `AGENTCTL_BIN` | CLI override for the optional OpenCode plugin |
| `AGENTCTL_OPENCODE_DATA_DIR` | Local OpenCode database directory |
| `AGENTCTL_CODEX_CONTROL_SOCKET` | Existing app-server socket for read-only preflight |
| `AGENTCTL_FROM` | Explicit `harness:native-id` caller identity |
| `AGENTCTL_RECEIVE_TIMEOUT` | Claude inbox waiter lifetime; bounded to 550 seconds |

Exit codes: `0` success; `2` legacy foreground rewake; `64` invalid usage/configuration;
`65` unknown handle/session; `66` invalid or unreachable target; `67` unsupported;
`68` delivery failed; `69` daemon unavailable/incompatible; `70` child failed;
`71` cancelled; `75` missing prerequisite. JSON records describe request and
callback states separately. A delivery in flight during a crash is recovered as
unknown instead of resent.

## Design and validation

The session communication design is specified in [PRD.md](PRD.md).

```sh
python3 -m unittest discover -s tests
```

Tests use native-format fixtures, stand-in binaries, and local HTTP servers;
they do not invoke paid model prompts.
