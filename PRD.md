# agentctl session communication PRD

Session discovery, communication, and completion observation.

## Problem and outcome

Agents currently start work and wait for a response through agentctl. They also
need to find locally available user conversations, read their context, address
an existing conversation, and learn which agents are waiting for its current
turn. Sessions created outside agentctl must be discoverable.

This iteration exposes existing harness mechanisms through one explicit session
interface. It distinguishes reading a stored conversation, submitting work to
an existing client, resuming a stored conversation in another process, and
receiving a completion callback.

## Agreed scope

- Discover all locally available user sessions across Codex, Claude Code,
  Cursor, and OpenCode, including archived transcripts where available.
- Read full conversation text with pagination and access to raw native records.
- Address sessions using canonical `harness:native-id` identifiers.
- Send through supported native transports and explicitly resume known stored
  sessions. Starting a fresh session remains a separate operation.
- Automatically register one-shot completion subscriptions for requests when
  the caller is known, with opt-out and require-callback controls.
- Subscribe explicitly to a known current turn and inspect its listeners.
- Preserve classic job handles, explicit async waits, and blocking waits.
- Remove client hooks and retain selective legacy cleanup that preserves other settings.
- Poll subscribed external transcripts every 60 seconds for explicit native terminal markers.

Remote or cloud sessions without an accessible local store or configured owning
server are outside this first iteration. Rooms, membership, and broadcast
activity notifications are deferred.

## Identity and addressing

Use the native session ID, prefixed by its harness. Keep the client kind in
metadata: Claude root/subagent, Cursor CLI/IDE/cloud. Read and send capabilities
are independent; finding a transcript does not establish a live delivery route.

Caller identity comes from explicit `--from`, `AGENTCTL_FROM`, native harness
session variables, then Codex's native thread variable. Do not guess from the
newest transcript. A child process must not inherit its parent's session
identity. Learn native IDs from native lifecycle events and structured output,
then update provisional addresses and subscriptions.

A request addressed to an unknown existing session fails before launching. It
must never silently create a fresh session. Explicit managed resume against a
stored session does not imply that its original interactive client was woken.

## Request and completion lifecycle

Persist the outbound request and any one-shot subscription before dispatch or
launch. Return a durable result handle even when notification is disabled or
caller identity is unknown. Rejected requests are not retained as a dispatch
queue for later execution.

Bind automatic subscriptions to the requested turn. Codex requests use a
unique prompt marker until their native turn is known. OpenCode requests use
unique native message IDs. Managed processes use their job identity and native
structured result. A busy target's unrelated completion must not satisfy a
queued request's listener.

An explicit subscription observes a known current turn. An idle session has no
current turn to subscribe to. Unsupported completion observation is reported
rather than inferred from file inactivity or the last assistant message.

Completion means the requested turn ended. It does not assert that the broader
task was achieved. Preserve completed, interrupted, failed, cancelled, and
unknown outcomes where native evidence permits them. Intermediate tool responses
or generation limits are not definitive turn completion.

Keep result storage and callback delivery separate. A full result remains
readable after callback failure. A queued inbox entry is storage awaiting a
legacy receiver; these hook-dependent routes are now unsupported. Legacy consumption
is not a human read receipt.

Callbacks do not create automatic return subscriptions. Notification identifiers
are stable per logical delivery. Persist an in-flight claim before attempting
an external delivery; after a crash, recover ambiguous attempts as unknown
without resending. Restore observation and expiry from durable records.

## Dispatch matrix

| Target state | Action |
|---|---|
| Supported and reachable | Submit through the supported native transport and return a tracked receipt |
| Busy with native queue | Queue and follow the requested turn |
| Busy without native queue | Reject as target busy, with no dispatch |
| Supported but not configured | Reject with configuration instructions |
| Configured but unreachable | Reject with connection/target details |
| Sending unsupported | Reject and explain the client limitation |
| Acceptance uncertain | Record unknown; do not retry automatically |

`--no-notify` does not bypass busy, identity, capability, or existence checks.

## Callback matrix

| Caller state | Action |
|---|---|
| Supported, configured, reachable | Subscribe and deliver using the existing callback mechanism |
| Busy with native callback queue | Queue until the recipient can consume it |
| Busy without queue | Retain the result and report that callback delivery cannot proceed |
| Callback unsupported | Proceed with the request; report unsupported and provide wait/read access |
| Supported but not configured | Proceed; report missing setup and provide wait/read access |
| Configured but unreachable | Proceed; report unreachable and provide wait/read access |
| Delivery fails | Retain result and failed delivery evidence |
| Acceptance uncertain | Retain result and unknown state; no automatic resend |

`--require-callback` rejects before any request dispatch if the callback route
cannot be verified. Preflight cannot guarantee that a route will stay available
until completion. Combining it with `--no-notify` is invalid.

Unsupported means no implemented mechanism for that client. Not configured
means a supported mechanism lacks required setup. Unreachable means a configured
route cannot currently be contacted. Records must expose these distinctions.

## Native mechanisms and feasibility

Completion detection and callback reception are separate capabilities.
Managed jobs use immediate process-exit detection. External subscriptions bind a
native active turn and poll saved transcripts every 60 seconds. Codex terminal
turn events, Claude terminal assistant stop reasons, Cursor IDE current composer
terminal state, and OpenCode local assistant parent/completion records provide
explicit evidence. Silence and tool boundaries are insufficient. Cursor IDE's
older outcome becomes unknown if a newer turn replaces its state before a scan.
Unsupported saved formats reject external subscriptions while managed jobs remain
watchable. Codex sends and OpenCode server requests retain exact request observers.

Codex receives through its native queue; Cursor CLI through existing CLI resume;
OpenCode through the reachable owning server. Claude and Cursor IDE lack an
external callback receiver after hooks are removed: preserve stored results and
report callback reception as unsupported. Discovery and search remain available.

## Setup and observability

Native hooks are retired. Installation and hook invocation are rejected; inspect
and selective uninstall remain for legacy cleanup, preserving unrelated handlers.
Polling runs independently of search indexing and restores persisted subscriptions
on restart. Records expose observation errors and expiry distinctly from completion.

Listener inspection includes pending first-class subscriptions and classic
async waits, with complete caller/session addresses and turn binding. Result
inspection exposes dispatch state, native correlation, stored text, callback
state, errors, and expiry. A CLI/daemon protocol guard rejects stale runtimes
before they silently ignore new fields.

## Acceptance scenarios

- Find and read locally persisted sessions created independently in each harness.
- Preserve full messages, native metadata, tools through raw access, and history
  pagination; exclude obsolete Cursor branches when the native graph is known.
- Send a uniquely tracked request; an unrelated turn ending does not complete it.
- A managed child finishes immediately after launch without losing its listener.
- Busy nonqueued and unknown targets are rejected before work is launched.
- No-notify and unknown-caller requests still produce readable result handles.
- Failed, unsupported, unconfigured, unreachable, queued, and unknown callbacks
  retain full results and accurate states.
- Transcript polling correlates explicit completion markers with the subscribed
  native turn; tool boundaries and inactivity never complete it.
- Restart resumes observation and expires pending subscriptions; ambiguous
  delivery is not repeated.
- Selective legacy hook cleanup preserves unrelated handlers and remains idempotent.
- No room commands or broadcasts are introduced in this iteration.
