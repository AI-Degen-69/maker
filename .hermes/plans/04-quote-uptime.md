# Plan 04 — Quote Uptime Instrumentation (Paper-Trading Realism)

**Branch:** `harden-to-reality` · **Baseline:** 703 passed
**Source of truth:** `strategy/store.py:140` (`decisions` table), `strategy/store.py:987` (`log_decision`), `strategy/sweep.py:1083-1134` (hedge decisions logged), `strategy/kpi.py:392` (`top_skip_reasons: []` from `decisions_non_quote`)
**Objective:** Record WHY every quote is skipped, so the 0.75% fill rate is diagnosable instead of opaque.

---

## CEO review
The live KPI shows `top_skip_reasons: []` and `quote_uptime: null` because **resting-quote skips are never logged** — only HEDGE/BLOCKED decisions are (sweep.py:1083+). The `decisions` table has 0 rows in the live run. Without this, you cannot tell if the 0.75% fill rate is depth, spread, band, budget, or gate — you just see "no fills."

**Conclusion:** Every `continue` in `decide_quotes` (quotes.py) that *skips a side* must call `store.log_decision(action="SKIP_<REASON>", reason_code=...)`. This is pure instrumentation — no strategy change.

---

## Design review
1. **Wrap each `continue` in `decide_quotes`** (quotes.py:579 rebate window, :583 band, :622 band, :633 pair cost, :641 heavy-side) with a `store.log_decision(action="SKIP_REBATE_WINDOW"/"SKIP_BAND"/"SKIP_PAIR_COST"/"SKIP_HEAVY", reason_code=..., side=side, mid=mid, price=price)`.
2. **Collapse duplicates** — `log_decision` already supports `count` increment (store.py:155); use `reason_code` as the collapse key.
3. **KPI already reads** `decisions_non_quote` → `top_skip_reasons` (kpi.py:392). After logging, the dashboard surfaces the dominant skip cause.
4. **Regression test** `tests/test_quotes.py::test_skips_are_logged_with_reason` — assert a band-rejected market yields a `SKIP_BAND` decision row.

---

## Eng review
- Files: `strategy/quotes.py` (add log calls at each skip), `tests/test_quotes.py`.
- `log_decision` signature already supports all fields needed.
- Verification: `pytest tests/test_quotes.py` green; a paper run should populate `decisions` and show `top_skip_reasons` non-empty (e.g. `SKIP_PAIR_COST` dominant).

---

## Taste decisions (approval gate)
- **T1:** Log every skip (more DB rows, clearer diagnosis) vs sample (log 1 in 10 to limit growth)? *Recommend log every skip — `decisions` collapses by `reason_code`, so row count stays bounded by distinct causes.*
- **T2:** Include `t_remaining` and `balance` in skip rows (fat rows) or minimal (side+reason)? *Recommend include both — they are already in `log_decision` and explain WHY a skip happened at that moment.*
