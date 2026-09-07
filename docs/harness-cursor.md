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

## The busy-session limitation

Cursor has no durable message queue. Codex can accept a queued message while a
turn is running and deliver it when the thread goes idle; Cursor cannot.

So if your chat already has a turn in flight when the child finishes:

- the delivery fails, and says the chat was busy
- agentctl does not queue behind it, and does not retry, because a retry could
  duplicate the follow-up in your context
- the child's result is unaffected and still readable with
  `agentctl read <job>`

Check for that case with `agentctl status <job>`, which shows every wait and its
delivery state.

## Cursor as the child

agentctl allocates the child's chat id first with `cursor-agent create-chat`,
then runs `cursor-agent -p --output-format stream-json --resume <id> --force
--trust`. The chat id is therefore stable from the moment the job starts, and
`agentctl followup` resumes exactly that chat.
