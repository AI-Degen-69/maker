```markdown
# maker Development Patterns

> Auto-generated skill from repository analysis

## Overview

This skill teaches you how to contribute effectively to the `maker` Python codebase, which is organized for clarity, maintainability, and robust development workflows. You'll learn the project's coding conventions, commit patterns, and step-by-step processes for feature development, bugfixing, script improvements, file management, and archiving legacy code. The repository emphasizes conventional commits, modular Python code, and disciplined workflow automation.

## Coding Conventions

- **File Naming:**  
  Use `snake_case` for Python files and scripts.  
  _Example:_  
  ```
  strategy/main_logic.py
  server/api_handler.py
  scripts/fleet_bg.ps1
  ```

- **Import Style:**  
  Use **relative imports** within modules.  
  _Example:_  
  ```python
  from .utils import parse_config
  from ..strategy.base import StrategyBase
  ```

- **Export Style:**  
  Use **named exports**; avoid wildcard imports/exports.  
  _Example:_  
  ```python
  # In strategy/main_logic.py
  def run_strategy(...):
      ...

  __all__ = ["run_strategy"]
  ```

- **Commit Messages:**  
  Follow **conventional commit** patterns:  
  - Prefixes: `feat`, `fix`, `chore`, `archive`
  - Example:  
    ```
    feat: add portfolio rebalancing strategy
    fix: handle edge case in order matching logic
    chore: update dependencies and cleanup scripts
    archive: move legacy bot to archive directory
    ```

## Workflows

### Feature Development with Tests and Docs
**Trigger:** When adding a new capability, refactoring core logic, or changing a major workflow  
**Command:** `/feature`

1. Implement or refactor logic in one or more of:
    - `strategy/*.py`
    - `server/*.py`
    - `scripts/*.py`
2. Update or add tests in `tests/test_*.py` to cover new or changed logic.
3. Update or add documentation in `docs/plans/*.md` or `research/*.md` as needed.
4. Commit with a descriptive `feat:` or `refactor:` message.

_Example:_
```python
# strategy/portfolio.py
def rebalance_portfolio(...):
    ...
```
```python
# tests/test_portfolio.py
def test_rebalance_portfolio():
    ...
```
```markdown
# docs/plans/portfolio.md
## Portfolio Rebalancing Plan
...
```

---

### Bugfix with Test
**Trigger:** When a bug is found in core logic or scripts  
**Command:** `/fix-bug`

1. Fix the bug in the relevant `strategy/*.py`, `server/*.py`, or `scripts/*.py` file.
2. Add or update a test in `tests/test_*.py` to ensure the bug is covered.
3. Optionally, update documentation or code comments to clarify the fix.
4. Commit with a `fix:` message.

_Example:_
```python
# strategy/order_matching.py
def match_orders(...):
    # Fixed off-by-one error
    ...
```
```python
# tests/test_order_matching.py
def test_match_orders_edge_case():
    ...
```

---

### Script Improvement or Safety Hardening
**Trigger:** When a script is unsafe, unreliable, or needs new features for process control  
**Command:** `/script-fix`

1. Update `scripts/fleet-*.ps1` or related process scripts.
2. Add or update comments explaining the change and rationale.
3. Test the script manually or via dry-run.
4. Optionally, update `.gitignore` or related config if output files/processes change.
5. Commit with a `chore:` or `fix:` message.

_Example:_
```powershell
# scripts/fleet-bg.ps1
# Improved error handling for process start
...
```

---

### Untrack Runtime or Generated Files
**Trigger:** When runtime or output files are accidentally committed or need to be excluded from version control  
**Command:** `/untrack`

1. Remove the file(s) from git tracking using `git rm --cached`.
2. Add the relevant file patterns to `.gitignore`.
3. Commit the changes with a `chore:` message.

_Example:_
```bash
git rm --cached run/output.log
echo "run/*" >> .gitignore
git commit -m "chore: untrack runtime output files"
```

---

### Archive or Refactor Legacy Code
**Trigger:** When a major refactor or new pipeline supersedes an old one  
**Command:** `/archive`

1. Move legacy files to `archive/` or similar directory, preserving structure.
2. Update `.gitignore` to include or exclude `archive` as needed.
3. Update or remove tests and documentation referencing the legacy code.
4. Commit with an `archive:` message documenting the rationale.

_Example:_
```bash
mkdir -p archive/legacy-bot-v1
git mv strategy/main.py archive/legacy-bot-v1/
echo "archive/" >> .gitignore
git commit -m "archive: move legacy bot to archive/legacy-bot-v1"
```

## Testing Patterns

- **Test Framework:** Unknown (Python standard, e.g., `unittest` or `pytest`, is likely)
- **Test File Naming:**  
  All test files are named as `tests/test_*.py`
- **Test Coverage:**  
  Each new feature or bugfix should be accompanied by a corresponding test or test update.
- **Example Test:**
  ```python
  # tests/test_utils.py
  def test_parse_config_valid():
      config = parse_config("...")
      assert config["key"] == "expected_value"
  ```

## Commands

| Command     | Purpose                                                        |
|-------------|----------------------------------------------------------------|
| /feature    | Start a new feature, refactor, or major workflow change        |
| /fix-bug    | Fix a bug and add or update regression tests                   |
| /script-fix | Improve or harden PowerShell/process management scripts        |
| /untrack    | Remove runtime or generated files from git tracking            |
| /archive    | Archive or refactor legacy code and update references          |
```
