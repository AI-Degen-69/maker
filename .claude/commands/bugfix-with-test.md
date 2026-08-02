---
name: bugfix-with-test
description: Workflow command scaffold for bugfix-with-test in maker.
allowed_tools: ["Bash", "Read", "Write", "Grep", "Glob"]
---

# /bugfix-with-test

Use this workflow when working on **bugfix-with-test** in `maker`.

## Goal

Fixes a bug in application logic and adds or updates a test to cover the regression.

## Common Files

- `strategy/*.py`
- `server/*.py`
- `scripts/*.py`
- `tests/test_*.py`

## Suggested Sequence

1. Understand the current state and failure mode before editing.
2. Make the smallest coherent change that satisfies the workflow goal.
3. Run the most relevant verification for touched files.
4. Summarize what changed and what still needs review.

## Typical Commit Signals

- Fix the bug in the relevant strategy/*.py, server/*.py, or scripts/*.py file.
- Add or update a test in tests/test_*.py to ensure the bug is covered.
- Optionally, update documentation or comments to clarify the fix.

## Notes

- Treat this as a scaffold, not a hard-coded script.
- Update the command if the workflow evolves materially.