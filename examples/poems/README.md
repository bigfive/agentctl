# Poem pipeline

`create.py NEW_DIRECTORY` creates a disposable Git repository and a configurable
write → review → integration pipeline. See [TEAM.md](../../TEAM.md) for commands.

Two items can run concurrently. Each retains one branch/worktree throughout.
Writing and reviewing deliberately leave uncommitted files. The integration
stage's configured instructions commit and merge into the toy repository's
main branch, with one integration worker at a time.

The example uses real Codex workers through the configured agentctl executable.
It never edits agentctl configuration or restarts a shared daemon. The existing
daemon must have the expected Codex environment and authentication. If the daemon
was started from a different environment, use an explicitly isolated
`AGENTCTL_HOME` for this test, or correct that daemon's environment separately.
Do not copy credentials into the prepared stage files.
