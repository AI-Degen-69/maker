## Fix round 1

### Finding
Reviewer found: `closes` stored only a combined `cost_basis`, so
`_inventory_from_db` had to guess the per-leg split on restart, and it
guessed by share-count ratio (`frac = up_shares/(up_shares+down_shares)`)
when the close actually removed cost by each leg's own average price
(`n * avg("UP")`, `n * avg("DOWN")`). Worked example (150 UP @ 0.50,
100 DOWN @ 0.40, close 100 pairs): live ends at up_cost=25, down_cost=0;
the old share-count split reconstructed up_cost=21, down_cost=4 — total
right, per-leg wrong, and a nonzero cost sitting on a zero-share leg
corrupts `avg("DOWN")` on the next DOWN fill.

Authorized deviation from the plan text: add `up_cost_removed` /
`dn_cost_removed` columns to `closes` and use them directly instead of
splitting `cost_basis` after the fact.

### Changes

**`strategy/store.py`**
- `closes` table in `SCHEMA`: added `up_cost_removed REAL` and
  `dn_cost_removed REAL` columns with the explanatory comment (already in
  place before this round per coordinator's note — verified, unchanged).
- `log_close`: INSERT column list and `VALUES` placeholders extended to 12
  columns; parameter tuple now includes `kw["up_cost_removed"],
  kw["dn_cost_removed"]`.
- `_MIGRATIONS`: added a `"closes": {"up_cost_removed": "REAL",
  "dn_cost_removed": "REAL"}` entry, same shape as the existing `"fills"`
  entry, so a `run/fleet.db` created before this change gets the two
  columns added via `ALTER TABLE` instead of failing the INSERT.

**`strategy/fleet.py`**
- Close block in `visit()`: now captures `up_removed = n *
  st.inv.avg("UP")` and `dn_removed = n * st.inv.avg("DOWN")` BEFORE any
  mutation (the cost-before-shares ordering trap still applies — capture
  happens while `up_shares`/`down_shares` are still their pre-close
  values), uses those two variables when decrementing `up_cost`/
  `down_cost`, and passes `up_cost_removed=up_removed,
  dn_cost_removed=dn_removed` to `store.log_close`.
- `_inventory_from_db`: the `frac` share-count-split logic is deleted
  entirely. The closes query now selects `up_cost_removed,
  dn_cost_removed` alongside `shares`/`cost_basis` and subtracts them
  directly from `up_cost`/`down_cost`. For rows written before this
  change (both columns NULL), falls back to an even 50/50 split of the
  old `cost_basis` with a comment explaining why the fallback exists,
  rather than crashing.

### Covering test
Added one new test function to `tests/test_profit_take.py`,
`test_close_reconstruction_uses_per_leg_removed_cost_not_share_split`
(the six existing tests untouched). It exercises the real store rather
than testing the arithmetic in isolation: uses `monkeypatch.setenv` to
point `MAKER_DB` at a `tmp_path` DB, seeds the `fills` table with the
skewed position (150 UP @ 0.50, 100 DOWN @ 0.40), mimics the fleet close
block's exact mutation order to close 100 pairs, asserts the live values
(`up_cost=25.0`, `down_cost=0.0`, `up_shares=50.0`, `down_shares=0.0`),
calls the real `store.log_close(...)` with the per-leg removed costs, then
calls the real `strategy.fleet._inventory_from_db(cid)` and asserts the
reconstruction matches the live values exactly — including
`down_cost == 0.0` on a zero-share leg, the specific case the old split
got wrong. Driving the real store/rehydration function was not awkward
(both worked directly against the temp DB via `MAKER_DB`), so no
arithmetic-only fallback was needed.

### Commands and verbatim output

`python -m pytest tests/test_profit_take.py -v`:
```
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.1.1, pluggy-1.6.0 -- C:\Users\Tiger\AppData\Local\Programs\Python\Python312\python.exe
cachedir: .pytest_cache
rootdir: C:\Users\Tiger\Agents\Projects\AI Trading\maker
plugins: anyio-4.14.2
collecting ... collected 7 items

tests/test_profit_take.py::test_no_paired_shares_never_closes PASSED     [ 14%]
tests/test_profit_take.py::test_missing_bid_never_closes PASSED          [ 28%]
tests/test_profit_take.py::test_move_that_only_covers_the_fees_does_not_close PASSED [ 42%]
tests/test_profit_take.py::test_move_past_the_threshold_closes PASSED    [ 57%]
tests/test_profit_take.py::test_realized_pnl_is_proceeds_minus_cost_minus_fee PASSED [ 71%]
tests/test_profit_take.py::test_only_the_paired_portion_is_closed PASSED [ 85%]
tests/test_profit_take.py::test_close_reconstruction_uses_per_leg_removed_cost_not_share_split PASSED [100%]

============================== 7 passed in 0.76s ==============================
```

`python -m pytest tests/ -q`:
```
........................................................................ [ 79%]
...................                                                      [100%]
91 passed in 1.75s
```

91 passed, not 87. Confirmed this is not a regression: `tests/test_supervisor.py`
now exists (10 test items visible via `ls tests/`), landed by the concurrent
dispatch building `strategy/supervisor.py`, which was not touched here. 91 =
86 (pre-existing) + 1 (this round's new test) + 4 (supervisor tests from the
concurrent agent). No files under `strategy/supervisor.py` or
`tests/test_supervisor.py` were read or modified by this fix.

### Concerns
None. The fix is additive (new columns, migration entry, no removed
functionality except the buggy `frac` split which is exactly what needed
removing), old rows without the new columns degrade gracefully via the
NULL-check fallback, and the new test drives the real store and the real
rehydration function rather than re-implementing the logic under test.

---

# Task 3 & 4 report — Persist closes, wire into fleet

## Scope
Task 3 (Steps 1-3) and Task 4 (Steps 1-5) of
`docs/superpowers/plans/2026-07-29-profit-take-supervisor.md`, verbatim.
Did not touch `strategy/profit_take.py`, `strategy/config.py`,
`tests/test_profit_take.py`, and did not create `strategy/supervisor.py` or
run the DEMO.

## Files changed

### `strategy/store.py`
- After the `markouts` table block (originally ending at line 155-156, the
  closing `CREATE INDEX ... idx_mk_done` line followed by the closing `"""`
  of `SCHEMA`): appended the `closes` table DDL and its index
  (`idx_cl_ts`), verbatim from Task 3 Step 1.
- After `log_markout_open` (originally ending ~line 227) and before
  `pending_markouts` (originally line 230): added `log_close(**kw) -> None`,
  verbatim from Task 3 Step 2.

### `strategy/fleet.py`
- Line 27: changed
  `from strategy import gate, markout, rewards, store`
  to
  `from strategy import gate, markout, profit_take, rewards, store`
  (Task 4 Step 1).
- In `visit()`, immediately after
  `cfg = replace(cfg, gate_state=st.gate, fleet_naked_usd=fleet_naked_usd)`
  and before the `# Requote.` comment: inserted the profit-take try/except
  block that calls `profit_take.should_close`, mutates `st.inv` (cost before
  shares, as required), calls `store.log_close(...)`, logs `CLOSE ...` on
  take, and falls back to `pt = {"take": False, "why": f"error: {e}"}` on
  exception (Task 4 Step 2). Landed right before the `# Requote.` block that
  begins around the former line 267.
- In the `st.spec["_live"] = {...}` dict, immediately after
  `"markout_n": st.markout.get("n", 0),`: added
  `"close_why": pt.get("why", ""),` (Task 4 Step 3).
- In `_inventory_from_db`, inside the existing `with store.db() as c:` block,
  immediately after the `for side, size, price in c.execute(...)` fills loop
  and still inside the `with`/`try`: added the `closes`-aware loop that
  backs out `up_shares`/`down_shares`/`up_cost`/`down_cost` per the brief's
  proportional-split logic, verbatim (Task 4 Step 4). It sits inside the
  same `try/except Exception` that already wrapped the fills query, so a DB
  without the `closes` table still degrades to old behaviour.

Confirmed `m.condition_id`, `m.market_slug`, and `st.title` are already in
scope inside `visit()` (used a few lines above/below the inserted block), so
no additional plumbing was needed.

## Verification

### Task 3 Step 3 — fresh-DB check (PowerShell env-var form used per brief)

Command run (bash tool, `$env:` form not needed there since Bash tool is
Git Bash on this box — plain `MAKER_DB=run/t.db python -c "..."` worked):

```
rm -f run/t.db
MAKER_DB=run/t.db python -c "
from strategy import store
store.log_close(condition_id='x', market_slug='s', shares=10, up_price=0.56,
                dn_price=0.45, cost_basis=9.5, proceeds=10.1, fee=0.34,
                realized_pnl=0.26)
with store.db() as c:
    print(list(c.execute('SELECT shares, realized_pnl FROM closes')))
"
```

Output (verbatim):
```
[(10.0, 0.26)]
```

Matches the expected `[(10.0, 0.26)]` exactly. `run/t.db` was then deleted
(`rm -f run/t.db`), confirmed gone via `ls run/` afterward (only
`fleet.db`, `fleet_state.json`, `live_test_0x14d32732.json`, `markets.json`
remain).

### Task 4 Step 5 — full suite

Command: `python -m pytest tests/ -q`

Output (verbatim):
```
........................................................................ [ 83%]
..............                                                           [100%]
86 passed in 1.01s
```

86 passed, matching the brief's "80 pre-existing plus 6 new" expectation
(brief also referenced 86 in the dispatch instructions as the starting
count before this dispatch — that count already included the 6 new
`test_profit_take.py` tests from the earlier dispatch, since
`profit_take.py` was already landed and tested; this run shows no
regressions and no new failures).

## Ambiguities and how they were resolved
- The brief's PowerShell note (`$env:MAKER_DB='run/t.db'`) applies to
  PowerShell; I ran the verification through the Bash tool (Git Bash),
  where the `VAR=val cmd` prefix form works natively, so I used that form
  instead — same effect, correct verbatim SQL/Python from the brief.
- No other ambiguity: all inserted code is verbatim from the plan, and the
  insertion points (import line, post-gate/pre-requote block, `_live` dict
  key ordering, `_inventory_from_db` loop placement) matched the plan's
  descriptions exactly against the current file contents.

## Concerns
None. Schema change is additive (new table only), the close block is
try/except-wrapped so a `profit_take` bug cannot stop the fleet loop, the
cost-before-shares ordering was followed exactly as specified, and the
rehydration fix (Task 4 Step 4) prevents closed positions from being
resurrected on restart. Test suite is green with no changes to previously
passing tests.
