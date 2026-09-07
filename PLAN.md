# agentctl

## Goal

Provide one small CLI for agents to start work in another agent harness, wait for completion, inspect results, send follow-ups, check status, and cancel work.

The first version will implement one happy path: a background wait is registered with a local daemon, and completion is delivered back into the parent agent session as a follow-up message. If a required harness operation fails, it fails loudly.

## Core user experience

An agent starts a child session using the harness-native command, captures the resulting handle, and then runs:

```bash
agentctl asyncwait "$a" --notify codex:<thread-id>
agentctl asyncwait "$a" --notify cursor:<agent-id>
agentctl asyncwait "$a" --notify opencode:<session-id>
```

`asyncwait` registers a durable wait and returns immediately. The parent harness can continue working. When the child reaches a terminal state, `agentctl` sends a structured completion or failure message to the notification target.

## Scope of the first implementation

### Included

- A local `agentctl` daemon with a durable job/wait store.
- Agent handles that identify a child session and its harness.
- `asyncwait` registration and completion delivery.
- Status, transcript/result reading, follow-up, and cancellation primitives.
- Codex, OpenCode, and Cursor notification adapters.
- Instructions for each harness explaining how the agent discovers its own current session ID before passing `--notify`.
- Loud errors for unknown handles, invalid target IDs, unsupported operations, and failed delivery.

### Deliberately excluded

- Automatic fallback between transports or CLIs.
- Permission and approval orchestration.
- A universal transcript format beyond the fields needed for status and result delivery.
- Tmux or terminal-pane injection as a primary transport.
- Silent retries that could duplicate a prompt or completion message.

## Architecture

```text
parent agent
  |
  | starts child and runs agentctl asyncwait
  v
agentctl CLI  --->  local daemon  --->  child adapter
                         |
                         | terminal event
                         v
                    notify adapter
                         |
                         v
                 parent harness session
```

The CLI hands the wait to the daemon before returning. The daemon must not depend on the parent agent's background shell process remaining alive.

Each child job stores:

- harness (`codex`, `claude`, `cursor`, or `opencode`)
- native session/agent ID
- launch command and working directory
- current status
- start and finish timestamps
- result/transcript location
- notification target
- delivery state and stable notification ID

## Notification adapters

### Codex

Use the native queued follow-up path:

```bash
codex queue --thread <thread-id> --message '<completion message>'
```

This should queue the message behind an active turn and let the Codex app-server deliver it when the parent becomes idle.

### OpenCode

Assume an OpenCode server is already running. Deliver to the target session through its asynchronous prompt endpoint:

```text
POST /session/<session-id>/prompt_async
```

The parent agent can discover its own `ses_...` ID from OpenCode's session logs and include it in the `--notify` target. The plan does not require an OpenCode plugin for the initial path.

### Cursor

Use Cursor's persisted agent/session interface to resume the target and send the completion message. The agent can discover its local conversation/agent ID from Cursor's accessible session records.

The adapter must make the distinction between a local conversation ID and a cloud `bc-...` agent ID explicit. It must also report the busy-session condition clearly because Cursor does not provide the same durable queue primitive as Codex.

### Claude

Use Claude's native background wait/wake behavior for the parent harness. `agentctl` remains responsible for tracking the child and exposing its result/status, while Claude-specific wake delivery is handled through the native integration.

## Harness instructions

Each supported harness gets a short instruction or skill covering:

1. How to identify the current parent session.
2. How to launch a child and capture its handle.
3. How to invoke `agentctl asyncwait` with the resolved notification target.
4. What completion message format the parent will receive.

The model does not need the session ID injected as a special hidden variable. It may discover the ID through the harness's normal tools and session records, then include the concrete ID in the command.

## Completion message

Use a compact, machine-readable envelope inside a human-readable follow-up:

```text
[agentctl completion]
job: <job-id>
child: <child-handle>
harness: <harness>
status: succeeded | failed | cancelled
result: <short result or pointer to the full result>
```

The parent should be told where to read the full transcript/result rather than receiving an unbounded transcript automatically.

## CLI shape

Initial commands:

```text
agentctl start <harness> <prompt> [options]
agentctl asyncwait <job> --notify <harness>:<session-id>
agentctl status <job>
agentctl read <job>
agentctl followup <job> <prompt>
agentctl cancel <job>
agentctl daemon status
```

`start` should print a stable job handle suitable for shell capture. The native harness session ID and the `agentctl` job handle are separate identifiers.

## Failure semantics

Failures are visible and terminal:

- child launch failure: job is `failed`
- child harness reports failure: job is `failed`
- child cancellation: job is `cancelled`
- invalid notification target: delivery fails with an explicit error
- notification transport failure: job result remains readable and delivery is marked failed

Every delivery has a stable ID so a daemon restart or repeated completion event cannot silently create duplicate follow-ups.

## Implementation order

1. Create the local daemon, job store, handles, and `status/read/cancel` commands.
2. Implement one child adapter and a deterministic terminal-state watcher.
3. Implement Codex notification using `codex queue`; verify delivery into a live Codex Desktop task.
4. Add the generic `--notify <harness>:<id>` target parser and delivery records.
5. Add OpenCode notification through `prompt_async`.
6. Add Cursor resume/send integration and document its busy-session behavior.
7. Add Claude's native parent wake integration.
8. Package the harness-specific self-ID instructions and run an end-to-end test from each harness.

## First end-to-end acceptance test

From a live Codex session:

```bash
a="<child-job-handle>"
agentctl asyncwait "$a" --notify codex:<current-thread-id>
```

The child completes, the daemon records the result, and the same parent session receives exactly one follow-up containing the terminal status and a pointer to the full result.
