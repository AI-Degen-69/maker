```markdown
# maker Development Patterns

> Auto-generated skill from repository analysis

## Overview

This skill documents the development patterns, coding conventions, and workflows used in the `maker` Python codebase. The repository focuses on trading and strategy logic, with an emphasis on modular design, clear commit practices, and robust testing. It covers how to add features, fix bugs, archive legacy code, and maintain code quality through conventions and workflows.

## Coding Conventions

- **File Naming:**  
  Use `snake_case` for all Python files.  
  _Example:_  
  ```
  strategy/merge.py
  tests/test_fills.py
  ```

- **Import Style:**  
  Use relative imports within modules.  
  _Example:_  
  ```python
  from .store import Store
  from .config import DEFAULTS
  ```

- **Export Style:**  
  Use named exports; avoid wildcard imports/exports.  
  _Example:_  
  ```python
  # In strategy/quotes.py
  def get_quotes(...):
      ...
  ```

- **Commit Messages:**  
  Follow [Conventional Commits](https://www.conventionalcommits.org/) with prefixes like `feat`, `fix`, and `archive`.  
  _Example:_  
  ```
  feat: add new merge strategy for market ranking
  fix: correct quote calculation in fleet.py
  archive: move legacy bot to archive directory
  ```

## Workflows

### Feature Development with Tests and Config
**Trigger:** When adding a new trading or strategy feature, or making a major logic change  
**Command:** `/new-feature`

1. Edit or create implementation files in `strategy/` (e.g., `fleet.py`, `merge.py`, `quotes.py`, `fills.py`, `store.py`, `config.py`).
2. Update configuration in `strategy/config.py` if needed.
3. Add or update corresponding test files in `tests/` (e.g., `test_merge.py`, `test_fills.py`, `test_quotes.py`, `test_rank_markets.py`).
4. Optionally update related files in `scripts/` or `server/` if the feature affects them.

_Example:_
```python
# strategy/merge.py
def merge_strategies(a, b):
    # ...implementation...

# tests/test_merge.py
def test_merge_strategies():
    assert merge_strategies([1,2], [3]) == [1,2,3]
```

---

### Bugfix with Test Update
**Trigger:** When fixing a bug or logic error in trading/strategy code  
**Command:** `/fix-bug`

1. Edit implementation file(s) in `strategy/` to fix the bug (e.g., `fleet.py`, `store.py`, `quotes.py`).
2. Update or add relevant test(s) in `tests/` to cover the fixed behavior.
3. Optionally update related files if the bug affects other modules.

_Example:_
```python
# strategy/quotes.py
def get_quotes(...):
    # fixed logic here

# tests/test_quotes.py
def test_get_quotes_handles_edge_case():
    # test for the bugfix
```

---

### Archive Legacy Code Branch
**Trigger:** When deprecating a pipeline or major code section but keeping it for historical purposes  
**Command:** `/archive-code`

1. Move legacy files to the `archive/` directory, preserving subpaths.
2. Update `.gitignore` to ensure `archive/` code is tracked (or not ignored).
3. Update or remove tests that reference archived code.
4. Update main entry points (e.g., `strategy/main.py`) to reference new or slimmed-down logic.

_Example:_
```
archive/legacy-bot-8788/old_strategy.py
.gitignore  # ensure archive/ is handled correctly
```

## Testing Patterns

- **Framework:** Not explicitly detected; likely uses standard Python testing (e.g., `pytest` or `unittest`).
- **Test File Naming:**  
  Use `test_*.py` for Python tests.  
  _Example:_  
  ```
  tests/test_merge.py
  tests/test_fills.py
  ```
- **Test Structure:**  
  Each test covers a specific function or behavior, especially after bugfixes or feature additions.

## Commands

| Command        | Purpose                                                        |
|----------------|----------------------------------------------------------------|
| /new-feature   | Start a new feature or major logic change with tests and config|
| /fix-bug       | Fix a bug and update/add tests                                 |
| /archive-code  | Archive legacy or deprecated code                              |
```
