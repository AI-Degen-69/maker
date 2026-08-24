# Plan 01 — Pair-Cost Cap Enforcement (Paper-Trading Realism)

**Branch:** `harden-to-reality` · **Baseline:** 703 passed (green)
**Source of truth:** `strategy/config.py:615` (`max_pair_cost=0.995`), `strategy/quotes.py:633`, `strategy/sweep.py:763`, `strategy/risk.py:363`
**Objective:** Make the sim never build a pair costing ≥ $1.00 — the one guaranteed-loss configuration.

---

## CEO review (why this matters)
The live run (`run/fleet.db`) showed **18 of 23 pairs ≥ $1.00**; median pair cost = **$0.99**, only 78% under $1.00. That is the exact "guaranteed loss on the hedged portion" the research log (22/07) flagged as OPEN. BUT the code on `main` already enforces the cap at three sites:
- `quotes.py:633` — resting quote rejected if `(price + other_avg) >= max_pair_cost`
- `sweep.py:763` — cross completion capped: `max_price = max(max_pair_cost - fill_cost, 0.0)`
- `risk.py:363` — risk gate refuses the bid

**Conclusion:** this is a *verification* plan, not an implementation plan. The live data reflects a run before these caps landed. The taste call is whether to also forbid resting when `pair_cost_at_touch >= 1.00` (currently only checked for the skip path at `quotes.py:592`, not after inventory accrues).

---

## Design review (what changes)
1. **Add post-inventory pair-cost guard in the quote loop.** After `other_avg > 0` check (line 633), also skip if `inv.pair_cost() >= cfg.max_pair_cost` for the resting side — so a market that was fillable at touch but drifted to ≥$1.00 mid-session stops adding.
2. **Add a regression test** `tests/test_quotes.py::test_pair_cap_blocks_above_one_dollar_after_fill` — build inv with heavy avg 0.97, assert UP quote at 0.03 is rejected.
3. **KPI census already records** `pair_cost_at_touch` + `fillable_sub_one` — verify the dashboard surfaces "fillable_rate" honestly (it does: `kpi.py:379`).

---

## Eng review (mechanical)
- File: `strategy/quotes.py` line ~633 (add one `if` guard).
- File: `tests/test_quotes.py` (new test, mirrors `test_the_pair_cap_applies_to_the_rewards_objective`).
- No new config knob — reuse `max_pair_cost`.
- Verification: `pytest tests/test_quotes.py -q` must pass; then a fresh paper run should show `median_pair_cost < 1.00` and `pairs_under_1 == 100%`.

---

## Taste decisions (approval gate)
- **T1:** Should the cap be `0.995` (current) or tightened to `0.99` to leave gas/edge margin? *Recommend 0.99 — the pair pays exactly $1.00, so any cost ≥$0.99 leaves ≤1c before fees; 0.995 is too thin.*
- **T2:** Should we reject a market entirely (sit out) when `pair_cost_at_touch >= 1.00`, or just stop adding? *Current design sits out (line 592). Keep.*
