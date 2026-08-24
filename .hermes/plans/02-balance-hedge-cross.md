# Plan 02 — Balance Hedge as a True Cross (Paper-Trading Realism)

**Branch:** `harden-to-reality` · **Baseline:** 703 passed
**Source of truth:** `strategy/sweep.py:1040-1138` (`_requote` emergency hedge via `engine.cross`), `strategy/config.py:681` (`balance_hedge_sec=20.0`), `strategy/sweep.py:680-770` (U35 pairs-only rule)
**Objective:** Ensure the one-sided-fill hedge actually crosses the book, not rests a passive bid that fills 0.

---

## CEO review
Two distinct hedges exist:
1. **Emergency hedge** (mid falling below avg, inside cap) — `sweep.py:1040+`. **ALREADY FIXED**: uses `engine.cross()` (real ask-depth walk), not a passive bid. The old "bid at ask" bug (28/07) is dead.
2. **Close-window balance hedge** (`balance_hedge_sec=20.0`) — meant to cross the missing leg near resolution to guarantee a balanced settlement. **DORMANT**: `kpi.balance_hedges == 0` in the live run; never fired. The research log (28/07) says it posted a *passive bid at the ask* and filled 0 — that code path may still be the passive variant.

**Conclusion:** Verify emergency path is live (it is). Fix/confirm the close-window path uses `engine.cross`, not a resting bid. Surface the cost/benefit taste call.

---

## Design review
1. **Locate the `balance_hedge_sec` trigger** in `sweep.py`/`fleet.py`. Confirm it calls `engine.cross(token, side, size, asks, now)` — NOT `engine.post(...)`. If it posts, convert to cross.
2. **Add event logging** `BALANCE_HEDGE` via `store.log_decision(action="CROSS_HEDGE", ...)` so `kpi.balance_hedges` increments (currently counts `CROSS_HEDGE` rows — none exist).
3. **Regression test** `tests/test_sweep.py::test_balance_hedge_crosses_not_rests` — assert a near-close imbalanced market produces a `crossed=True` fill, not a resting quote.

---

## Eng review
- Files: `strategy/sweep.py` (close-window block), `strategy/store.py` (log_decision already supports `CROSS_HEDGE`), `tests/test_sweep.py`.
- Reuse `_affordable_cross_size` (already used at line 1080).
- Verification: unit test + a paper run on a lopsided market near close should show `balance_hedges > 0` and the settled market `balance == 1.0`.

---

## Taste decisions (approval gate)
- **T1:** Accept taker fee on the close-window hedge (costs ~1.75c/sh) to guarantee balance, OR let it ride to settlement and risk full $1.00 loss? *Recommend cross — the fee is bounded cents; the unresolved leg is bounded by $1.00.*
- **T2:** Fire the close hedge at `balance_hedge_sec=20.0` before close, or earlier (e.g. 60s) to ensure fill? *Recommend 20s as designed; if fill rate is 0, escalate to 60s.*
