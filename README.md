# agentctl

Run work in Codex, Claude Code, Cursor, or OpenCode from another agent session,
receive completion notifications, and read the results. Requires Python 3.

## Install

```sh
./install.sh
```

Installs `agentctl`, `agentctl-team`, and skills for the supported harnesses it
finds. Use `./install.sh --cli` to install just the commands.

## Delegate a task

```sh
job=$(agentctl start claude "Write the migration and run the tests")
agentctl wait "$job"
agentctl read "$job"
```

To receive a completion notification in a Codex session while you keep working:

```sh
agentctl asyncwait "$job" --notify codex:<your-thread-id>
```

Children run with approvals disabled and can execute commands in the working
directory you provide. Use `--cwd /path/to/project` to select it.

## Run a team workflow

`agentctl-team` runs custom pipelines across harnesses and models: for example,
Luna implements in Codex, then Sonnet reviews in Claude Code. Each stage has its
own prompt and outcome routes; pools control concurrent workers.

For example, save this as `pipeline.json` in your repository root:

```json
{
  "stages": {
    "implement": {
      "worker": {"harness": "codex", "model": "gpt-6-luna"},
      "prompt": "Implement the task and run relevant checks.",
      "outcomes": {"ready": "review", "blocked": "hold"}
    },
    "review": {
      "worker": {"harness": "claude", "model": "sonnet"},
      "prompt": "Review the changes against the task and report your findings.",
      "outcomes": {"passed": "done", "needs_changes": "implement", "blocked": "hold"}
    }
  }
}
```

The first stage is the entry point. Defaults allow four concurrent workers and
30 minutes per attempt. Add a task, then start with your manager session ID:

```sh
agentctl-team --config pipeline.json issue add --id fix --title 'Fix the bug' --body-file task.md
agentctl-team --config pipeline.json start --notify codex:NATIVE-SESSION-ID
```

See [agentctl-team](TEAM.md) for workflow examples and
[the reference](docs/agentctl-team-reference.md#definition) to override defaults.

## Documentation

- [agentctl commands, harness setup, and configuration](docs/agentctl-reference.md)
- [agentctl-team configuration and commands](docs/agentctl-team-reference.md)
- [Coding and documentation standards](docs/coding-standards.md)

## Tests

```sh
python3 -m unittest discover -s tests
```
