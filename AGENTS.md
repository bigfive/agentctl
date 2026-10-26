# AGENTS.md

## Required standards

Read and follow [docs/coding-standards.md](docs/coding-standards.md) when writing or
reviewing changes. Its documentation review requirements are approval criteria;
request changes for violations before approving.

## Tests

Tautological tests are considered harmful. Do not add a test that cannot fail,
that only restates the implementation, that asserts a constant, or that mocks
the behavior it is supposed to verify. Every test must encode an independent
expectation and fail if the requirement it covers regresses.

- Assert observable outcomes: state transitions, files, exit codes, emitted
  records, and externally visible behavior.
- Do not assert literal prompt, help, or log strings unless the string itself is
  the contract. Test the property instead. For example, assert that the worker
  prompt does not contain the root checkout path, not that it contains an exact
  sentence.
- Do not use the code under test, or a fixture that derives its expectation the
  same way as the implementation, as the only oracle.
- Remove tautological tests when you find them. Do not keep them for coverage.
