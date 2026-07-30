# SDD ledger — plan: docs/superpowers/plans/2026-07-29-profit-take-supervisor.md

Adaptation: this directory is NOT a git repository. No worktree, no commits,
no diffs. Reviewers read the actual files instead of a review package. The
plan already anticipates this ("Commit steps are omitted; the test run is
the verification").

Task grouping (right-sizing per the skill; the plan's Task 1 is a two-field
config change that no reviewer could accept or reject independently of the
module that reads those fields):
  - Dispatch A = plan Tasks 1 + 2  (config + strategy/profit_take.py + tests)
  - Dispatch B = plan Tasks 3 + 4  (store.closes + fleet wiring)
  - Dispatch C = plan Task 5       (strategy/supervisor.py + tests)
  - Task 6 (DEMO run) is controller work, not a subagent task.

Plan line ranges (for briefs):
  Global Constraints  11-21
  Task 1              34-68
  Task 2              70-228
  Task 3              230-295
  Task 4              297-406
  Task 5              408-601
  Task 6              603-666

## Progress

Dispatch A (plan Tasks 1+2): complete — review clean, spec ✅, quality approved.
  Files: strategy/config.py (+2 fields), strategy/profit_take.py (new),
  tests/test_profit_take.py (new, 6 tests). Suite: 86 passed.
Dispatch A: minor (deferred): no boundary-equality test (net exactly == 0.02,
  the `>=` edge). Reviewer flagged as optional; the brief did not ask for it.

Dispatch B (plan Tasks 3+4): implemented, then reviewed.
  Review found 1 Important finding: `_inventory_from_db` splits a close's
  cost back across the legs by SHARE COUNT, but the close removed cost by
  each leg's AVERAGE PRICE. Total preserved, per-leg split wrong; leaves
  dead cost on a zero-share leg, corrupting avg() on the next fill.
  Cause is plan-level: `closes` stored one combined `cost_basis`.
  HUMAN RULING (asked, since the fix deviates from plan text): FIX IT —
  add per-leg `up_cost_removed` / `dn_cost_removed` columns.
  Reviewer also noted 1 minor (deferred): if `log_close` throws after the
  `st.inv` mutation, memory and DB diverge. Accepted risk, same tradeoff
  the surrounding try/except already makes.

Dispatch B fix round 1/5: INTERRUPTED by API spend limit, PARTIALLY applied.
  Verified on disk 2026-07-29 after quota restored:
    - store.py SCHEMA: the two new columns ARE present (lines ~178-179). DONE.
    - store.py log_close: NOT updated, still inserts 10 columns. TODO.
    - store.py _MIGRATIONS: NOT updated, still only `fills`. TODO.
    - fleet.py: entirely untouched, `frac` split still at lines 86-90. TODO.
    - the covering test: not written. TODO.
  Suite still 86 passed — the partial edit is inert (columns stay NULL).

Dispatch C (plan Task 5, supervisor): INTERRUPTED by API spend limit before
  any tool call. Nothing on disk. Re-dispatched from scratch after quota
  restored.
Dispatch C: complete — review clean, spec ✅, no Critical/Important.
  Files: strategy/supervisor.py (new), tests/test_supervisor.py (new,
  4 tests). Reviewer traced the restart state machine: no path leaves a
  child permanently dead, backoff sane at 0 and 1, shutdown safe.
  The implementer's one deviation (sys.path setup in the test file) was
  verified as the project's real convention — test_gate.py and
  test_allocate.py both do it. Not a finding.
Dispatch C: minor (deferred): `crashes` resets after STABLE_SEC of uptime,
  so a child crashing slower than every 60s never escalates its backoff and
  reads identically to a one-off blip. Inherent to the plan's design, which
  only reasoned about fast crash loops. Worth revisiting if the DEMO run
  shows chronic slow crashes.

Dispatch B fix round 1/5: applied (resumed after the interruption).
  store.py log_close + _MIGRATIONS["closes"]; fleet.py captures
  up_removed/dn_removed before mutating and _inventory_from_db subtracts
  per-leg, frac logic deleted, NULL fallback for pre-change rows;
  1 new covering test in tests/test_profit_take.py.
  Suite: 91 passed (86 + 1 new + 4 supervisor).
Dispatch B fix round 1/5: 1 addressed, 0 open. Re-reviewer worked the
  skewed example through the new code by hand and confirmed the
  reconstruction lands exactly on up_cost=25.0 / down_cost=0.0; ordering
  still correct; the _MIGRATIONS entry matches the loop's shape and the
  ALTER is valid SQLite; NULL fallback is an explicit commented degrade for
  legacy rows only; the new test round-trips through the real store and the
  real _inventory_from_db rather than asserting arithmetic back at itself.
  No new breakage.
Dispatch B: complete — review clean after 1 fix round.
Dispatch B: minor (deferred): if log_close throws after the st.inv mutation,
  in-memory inventory and the DB diverge. Same accepted tradeoff the
  surrounding try/except already makes.

All three dispatches complete.

FINAL WHOLE-CHANGE REVIEW (opus): integration clean — a close leaves the
  imbalance invariant so skew and both exposure caps see identical inputs;
  fleet_naked_usd computed before visit() stays correct; markout is
  independent of inventory; no path can emit a real order. Raised three
  Important findings; human ruled FIX ALL THREE.
FINAL FIX WAVE (one dispatch, four fixes): all four ADDRESSED per scoped
  re-review (opus). Suite 91 -> 95 passed.
    F1 depth-blind close: should_close now takes bid LADDERS, sells
       min(paired, depth_up, depth_dn), walks the ladder, and tests the
       threshold on the ACHIEVED AVERAGE. Partial closes allowed; repeat
       behaviour verified strictly monotone toward zero, not a loop.
    F2 forgone_vs_settlement column recorded, so the capital-velocity
       justification is checkable rather than assumed.
    F3 dashboard closes-aware: resolution credit only for shares still
       held, proceeds counted, EARLY CLOSES (SIM) tile, close_why rendered.
    F4 log_close now precedes the st.inv mutations.

RESIDUAL findings from the re-review (no second fix wave per the skill):
Task final: parked — paper mode never consumes the book, so a stale resting
  bid can be sold into on consecutive visits; overstates cumulative closable
  liquidity. Ruling: pre-existing simulation limitation, merely reachable
  now that partial closes repeat. The live fetch self-corrects when the
  order leaves. Real, deferred.
Task final: parked — sizing is all-or-nothing at max depth, so a close is
  rejected outright when deep bad levels drag the average under threshold,
  even where a smaller top slice would clear it. Ruling: conservative in the
  safe direction — it never books fiction. Worth revisiting only if the DEMO
  shows closes being skipped often.
Task final: OPEN, surfaced to human — closes rows are internally
  inconsistent: fleet.py logs up_price/dn_price as TOP-OF-BOOK while
  proceeds is the WALKED AVERAGE, so on a partial close the price columns
  no longer reconcile with the proceeds column on the one table that claims
  to book realized money.

Task final: RESOLVED — the price/proceeds inconsistency was fixed on the
  human's ruling. should_close now returns up_avg_price / dn_avg_price (the
  achieved size-weighted averages) and fleet.py logs those instead of
  top-of-book. New test pins proceeds == shares*(up_avg+dn_avg) on a ladder
  where the walked average (0.575) genuinely differs from the top (0.60).
  Suite 96 passed.

Task 6 (DEMO run): COMPLETE, on the human's request for a clean sheet.
  - Old-code fleet/dash processes (running since earlier in the session on
    pre-change code) stopped.
  - Collected data archived, nothing deleted:
    archive/20260729-preclean/fleet.db (1.0MB) + fleet_state.json.
    run/ left holding only markets.json and the pre-registered prediction.
  - Dashboard change, also requested: the wall clock in the top-right is now
    a stopwatch, `T+ Hh MMm SSs`. Anchored to MIN(ts) of reward_samples in
    the DB via the new `_run_started()`, NOT to dashboard process start --
    the supervisor restarts the dashboard independently, and a
    module-import anchor would silently reset to zero on a dash crash and
    report a fresh run that never happened.
  - Supervisor started clean: MAKER_DB=run/fleet.db, both children up.
  - RESTART PROVEN: killed the fleet child (pid 14848); supervisor logged
    "fleet EXITED code=4294967295 after 178s (crash #1) -- restarting in 5s"
    and started pid 40796 five seconds later, then logged "fleet stable,
    clearing crash count" after 60s. This is the exact failure that went
    unnoticed for 3.5 hours on 2026-07-29.
  - `closes` table created in the fresh DB with all 14 columns including
    forgone_vs_settlement, up_cost_removed, dn_cost_removed.
  - Readings at T+270s: 17/20 scoring, $70.10/day projected, 0 closes,
    $0.00 at risk. Everything PAPER.

PLAN COMPLETE. All 6 tasks done, final review clean, 2 minors parked with
rulings (paper mode never consumes the book; sizing is all-or-nothing at
max depth).
