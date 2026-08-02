---
name: feature-development-with-tests-and-docs
description: Workflow command scaffold for feature-development-with-tests-and-docs in maker.
allowed_tools: ["Bash", "Read", "Write", "Grep", "Glob"]
---

# /feature-development-with-tests-and-docs

Use this workflow when working on **feature-development-with-tests-and-docs** in `maker`.

## Goal

Implements a new feature or major refactor, with associated tests and documentation updates.

## Common Files

- `strategy/*.py`
- `server/*.py`
- `scripts/*.py`
- `tests/test_*.py`
- `docs/plans/*.md`
- `research/*.md`

## Suggested Sequence

1. Understand the current state and failure mode before editing.
2. Make the smallest coherent change that satisfies the workflow goal.
3. Run the most relevant verification for touched files.
4. Summarize what changed and what still needs review.

## Typical Commit Signals

- Implement or refactor logic in one or more strategy/*.py, server/*.py, or scripts/*.py files.
- Update or add tests in tests/test_*.py to cover new/changed logic.
- Update or add documentation in docs/plans/*.md or research/*.md as needed.

## Notes

- Treat this as a scaffold, not a hard-coded script.
- Update the command if the workflow evolves materially.