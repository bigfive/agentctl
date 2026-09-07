# Using agentctl from OpenCode

OpenCode does not put your session id in your prompt, but you can find it, and
a running OpenCode server can accept a new prompt into that session from
outside. That is enough for the asynchronous path without any plugin.

## 1. Find your own session id

Your session id is in the OpenCode log:

```bash
grep -o 'ses_[A-Za-z0-9]*' ~/.local/share/opencode/log/opencode.log | tail -1
```

Confirm it is yours before using it. With several OpenCode sessions running,
the newest id in the log is not necessarily yours; correlate it with your own
`OPENCODE_PID` or with the session's directory:

```bash
curl -s "$OPENCODE_URL/session/<candidate>" | grep -o '"directory":"[^"]*"'
```

## 2. Start the child and capture its handle

```bash
a=$(agentctl start codex "Reproduce the flaky test" --cwd /path/to/repo)
```

## 3. Register the wait

```bash
agentctl asyncwait "$a" --notify opencode:ses_f85ae7073ffe3i7aOYmTzCVnG1
```

This returns immediately.

## 4. What you will receive

agentctl posts the completion envelope to
`POST /session/<your-session>/prompt_async` on the OpenCode server. It arrives
as a new prompt in your session.

## Prerequisites

- An OpenCode server must already be running. agentctl never starts one.
- agentctl finds it at `http://127.0.0.1:4096` unless `AGENTCTL_OPENCODE_URL`,
  `OPENCODE_SERVER`, or `OPENCODE_BASE_URL` says otherwise. If the server is
  unreachable, the delivery fails loudly and the result stays readable with
  `agentctl read <job>`.
- The delivery carries a `messageID` derived from the notification id, so the
  same completion cannot be appended to your session twice.

## OpenCode as the child

OpenCode children are created on that same server (`POST /session`) and then
driven with `opencode run --attach <url> --session <id> --format json --auto`.
`--auto` approves every permission that is not explicitly denied, because a
permission prompt would hang a child nobody is watching.
