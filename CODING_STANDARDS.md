# Coding standards

## Documentation and explanatory text

Write for a reader who has the repository and a task to perform. Explain current
behavior, configuration, requirements, and consequences. Apply this standard to
README files, guides, skills, comments, docstrings, CLI help, and generated
instructions.

Keep deliberation and rejected alternatives in issues, pull requests, or design
records when they explain a decision worth preserving. Product documentation
should state the resulting behavior without requiring the reader to reconstruct
the conversation that produced it.

### Describe behavior directly

Prefer a concrete description of what the software does and how the reader uses
it. Remove lists of unrelated things it does not do, rejected scope proposals,
and justifications based on the experiment or conversation that produced it.

Reject:

> The runner does not implement delivery policy, inspect commit SHAs, run checks,
> merge, or close an external tracker item.

Use:

> The runner schedules configured stages. Stage prompts and scripts define the
> work performed; pools and outcome routes control concurrency and progression.

Reject:

> There are no generic validation hooks or automatic repair policies. They are
> not necessary for the toy pipeline.

Remove this passage. Document an available configuration or recovery procedure
where the reader needs it.

Reject:

> A shadow CODEX_HOME previously caused missing-credential failures.

Use:

> Ensure the daemon's environment provides authentication for the configured
> harness.

### Explain limitations when they affect use

A negative statement belongs when it answers a plausible question raised by the
interface, prevents a concrete mistake, or defines an observable contract. State
the trigger, consequence, and action when applicable.

For example:

> Submitting a report leaves the worker running and its slots occupied until it
> terminates successfully. The stage brief must tell workers to exit after
> submission.

> Repeating a human report command after the item has left the human stage fails.

> Workers run with the user's permissions and can edit generated files.

Avoid adding hypothetical expectations just to deny them. Words such as
“explicitly,” “silently,” “currently,” and “first implementation” need a concrete
purpose; they should not substitute for explaining behavior.

### Review requirement

Before approving a change, review its explanatory text as well as its code.
Request changes for passages that:

- Record a past conversation, rejected proposal, or local incident without a
  reader-facing reason to include it.
- List absent features that the documented interface gives readers no reason to
  expect.
- Defend scope using phrases such as “not necessary for this experiment” instead
  of explaining the supported workflow.
- Assume knowledge of the author, a particular chat, or an unrelated project.

For each limitation, identify the reader's concrete expectation or mistake and
how the passage helps them act. If that reason is absent, remove the passage or
rewrite it as a direct description of behavior. These issues must be resolved
before approval; factual accuracy alone is insufficient.
