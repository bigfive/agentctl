# Using agentctl from Codex

Codex is the only harness here with a durable message queue, so it gets the
cleanest path: register the wait and carry on working. The completion arrives
as an ordinary follow-up turn once your current turn ends.

## 1. Find your own thread id

You are not told your thread id, but you can look it up. Your thread is the
newest session recorded under `~/.codex/sessions/<year>/<month>/<day>/`, named
`rollout-<timestamp>-<thread-id>.jsonl`:

```bash
ls -t ~/.codex/sessions/*/*/*/rollout-*.jsonl | head -1
```

The thread id is the UUID at the end of that filename. Confirm it is yours by
checking that the file's last lines contain the message you are answering.

This is a Codex thread id. It is not the agentctl job handle, and it is not the
child's id.

## 2. Start the child and capture its handle

```bash
a=$(agentctl start claude "Write the migration and run the tests" --cwd /path/to/repo)
```

`start` prints only the job handle, so `$a` holds it exactly.

## 3. Register the wait

```bash
agentctl asyncwait "$a" --notify codex:01a07a1e-c33d-70b3-b0a0-d49304f192b3
```

This returns immediately. The wait now belongs to the agentctl daemon, so it
survives your shell, your turn, and the daemon itself restarting.

## 4. What you will receive

When the child finishes, agentctl runs `codex queue --thread <your-thread>` and
you get a follow-up turn containing:

```text
[agentctl completion]
job: job_9f2c11a7e4d0
child: claude:6b1f...
harness: claude
status: succeeded
result: Migration written; 24 tests pass.
full: agentctl read job_9f2c11a7e4d0
```

Read the whole result with `agentctl read <job>`, the raw event stream with
`agentctl read <job> --stream`.

## Notes

- The thread must be visible to the local Codex app-server daemon. This works
  for a task open in Codex Desktop.
- The message is queued, not steered, so your current turn is never
  interrupted.
- Delivery happens once. A repeated terminal event or a daemon restart will not
  produce a second follow-up.
- `codex` may not be on your `PATH`; on macOS it ships inside
  `/Applications/ChatGPT.app/Contents/Resources/codex`. agentctl looks there
  itself, and `AGENTCTL_CODEX_BIN` overrides the lookup. The bundled copy is a
  separate build that can drift in version from one you install yourself, so
  `PATH` wins whenever a `codex` is on it.
