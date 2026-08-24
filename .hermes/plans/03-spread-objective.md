# Plan 03 — Spread Objective for Short-Dated Markets (Paper-Trading Realism)

**Branch:** `harden-to-reality` · **Baseline:** 703 passed
**Source of truth:** `strategy/config.py:43` (`objective: str = "rewards"`), `strategy/quotes.py:532` (`if cfg.objective == "rewards"`), `strategy/allocate.py:63` (`spread_capture_daily`), `strategy/allocate.py:170` (`allocate_fundable`), `strategy/fleet.py:421` (`self.source = "rewards" if self.daily > 0 else "spread"`)
**Objective:** Route short-dated / low-rebate markets to the spread-capture objective instead of resting at mid−2c (which gets 0 fills behind 400+ queue depth).

---

## CEO review
The `rewards` objective rests at `mid - 2c` (`offset_c=2.0` in live `reward_samples`). On books with 400+ shares ahead, fill rate is **0.5%** (`fill_by_queue` 400+ bucket). The `spread` objective exists (`allocate.spread_capture_daily` at `allocate.py:63`, `spread_capture_frac=0.25` at `config.py:326`) but is only auto-selected when `daily == 0` (`fleet.py:421`). It is **never explicitly selected per market** — there is no `objective="spread"` branch in `quotes.py` (only `== "rewards"` at line 532).

**Conclusion:** The spread path is half-wired. Plan = complete the wiring so a market can be tagged `objective="spread"` and `decide_quotes` uses `spread_capture_daily` sizing + a real resting offset that captures the book spread, not mid−2c.

---

## Design review
1. **Add `objective == "spread"` branch in `quotes.py`** `decide_quotes` — rest at `best_bid + tick` (inside the bid, not the ask) to capture the bid-ask spread on the taker's touch, sized by `spread_capture_daily`.
2. **Allocator tag:** `allocate_fundable` already splits by `source`; extend to set `objective="spread"` for short-dated markets (e.g. `t_remaining < 1 day` OR `daily == 0`).
3. **Config:** add `objective_spread_offset_c: float = 0.5` (rest just inside the bid) — distinct from `reward_offset`.
4. **Regression test** `tests/test_quotes.py::test_spread_objective_rests_inside_bid`.

---

## Eng review
- Files: `strategy/quotes.py` (new branch at ~532), `strategy/allocate.py` (`allocate_fundable` tag), `strategy/config.py` (new knob), `tests/test_quotes.py`.
- Reuse `mid_price`, `spread_capture_daily`.
- Verification: `pytest tests/test_quotes.py tests/test_allocate.py` green; a paper run on a short-dated MLB market should show `objective=spread` in `fleet_state` and a non-zero `spread_capture` in KPI (currently `$0.00`).

---

## Taste decisions (approval gate)
- **T1:** Rest inside the bid (capture maker spread, but queue behind bids) vs cross the spread (taker, guaranteed fill, pays fee)? *Recommend rest-inside-bid for paper realism; measure fill rate before considering cross.*
- **T2:** Auto-switch objective by `t_remaining` threshold, or explicit per-market tag from the ranker? *Recommend auto by `t_remaining < 1d` — less operator toil.*
