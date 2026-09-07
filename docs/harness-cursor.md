# Using agentctl from Cursor

Cursor can receive a completion, but it is the weakest of the three push paths,
and the reason is worth understanding before you rely on it.

## 1. Find your own chat id

Cursor stores chats under `~/.cursor/chats/<workspace-hash>/<chat-id>/`:

```bash
ls -td ~/.cursor/chats/*/*/ | head -1
```

The last path segment is the chat id. Two kinds exist and they are not
interchangeable:

- a bare UUID, e.g. `5a4f300a-61b1-4975-82e2-aa4be275aa49`, is a local chat
- an id starting with `bc-` is a Cursor cloud agent

agentctl delivers to local chats only. Passing a `bc-` id is an error, not a
silent local attempt.

## 2. Start the child and capture its handle

```bash
a=$(agentctl start claude "Port the component to the new API" --cwd /path/to/repo)
```

## 3. Register the wait

```bash
agentctl asyncwait "$a" --notify cursor:5a4f300a-61b1-4975-82e2-aa4be275aa49
```

## 4. What you will receive

agentctl runs `cursor-agent -p --resume <your-chat-id>` with the completion
envelope, which appends a new turn to that chat.

## What happens if your chat is already busy

Codex can queue a message while a turn is running and deliver it when the
thread goes idle. Cursor has no such queue, so it is worth knowing what it
actually does instead.

Measured on cursor-cli 2026.07.23: delivering into a chat that already had a
turn in flight was **accepted**. Both turns ran and both landed in the same
chat. Delivery was not refused, and the chat was not corrupted.

So the risk is not a rejected completion. It is that two clients wrote to one
chat at once, and the order the turns appear in is simply the order the two
runs finished. If you are mid-turn when a child completes, expect the
completion to arrive beside your own work rather than after it.

If Cursor ever does report the chat as busy, agentctl fails loudly and does not
retry, because a retry could duplicate a follow-up that in fact arrived. The
child's result is unaffected and stays readable with `agentctl read <job>`.

One thing that has **not** been tested: whether a completion delivered this way
appears in a Cursor GUI session you are actively watching. It was verified
against chats driven from the CLI. Do not assume the GUI picks it up live.

Check any delivery with `agentctl status <job>`, which shows every wait and its
delivery state.

## Which cursor binary is used

Cursor's CLI answers to two command names. Its own installer creates both
`cursor-agent` and `agent`; the Homebrew cask links only `cursor-agent`.
agentctl tries `cursor-agent` first and falls back to `agent`, preferring the
specific name because `agent` is generic enough to belong to something else.
`AGENTCTL_CURSOR_BIN` overrides the lookup.

Having two installs at once is easy to do, and `PATH` order decides which one
wins, not recency. Check with `cursor-agent --version` and `agent --version`.

## Cursor as the child

agentctl allocates the child's chat id first with `cursor-agent create-chat`,
then runs `cursor-agent -p --output-format stream-json --resume <id> --force
--trust`. The chat id is therefore stable from the moment the job starts, and
`agentctl followup` resumes exactly that chat.
