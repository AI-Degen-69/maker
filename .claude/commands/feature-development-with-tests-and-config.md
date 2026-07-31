---
name: feature-development-with-tests-and-config
description: Workflow command scaffold for feature-development-with-tests-and-config in maker.
allowed_tools: ["Bash", "Read", "Write", "Grep", "Glob"]
---

# /feature-development-with-tests-and-config

Use this workflow when working on **feature-development-with-tests-and-config** in `maker`.

## Goal

Implements a new feature or major logic change, updates configuration, and adds or updates corresponding tests.

## Common Files

- `strategy/fleet.py`
- `strategy/merge.py`
- `strategy/quotes.py`
- `strategy/fills.py`
- `strategy/store.py`
- `strategy/config.py`

## Suggested Sequence

1. Understand the current state and failure mode before editing.
2. Make the smallest coherent change that satisfies the workflow goal.
3. Run the most relevant verification for touched files.
4. Summarize what changed and what still needs review.

## Typical Commit Signals

- Edit or create implementation files in strategy/ (e.g., fleet.py, merge.py, quotes.py, fills.py, store.py, config.py)
- Update configuration in strategy/config.py if needed
- Add or update corresponding test files in tests/ (e.g., test_merge.py, test_fills.py, test_quotes.py, test_rank_markets.py)
- Optionally update related scripts/ or server/ files if feature affects them

## Notes

- Treat this as a scaffold, not a hard-coded script.
- Update the command if the workflow evolves materially.