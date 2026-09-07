# Using agentctl from Claude Code

Claude has no external command that injects a message into a live session, so
agentctl does not push to Claude. Claude's own wake mechanism is used instead,
and `--notify claude:<id>` deliberately fails rather than pretending otherwise.

There are two ways to wait, both built on the same blocking command.

## Option A: background the wait in the current turn

```bash
a=$(agentctl start codex "Audit the auth module" --cwd /path/to/repo)
agentctl wait "$a"
```

Run `agentctl wait` as a background shell task. It blocks until the job is
terminal, then prints the completion envelope, and the harness delivers that
output back to you when the task finishes. Its exit code says what happened:
`0` succeeded, `70` failed, `71` cancelled.

To wait on several children at once:

```bash
agentctl wait "$a" "$b"
```

## Option B: an asyncRewake Stop hook

Verified against Claude Code 2.1.153: a hook marked `asyncRewake` runs in the
background, and when it exits with code `2` Claude is woken and receives the
hook's **stderr** as a system reminder.

`agentctl wait <job> --rewake` implements exactly that: it blocks until the job
is terminal, writes the envelope to stderr, and exits `2`.

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "agentctl wait job_9f2c11a7e4d0 --rewake",
            "asyncRewake": true,
            "rewakeMessage": "A delegated agent finished:",
            "rewakeSummary": "agentctl completion"
          }
        ]
      }
    ]
  }
}
```

Use this when you want to stop your turn and be woken later, rather than
holding a background task open inside the current turn.

## Claude as the child

Claude children run in print mode only:
`claude -p --output-format stream-json --dangerously-skip-permissions`.
Interactive and background-agent modes are not supported, because they persist
and expose sessions differently.

agentctl chooses the child's session id up front with `--session-id`, so the
child handle is known the moment the job starts and `agentctl followup` resumes
exactly that session.
