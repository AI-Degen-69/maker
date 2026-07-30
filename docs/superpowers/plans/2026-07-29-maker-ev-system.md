# Maker Fleet EV System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure the cost of being filled (markout), allocate capital by marginal return instead of a flat constant, and automatically widen-then-exit markets whose fills lose money.

**Architecture:** Three independent units. `strategy/markout.py` records a reference mid at fill time and re-samples it at fixed horizons, yielding per-market cost-of-fill. `strategy/allocate.py` is a pure water-filling function turning measured competitor depth into per-market capital. `strategy/gate.py` is a small state machine consulted inside `_decide_quotes_rewards`. All three read and write through the existing `strategy/store.py`.

**Tech Stack:** Python 3.12, sqlite3, pytest, FastAPI (dashboard only).

## Global Constraints

- Objective is long-run EV, not daily drawdown control.
- Markout reference mid MUST exclude our own resting size. In paper mode this is automatic (our orders never reach the venue book). A live-mode guard is required — see Task 6.
- The existing `max_naked_shares = 360` hard cap stays. Markout is the slow signal; the cap is the fast one. Neither replaces the other.
- Never blend estimates into realized P&L. Markout is reported separately.
- Starting parameters are hypotheses, not tuned values: horizons 5m/1h/6h, `markout_min_sample = 20`, `markout_widen_threshold = -0.005`, `widen_offset` 0.020 → 0.035, `marginal_return_floor = 0.02`, `allocation_budget = 1200`.
- This directory is not a git repository. Commit steps are omitted; verification is the test run.

---

## File Structure

- Create `strategy/markout.py` — record and aggregate cost-of-fill. Depends on `store`.
- Create `strategy/allocate.py` — pure water-filling allocator. No I/O.
- Create `strategy/gate.py` — pure state machine. No I/O.
- Modify `strategy/store.py` — add `markouts` table + writers.
- Modify `strategy/config.py` — add the six parameters above.
- Modify `strategy/quotes.py` — consult the gate for the per-market offset.
- Modify `strategy/fleet.py` — call the meter, apply allocation, expose gate state.
- Create `tests/test_markout.py`, `tests/test_allocate.py`, `tests/test_gate.py`.

---

### Task 1: Config parameters

**Files:**
- Modify: `strategy/config.py`

**Interfaces:**
- Produces: `cfg.markout_horizons: tuple[float, ...]`, `cfg.markout_min_sample: int`, `cfg.markout_widen_threshold: float`, `cfg.widen_offset: float`, `cfg.marginal_return_floor: float`, `cfg.allocation_budget: float`

- [ ] **Step 1: Add fields to MakerConfig**

```python
    # --- EV system (docs/superpowers/specs/2026-07-29-maker-ev-system-design.md)
    # Horizons at which a fill is re-priced, in seconds. 5m catches immediate
    # adverse flow; 6h is the shortest horizon on which a long-dated market
    # plausibly repriced on news.
    markout_horizons: tuple[float, ...] = (300.0, 3600.0, 21600.0)
    # Below this many fills the mean markout is dominated by noise on a thin
    # book, and evicting a sound market on noise costs real rent.
    markout_min_sample: int = 20
    # Half the ~1c edge a paired quote earns. Losing more than this per share
    # means the fill was unprofitable even before inventory risk.
    markout_widen_threshold: float = -0.005
    # Widened quotes stay inside the 4.5c reward band, so rent continues.
    widen_offset: float = 0.035
    # Marginal $/day per $ committed, below which capital is better left idle.
    marginal_return_floor: float = 0.02
    allocation_budget: float = 1200.0
```

- [ ] **Step 2: Verify config loads**

Run: `python -c "from strategy.config import load; c=load(); print(c.markout_horizons, c.allocation_budget)"`
Expected: `(300.0, 3600.0, 21600.0) 1200.0`

---

### Task 2: Markout storage

**Files:**
- Modify: `strategy/store.py`

**Interfaces:**
- Produces: `store.log_markout_open(**kw) -> int`, `store.pending_markouts(now, horizons) -> list[dict]`, `store.close_markout(rowid, horizon_idx, mid_later, last=False) -> None`, `store.markout_rows() -> list[dict]`

One row per fill; columns fill in as each horizon matures. `ref_mid_source` records whether the mid was venue-clean, so a live run that cannot guarantee it writes `'contaminated'` and the aggregate excludes those rows.

- [ ] **Step 1: Add table to SCHEMA**

```sql
CREATE TABLE IF NOT EXISTS markouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    condition_id TEXT,
    market_slug TEXT,
    side TEXT,
    fill_price REAL,
    size REAL,
    ref_mid REAL,              -- mid at fill time, our own size excluded
    ref_mid_source TEXT,       -- 'venue_clean' | 'contaminated'
    mid_h0 REAL, mid_h1 REAL, mid_h2 REAL,
    done INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mk_done ON markouts(done, ts);
```

- [ ] **Step 2: Add writers**

```python
def log_markout_open(**kw) -> int:
    with db() as c:
        cur = c.execute(
            "INSERT INTO markouts (ts, condition_id, market_slug, side, "
            "fill_price, size, ref_mid, ref_mid_source) VALUES (?,?,?,?,?,?,?,?)",
            (kw["ts"], kw["condition_id"], kw["market_slug"], kw["side"],
             kw["fill_price"], kw["size"], kw["ref_mid"],
             kw.get("ref_mid_source") or "venue_clean"))
        return cur.lastrowid


def pending_markouts(now: float, horizons) -> list[dict]:
    """Rows with at least one horizon matured and not yet recorded."""
    out = []
    with db() as c:
        cur = c.execute("SELECT * FROM markouts WHERE done = 0")
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            row = dict(zip(cols, r))
            for i, h in enumerate(horizons):
                if row.get(f"mid_h{i}") is None and now - row["ts"] >= h:
                    row["_due"] = i
                    out.append(row)
                    break
    return out


def close_markout(rowid: int, horizon_idx: int, mid_later: float,
                  last: bool = False) -> None:
    with db() as c:
        c.execute(f"UPDATE markouts SET mid_h{horizon_idx} = ?, done = ? "
                  "WHERE id = ?", (mid_later, 1 if last else 0, rowid))


def markout_rows() -> list[dict]:
    with db() as c:
        cur = c.execute("SELECT * FROM markouts")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
```

- [ ] **Step 3: Verify schema applies to a fresh DB**

Run: `MAKER_DB=run/t.db python -c "from strategy import store; store.log_markout_open(ts=1.0, condition_id='x', market_slug='s', side='UP', fill_price=0.5, size=10, ref_mid=0.52); print(store.pending_markouts(1e9,(300.0,)))"`
Expected: one dict with `_due` = 0. Then delete `run/t.db`.

---

### Task 3: Markout computation

**Files:**
- Create: `strategy/markout.py`
- Test: `tests/test_markout.py`

**Interfaces:**
- Consumes: `store.log_markout_open`, `store.pending_markouts`, `store.close_markout`, `store.markout_rows`
- Produces: `markout_per_share(fill_price, mid_later, side) -> float`, `_stats_from_rows(rows, min_sample) -> dict`, `per_market_stats(min_sample) -> dict[str, dict]`, `sample_due(books_by_cid, now, horizons) -> int`

Sign convention: we only ever BUY. Buying UP at 0.57 when the UP mid later sits at 0.55 lost 2c/share. Buying DOWN at 0.38 when the DOWN mid later sits at 0.40 gained 2c/share. Each side is measured against its own token's mid, so one formula covers both.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from strategy.markout import markout_per_share, _stats_from_rows


def _row(mk, source="venue_clean"):
    return {"markout": mk, "ref_mid_source": source}


def test_buy_that_drifts_down_is_a_loss():
    assert markout_per_share(0.57, 0.55, "UP") == pytest.approx(-0.02)


def test_buy_that_drifts_up_is_a_gain():
    assert markout_per_share(0.38, 0.40, "DOWN") == pytest.approx(0.02)


def test_stats_ignore_markets_under_min_sample():
    stats = _stats_from_rows([_row(-0.02)] * 3, min_sample=20)
    assert stats["verdict"] == "insufficient_sample"
    assert stats["mean_per_share"] is None


def test_stats_report_mean_once_sample_is_adequate():
    stats = _stats_from_rows([_row(-0.02)] * 20, min_sample=20)
    assert stats["mean_per_share"] == pytest.approx(-0.02)
    assert stats["verdict"] == "losing"


def test_contaminated_rows_are_excluded():
    rows = [_row(-0.02, source="contaminated")] * 30
    stats = _stats_from_rows(rows, min_sample=20)
    assert stats["verdict"] == "insufficient_sample"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_markout.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'strategy.markout'`

- [ ] **Step 3: Implement**

```python
"""Cost of being filled, measured in hours instead of years.

These markets resolve in 2026-2027, so settlement P&L reads $0.00 for months.
Markout answers the same question early: after we were filled, where did the
price actually go? A maker who is systematically filled just before the price
moves against him is losing money no matter what the rent line says.
"""
from __future__ import annotations

import statistics

from strategy import store


def markout_per_share(fill_price: float, mid_later: float, side: str) -> float:
    """We only ever buy, so a mid below our fill price means the fill was
    informed against us. `side` is accepted because each side is measured
    against its own token's mid; the arithmetic is identical for both."""
    return mid_later - fill_price


def _stats_from_rows(rows: list[dict], min_sample: int) -> dict:
    clean = [r for r in rows if r.get("ref_mid_source") != "contaminated"]
    if len(clean) < min_sample:
        return {"n": len(clean), "verdict": "insufficient_sample",
                "mean_per_share": None}
    mean = statistics.mean(r["markout"] for r in clean)
    return {"n": len(clean), "mean_per_share": mean,
            "verdict": "losing" if mean < 0 else "earning"}


def _matured(row: dict) -> list[float]:
    """Markout at every horizon that has already been sampled for this row."""
    out = []
    for i in range(3):
        mid = row.get(f"mid_h{i}")
        if mid is not None:
            out.append(markout_per_share(row["fill_price"], mid, row["side"]))
    return out


def per_market_stats(min_sample: int) -> dict[str, dict]:
    """Longest matured horizon per fill, grouped by market."""
    by: dict[str, list[dict]] = {}
    for r in store.markout_rows():
        m = _matured(r)
        if not m:
            continue
        by.setdefault(r["condition_id"], []).append(
            {"markout": m[-1], "ref_mid_source": r.get("ref_mid_source")})
    return {cid: _stats_from_rows(rows, min_sample) for cid, rows in by.items()}


def sample_due(books_by_cid: dict, now: float, horizons) -> int:
    """Record the mid at every horizon that has just matured. Returns how many
    rows were updated. `books_by_cid` maps condition_id -> {side: mid}."""
    n = 0
    for row in store.pending_markouts(now, horizons):
        mids = books_by_cid.get(row["condition_id"])
        if not mids:
            continue
        mid = mids.get(row["side"])
        if mid is None:
            continue
        i = row["_due"]
        store.close_markout(row["id"], i, mid, last=(i == len(horizons) - 1))
        n += 1
    return n
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_markout.py -v`
Expected: 5 passed

---

### Task 4: Allocator

**Files:**
- Create: `strategy/allocate.py`
- Test: `tests/test_allocate.py`

**Interfaces:**
- Produces: `competitor_depth(capital, share) -> float`, `income(capital, daily, T) -> float`, `marginal(capital, daily, T) -> float`, `allocate(markets, budget, floor, step=5.0) -> dict[str, float]`

`markets` is a list of dicts with keys `cid`, `daily`, `capital`, `share`. Returns capital per market, in dollars.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from strategy.allocate import allocate, competitor_depth, income, marginal


def test_competitor_depth_inverts_the_share_formula():
    assert competitor_depth(115.0, 0.6334) == pytest.approx(66.56, abs=0.5)


def test_income_matches_the_observed_market():
    assert income(115.0, 50.0, 66.56) == pytest.approx(31.67, abs=0.05)


def test_marginal_return_is_higher_on_the_thin_market():
    thin = marginal(115.0, 50.0, 66.56)
    crowded = marginal(115.0, 100.0, 9631.0)
    assert thin > crowded * 5


def test_allocator_drops_markets_below_the_floor():
    markets = [
        {"cid": "good", "daily": 50.0, "capital": 115.0, "share": 0.6334},
        {"cid": "bad", "daily": 100.0, "capital": 115.0, "share": 0.0118},
    ]
    out = allocate(markets, budget=1200.0, floor=0.02)
    assert out["bad"] == 0.0
    assert out["good"] > 0.0


def test_allocator_respects_the_budget():
    markets = [{"cid": f"m{i}", "daily": 50.0, "capital": 115.0,
                "share": 0.6334} for i in range(5)]
    out = allocate(markets, budget=1200.0, floor=0.02)
    assert sum(out.values()) <= 1200.0 + 1e-6


def test_allocator_never_pushes_past_the_pot_ceiling():
    markets = [{"cid": "one", "daily": 50.0, "capital": 115.0, "share": 0.6334}]
    out = allocate(markets, budget=100000.0, floor=0.02)
    T = competitor_depth(115.0, 0.6334)
    assert income(out["one"], 50.0, T) < 50.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_allocate.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'strategy.allocate'`

- [ ] **Step 3: Implement**

```python
"""Capital allocation by marginal return.

quote_shares was a flat 120 on every market, which produced returns spanning
27.58%/day to 0.28%/day on identical $115 stakes. Income is pot x share, and
share is ours/(ours+theirs) -- so the pot size barely matters and the
competition does. Big pots are big precisely because nobody wants to quote
them cheaply.
"""
from __future__ import annotations


def competitor_depth(capital: float, share: float) -> float:
    """Invert share = C/(C+T) to recover T, competitors' resting depth in the
    same units as our own committed capital."""
    if share <= 0.0:
        return float("inf")
    if share >= 1.0:
        return 0.0
    return capital * (1.0 - share) / share


def income(capital: float, daily: float, T: float) -> float:
    if capital <= 0:
        return 0.0
    return daily * capital / (capital + T)


def marginal(capital: float, daily: float, T: float) -> float:
    """d(income)/d(capital). Falls off as our own size dilutes our share --
    which is what stops the allocator emptying the budget into one market."""
    return daily * T / ((capital + T) ** 2)


def allocate(markets: list[dict], budget: float, floor: float,
             step: float = 5.0) -> dict[str, float]:
    """Water-fill: give each increment to whichever market has the highest
    marginal return, until the budget is spent or every market pays less than
    the floor. Leftover budget is deliberately not forced out -- idle capital
    beats capital earning under the floor."""
    T = {m["cid"]: competitor_depth(m["capital"], m["share"]) for m in markets}
    daily = {m["cid"]: m["daily"] for m in markets}
    out = {m["cid"]: 0.0 for m in markets}
    spent = 0.0
    while spent < budget:
        best, best_m = None, floor
        for cid in out:
            mg = marginal(out[cid], daily[cid], T[cid])
            if mg > best_m:
                best, best_m = cid, mg
        if best is None:
            break
        out[best] += step
        spent += step
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_allocate.py -v`
Expected: 6 passed

---

### Task 5: Quality gate

**Files:**
- Create: `strategy/gate.py`
- Test: `tests/test_gate.py`

**Interfaces:**
- Produces: `offset_for(state, base, widened) -> float`, `next_state(state, stats, cfg) -> str`. States: `"NORMAL"`, `"WIDENED"`, `"EXITED"`.
- Consumes: stats dicts from `markout._stats_from_rows` (`verdict`, `mean_per_share`, `n`).

- [ ] **Step 1: Write the failing test**

```python
import dataclasses
import pytest
from strategy.config import load as load_cfg
from strategy.gate import next_state, offset_for


def _c():
    return dataclasses.replace(load_cfg(), markout_min_sample=20,
                               markout_widen_threshold=-0.005)


def test_thin_sample_never_moves_off_normal():
    s = {"verdict": "insufficient_sample", "mean_per_share": None, "n": 3}
    assert next_state("NORMAL", s, _c()) == "NORMAL"


def test_losing_market_widens_first():
    s = {"verdict": "losing", "mean_per_share": -0.02, "n": 25}
    assert next_state("NORMAL", s, _c()) == "WIDENED"


def test_still_losing_after_widening_exits():
    s = {"verdict": "losing", "mean_per_share": -0.02, "n": 60}
    assert next_state("WIDENED", s, _c()) == "EXITED"


def test_recovery_returns_to_normal():
    s = {"verdict": "earning", "mean_per_share": 0.01, "n": 60}
    assert next_state("WIDENED", s, _c()) == "NORMAL"


def test_exit_is_terminal():
    s = {"verdict": "earning", "mean_per_share": 0.01, "n": 99}
    assert next_state("EXITED", s, _c()) == "EXITED"


def test_widened_state_quotes_further_from_mid():
    assert offset_for("WIDENED", 0.020, 0.035) == 0.035
    assert offset_for("NORMAL", 0.020, 0.035) == 0.020
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gate.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'strategy.gate'`

- [ ] **Step 3: Implement**

```python
"""Widen before exiting.

A market priced 1c too aggressively and a market full of informed flow look
identical on a single reading. Only the second stays negative after we back
off, and only the second is worth giving up the rent for.
"""
from __future__ import annotations

NORMAL, WIDENED, EXITED = "NORMAL", "WIDENED", "EXITED"


def offset_for(state: str, base: float, widened: float) -> float:
    return widened if state == WIDENED else base


def next_state(state: str, stats: dict, cfg) -> str:
    if state == EXITED:
        return EXITED
    if stats.get("verdict") == "insufficient_sample":
        return state
    mean = stats.get("mean_per_share")
    if mean is None:
        return state
    losing = mean < cfg.markout_widen_threshold
    if state == NORMAL:
        return WIDENED if losing else NORMAL
    # WIDENED: demand a second full sample before surrendering the rent.
    if losing and stats.get("n", 0) >= 2 * cfg.markout_min_sample:
        return EXITED
    return NORMAL if not losing else WIDENED
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_gate.py -v`
Expected: 6 passed

---

### Task 6: Wire into the fleet

**Files:**
- Modify: `strategy/fleet.py`
- Modify: `strategy/quotes.py`

- [ ] **Step 1: Record a markout row on every fill**

In `strategy/fleet.py`, in the fill loop beside `store.log_fill(...)`:

```python
            store.log_markout_open(
                ts=now, condition_id=m.condition_id,
                market_slug=m.market_slug, side=f.side,
                fill_price=f.price, size=f.size,
                ref_mid=mid_price(book.get("best_bid"), book.get("best_ask")),
                # Paper mode: our quotes never reach the venue, so the fetched
                # book is already clean of our own size. A LIVE run MUST pass
                # 'contaminated' unless it subtracts our own resting size --
                # otherwise markout measures our own footprint and reports it
                # as edge.
                ref_mid_source="venue_clean")
```

- [ ] **Step 2: Sample matured horizons on each visit**

In `visit()`, after both books are fetched and before the requote:

```python
    markout.sample_due(
        {m.condition_id: {"UP": mid_price(up.get("best_bid"), up.get("best_ask")),
                          "DOWN": mid_price(dn.get("best_bid"), dn.get("best_ask"))}},
        now, cfg.markout_horizons)
```

- [ ] **Step 3: Recompute gate state per market each visit**

In `visit()`, before `decide_quotes`:

```python
    stats = markout.per_market_stats(cfg.markout_min_sample).get(
        m.condition_id, {"verdict": "insufficient_sample",
                         "mean_per_share": None, "n": 0})
    st.gate = gate.next_state(getattr(st, "gate", gate.NORMAL), stats, cfg)
    cfg = replace(cfg, gate_state=st.gate)
```

Add `gate_state: str = "NORMAL"` to `MakerConfig` in Task 1's block, and `self.gate = gate.NORMAL` to `MarketState.__init__`.

- [ ] **Step 4: Apply gate to the offset and skip exited markets**

In `_decide_quotes_rewards`, at the top of the function:

```python
    if getattr(cfg, "gate_state", "NORMAL") == "EXITED":
        return [], "market exited: fills lost money after widening"
```

and replace `offset = cfg.reward_offset + skew` with:

```python
        base = gate.offset_for(getattr(cfg, "gate_state", "NORMAL"),
                               cfg.reward_offset, cfg.widen_offset)
        offset = base + skew
```

- [ ] **Step 5: Expose gate and markout in `_live`**

In the `st.spec["_live"]` dict:

```python
        "gate": getattr(st, "gate", "NORMAL"),
```

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: all pass, including the 50 pre-existing tests

---

### Task 7: Surface it on the dashboard

**Files:**
- Modify: `server/fleet_dash.py`

- [ ] **Step 1: Add markout + gate to each row in `/api/fleet`**

```python
            "gate": live.get("gate", "NORMAL"),
            "markout": mk.get(s["cid"], {}).get("mean_per_share"),
            "markout_n": mk.get(s["cid"], {}).get("n", 0),
```

where `mk = _markout_stats()` reads `markouts` from the fleet DB the same way `_realized()` reads `fills`.

- [ ] **Step 2: Add a COST OF FILLS aggregate tile**

```javascript
    + K('COST OF FILLS',
        t.markout_n >= 20 ? usd(t.markout_total) : 'measuring',
        t.markout_n + ' fills measured', cls(t.markout_total))
```

- [ ] **Step 3: Add a MARKOUT column to the table**

```javascript
      <td class="num ${m.markout==null?'d':(m.markout<0?'r_':'g')}">
        ${m.markout==null?'-':(100*m.markout).toFixed(2)+'c'}</td>
```

- [ ] **Step 4: Run the page test**

Run: `python -m pytest tests/test_dashboard_page.py -q`
Expected: pass

---

## Self-Review

**Spec coverage:** Component 1 (markout meter) → Tasks 2, 3, 6. Component 2 (allocator) → Task 4. Component 3 (quality gate) → Tasks 5, 6. Parameters table → Task 1. Dashboard surfacing → Task 7. Naked cap → already shipped, deliberately untouched. No gaps.

**Placeholders:** none — every step carries runnable code or an exact command.

**Type consistency:** `_stats_from_rows` and `per_market_stats` return dicts keyed `verdict` / `mean_per_share` / `n`; `next_state` reads exactly those three. `allocate()` returns `dict[cid, float]` of dollars, converted to shares by the caller. `sample_due` takes `{cid: {side: mid}}`, matching what Task 6 Step 2 builds.

**Known deviation from spec:** the spec named `record_fill_mid` / `sample_horizons` / `per_market_stats`. Implemented as `store.log_markout_open` (matching the existing `log_fill` convention) plus `markout.sample_due` / `markout.per_market_stats`. Same behaviour, follows the codebase's naming.

**Deferred:** Task 4 builds and tests the allocator but does not wire it into `fleet.py`. Changing live position sizing while markout has zero samples would be optimising against the metric we just admitted we cannot yet measure. Wire it once markout has data.
