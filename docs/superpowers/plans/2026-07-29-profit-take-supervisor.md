# Profit-Taking + Supervisor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close paired positions early once the market has moved far enough to clear the taker fee and still profit, and keep both fleet processes alive unattended.

**Architecture:** Two independent units. `strategy/profit_take.py` is a pure decision function consulted inside `fleet.visit()`; it never mutates state and never talks to the venue. `strategy/supervisor.py` is a new top-level process that owns `strategy.fleet` and the dashboard as subprocesses and restarts either on crash. Persistence for closes goes through the existing `strategy/store.py`.

**Tech Stack:** Python 3.12, sqlite3, pytest, subprocess.

## Global Constraints

- Everything stays PAPER/DEMO. A close is simulated inventory arithmetic plus a DB row — no order is sent. `cfg.sim_only` stays `True`.
- Profit-taking applies to PAIRED shares only (`min(up_shares, down_shares)`). Naked shares are the existing skew / `max_naked_shares` / `max_fleet_naked_usd` machinery's business and must not be touched here.
- Threshold, fixed by the user: net profit per share must be at least `0.02` AFTER the taker fee. The fee is `0.017` per leg and a close sells BOTH legs, so the fee per pair is `0.034` and the gross move required is `0.054`.
- The supervisor watches BOTH `strategy.fleet` and the dashboard. It does NOT survive a machine reboot — out of scope.
- `strategy/profit_take.py` does no I/O. `strategy/supervisor.py` does not import strategy modules; it only spawns processes.
- This directory is not a git repository. Commit steps are omitted; the test run is the verification.
- `Inventory` (in `strategy/quotes.py`) has fields `up_shares`, `down_shares`, `up_cost`, `down_cost`, `fills` and a method `avg(side)`. Do not change its definition.

---

## File Structure

- Create `strategy/profit_take.py` — pure close decision. No I/O.
- Create `strategy/supervisor.py` — process babysitter. No strategy imports.
- Modify `strategy/config.py` — two new parameters.
- Modify `strategy/store.py` — `closes` table + `log_close`.
- Modify `strategy/fleet.py` — consult the decision, apply it to inventory, log it.
- Create `tests/test_profit_take.py`, `tests/test_supervisor.py`.

---

### Task 1: Config parameters

**Files:**
- Modify: `strategy/config.py`

**Interfaces:**
- Produces: `cfg.profit_take_fee_per_share: float`, `cfg.profit_take_net_threshold: float`

- [ ] **Step 1: Add fields to MakerConfig**

Insert immediately after the `gate_state` field (currently `strategy/config.py:97`), inside the `# --- EV system` block:

```python
    # --- profit taking ----------------------------------------------------
    # Nothing in this strategy ever closed a position: every fill rode to
    # settlement in 2027, so a filled pair immobilised capital that had been
    # earning daily rent. Selling a pair that has appreciated converts locked
    # capital back into working capital.
    #
    # Selling means crossing the spread, so BOTH legs pay the taker fee. The
    # move therefore has to clear two fees before it clears anything else.
    profit_take_fee_per_share: float = 0.017
    # Required profit per share AFTER both fees. Set at roughly one fee's
    # width again, so a close is only taken on a move clearly larger than the
    # cost of taking it -- at 1c the threshold sits inside the noise of a
    # one-tick book flicker and would close positions on nothing.
    profit_take_net_threshold: float = 0.020
```

- [ ] **Step 2: Verify config loads**

Run: `python -c "from strategy.config import load; c=load(); print(c.profit_take_fee_per_share, c.profit_take_net_threshold)"`
Expected: `0.017 0.02`

---

### Task 2: The close decision

**Files:**
- Create: `strategy/profit_take.py`
- Test: `tests/test_profit_take.py`

**Interfaces:**
- Consumes: `cfg.profit_take_fee_per_share`, `cfg.profit_take_net_threshold` (Task 1); `Inventory` from `strategy.quotes`.
- Produces: `should_close(inv, up_bid, dn_bid, cfg) -> dict` with keys `take: bool`, `shares: float`, `cost_basis: float`, `proceeds: float`, `fee: float`, `realized_pnl: float`, `why: str`.

Sign convention: we hold long UP and long DOWN shares bought at `avg("UP")` and `avg("DOWN")`. Closing means SELLING both, and a seller hits the BID. So exit value per pair is `up_bid + dn_bid` and cost per pair is `avg("UP") + avg("DOWN")`. The fee is charged per share on each of the two legs, i.e. `2 * fee_per_share` per pair.

An exit value above $1.00 for the pair is not a contradiction. A pair pays exactly $1.00 at settlement in 2027, but today's two bids can sum above that when the book is wide — and that is precisely the condition worth selling into. It is why the mechanism reads live bids rather than assuming $1.00.

- [ ] **Step 1: Write the failing test**

Create `tests/test_profit_take.py`:

```python
import pytest

from strategy.config import load as load_cfg
from strategy.profit_take import should_close
from strategy.quotes import Inventory


def _inv(up_sh=100.0, dn_sh=100.0, up_px=0.50, dn_px=0.45):
    return Inventory(up_shares=up_sh, down_shares=dn_sh,
                     up_cost=up_sh * up_px, down_cost=dn_sh * dn_px)


def _cfg():
    return load_cfg()


def test_no_paired_shares_never_closes():
    inv = Inventory(up_shares=100.0, up_cost=50.0)
    out = should_close(inv, 0.99, 0.99, _cfg())
    assert out["take"] is False


def test_missing_bid_never_closes():
    out = should_close(_inv(), None, 0.60, _cfg())
    assert out["take"] is False


def test_move_that_only_covers_the_fees_does_not_close():
    # cost 0.95, exit 0.99 -> gross 4c, fees 3.4c, net 0.6c < 2c threshold
    out = should_close(_inv(), 0.54, 0.45, _cfg())
    assert out["take"] is False


def test_move_past_the_threshold_closes():
    # cost 0.95, exit 1.01 -> gross 6c, fees 3.4c, net 2.6c >= 2c threshold
    out = should_close(_inv(), 0.56, 0.45, _cfg())
    assert out["take"] is True
    assert out["shares"] == pytest.approx(100.0)


def test_realized_pnl_is_proceeds_minus_cost_minus_fee():
    out = should_close(_inv(), 0.56, 0.45, _cfg())
    assert out["proceeds"] == pytest.approx(101.0)
    assert out["cost_basis"] == pytest.approx(95.0)
    assert out["fee"] == pytest.approx(3.4)      # 2 legs x 100sh x 0.017
    assert out["realized_pnl"] == pytest.approx(2.6)


def test_only_the_paired_portion_is_closed():
    # 150 UP vs 100 DOWN: the 50 naked UP shares are not this mechanism's
    # business and must be left alone.
    inv = _inv(up_sh=150.0, dn_sh=100.0)
    out = should_close(inv, 0.56, 0.45, _cfg())
    assert out["take"] is True
    assert out["shares"] == pytest.approx(100.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_profit_take.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'strategy.profit_take'`

- [ ] **Step 3: Implement**

Create `strategy/profit_take.py`:

```python
"""Turn a locked pair back into working capital.

A matched pair pays exactly $1.00 -- in 2027. Until then the money that
bought it is immobilised, and immobilised money earns no daily rent. That is
the real cost of a fill, and nothing in the strategy addressed it: every
position simply rode to settlement.

If the pair can be sold today for more than it cost, the trade is finished
early -- the profit is booked and the capital goes back to work. Selling
means crossing the spread on BOTH legs, so the move has to be big enough to
cover two taker fees before it is worth anything at all.

Pure arithmetic. It decides; the caller applies.
"""
from __future__ import annotations

NO: dict = {"take": False, "shares": 0.0, "cost_basis": 0.0, "proceeds": 0.0,
            "fee": 0.0, "realized_pnl": 0.0, "why": ""}


def _no(why: str) -> dict:
    return dict(NO, why=why)


def should_close(inv, up_bid, dn_bid, cfg) -> dict:
    """Should the paired portion of this position be sold now?

    `up_bid` / `dn_bid` are the venue's best BIDS -- we would be the seller,
    and a seller hits the bid. Using the ask here would book a profit we
    could not actually get.

    Only `min(up_shares, down_shares)` is considered. The naked residue is
    left entirely alone: it is a directional bet, owned by skew and the
    exposure caps, and closing it here would be a different decision wearing
    this one's clothes.

    Never mutates `inv`.
    """
    paired = min(inv.up_shares, inv.down_shares)
    if paired <= 0:
        return _no("no paired shares")
    if not up_bid or not dn_bid:
        return _no("no two-sided book")

    cost_per_share = inv.avg("UP") + inv.avg("DOWN")
    exit_per_share = up_bid + dn_bid
    # Two legs, each paying the taker fee.
    fee_per_share = 2.0 * cfg.profit_take_fee_per_share
    net_per_share = exit_per_share - cost_per_share - fee_per_share

    out = {
        "take": net_per_share >= cfg.profit_take_net_threshold,
        "shares": paired,
        "cost_basis": paired * cost_per_share,
        "proceeds": paired * exit_per_share,
        "fee": paired * fee_per_share,
        "realized_pnl": paired * net_per_share,
    }
    out["why"] = (
        f"close {paired:.0f} pairs @ {exit_per_share:.4f} vs cost "
        f"{cost_per_share:.4f}, net {100 * net_per_share:.2f}c/sh"
        if out["take"] else
        f"hold: net {100 * net_per_share:.2f}c/sh under "
        f"{100 * cfg.profit_take_net_threshold:.2f}c")
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_profit_take.py -v`
Expected: 6 passed

---

### Task 3: Persist closes

**Files:**
- Modify: `strategy/store.py`

**Interfaces:**
- Produces: `store.log_close(**kw) -> None`, keys `condition_id`, `market_slug`, `shares`, `up_price`, `dn_price`, `cost_basis`, `proceeds`, `fee`, `realized_pnl`.

- [ ] **Step 1: Add the table to SCHEMA**

Append inside the `SCHEMA` string in `strategy/store.py`, after the `markouts` block:

```sql
-- One row per profit-taking close. Kept separate from `fills` because a close
-- is the only row in this database that books REALIZED money -- everything
-- else is an estimate or an open position. Blending the two is how a
-- projection turns into a reported profit.
CREATE TABLE IF NOT EXISTS closes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    condition_id TEXT,
    market_slug TEXT,
    shares REAL,               -- pairs closed
    up_price REAL,             -- bid we sold the UP leg into
    dn_price REAL,
    cost_basis REAL,
    proceeds REAL,
    fee REAL,
    realized_pnl REAL
);
CREATE INDEX IF NOT EXISTS idx_cl_ts ON closes(ts);
```

- [ ] **Step 2: Add the writer**

Add after `log_markout_open` in `strategy/store.py`:

```python
def log_close(**kw) -> None:
    """A profit-taking close. Realized money -- never blended into estimates."""
    with db() as c:
        c.execute(
            "INSERT INTO closes (ts, condition_id, market_slug, shares, "
            "up_price, dn_price, cost_basis, proceeds, fee, realized_pnl) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), kw["condition_id"], kw["market_slug"], kw["shares"],
             kw["up_price"], kw["dn_price"], kw["cost_basis"], kw["proceeds"],
             kw["fee"], kw["realized_pnl"]))
```

- [ ] **Step 3: Verify the schema applies to a fresh DB**

Run:
```bash
MAKER_DB=run/t.db python -c "
from strategy import store
store.log_close(condition_id='x', market_slug='s', shares=10, up_price=0.56,
                dn_price=0.45, cost_basis=9.5, proceeds=10.1, fee=0.34,
                realized_pnl=0.26)
with store.db() as c:
    print(list(c.execute('SELECT shares, realized_pnl FROM closes')))
"
```
Expected: `[(10.0, 0.26)]`. Then delete `run/t.db`.

---

### Task 4: Wire the close into the fleet

**Files:**
- Modify: `strategy/fleet.py`

**Interfaces:**
- Consumes: `profit_take.should_close` (Task 2), `store.log_close` (Task 3).

- [ ] **Step 1: Import the module**

In `strategy/fleet.py`, change line 27 from:

```python
from strategy import gate, markout, rewards, store
```

to:

```python
from strategy import gate, markout, profit_take, rewards, store
```

- [ ] **Step 2: Add the close block**

In `visit()`, insert immediately AFTER the markout/gate block (after the line
`cfg = replace(cfg, gate_state=st.gate, fleet_naked_usd=fleet_naked_usd)`)
and BEFORE the `# Requote.` comment:

```python
    # Take profit on the paired portion, if the market has moved far enough to
    # cover selling both legs and still pay. Wrapped for the same reason
    # `reallocate` is: a bug in a money-making refinement must not stop the
    # data collection the whole run exists for.
    try:
        pt = profit_take.should_close(st.inv, up.get("best_bid"),
                                      dn.get("best_bid"), cfg)
        if pt["take"]:
            n = pt["shares"]
            # Remove the closed pairs at their own average cost, which leaves
            # the average cost of whatever remains unchanged -- the naked
            # residue keeps the basis it actually has.
            #
            # Order matters: avg("UP") divides by up_shares, so the cost must
            # be decremented BEFORE the share count. Reversing these two lines
            # silently rewrites the basis of the remaining shares.
            st.inv.up_cost -= n * st.inv.avg("UP")
            st.inv.down_cost -= n * st.inv.avg("DOWN")
            st.inv.up_shares -= n
            st.inv.down_shares -= n
            store.log_close(
                condition_id=m.condition_id, market_slug=m.market_slug,
                shares=n, up_price=up.get("best_bid"),
                dn_price=dn.get("best_bid"), cost_basis=pt["cost_basis"],
                proceeds=pt["proceeds"], fee=pt["fee"],
                realized_pnl=pt["realized_pnl"])
            log.info("CLOSE %-28s %.0f pairs realized $%+.2f | %s",
                     st.title[:28], n, pt["realized_pnl"], pt["why"])
    except Exception as e:
        log.warning("profit_take failed on %s: %s: %s",
                    st.title[:30], type(e).__name__, e)
        pt = {"take": False, "why": f"error: {e}"}
```

- [ ] **Step 3: Surface it in `_live`**

In the `st.spec["_live"] = {...}` dict, add immediately after the
`"markout_n": st.markout.get("n", 0),` line:

```python
        "close_why": pt.get("why", ""),
```

- [ ] **Step 4: Make inventory rehydration close-aware**

`_inventory_from_db` rebuilds `Inventory` by summing the `fills` table, which
knows nothing about closes — so after a restart every closed position comes
back and the mechanism silently undoes itself. This is the same class of bug
as the inventory-rehydration one already fixed in this file.

In `_inventory_from_db`, inside the existing `with store.db() as c:` block,
after the `for side, size, price in c.execute(...)` loop and still inside the
`with`, add:

```python
            for shares, cost_basis in c.execute(
                    "SELECT shares, cost_basis FROM closes WHERE condition_id=?",
                    (cid,)):
                # A close removed one UP and one DOWN share per pair, at the
                # combined basis recorded on the row. Split that basis back
                # across the two legs in the proportion the legs are currently
                # held in; with nothing held, fall back to an even split.
                n = shares or 0.0
                total_sh = inv.up_shares + inv.down_shares
                frac = (inv.up_shares / total_sh) if total_sh > 0 else 0.5
                inv.up_shares -= n
                inv.down_shares -= n
                inv.up_cost -= (cost_basis or 0.0) * frac
                inv.down_cost -= (cost_basis or 0.0) * (1.0 - frac)
```

The surrounding `try/except Exception` already present in the function covers
this query too, so a database without the `closes` table degrades to the old
behaviour rather than stopping the fleet.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: all pass — the 80 pre-existing tests plus the 6 new ones.

---

### Task 5: Supervisor

**Files:**
- Create: `strategy/supervisor.py`
- Test: `tests/test_supervisor.py`

**Interfaces:**
- Produces: `next_restart_delay(consecutive_crashes: int) -> float`, `main() -> None`.
- Consumes: nothing from the strategy package. It only spawns processes.

- [ ] **Step 1: Write the failing test**

Create `tests/test_supervisor.py`:

```python
import pytest

from strategy.supervisor import next_restart_delay


def test_first_crash_restarts_promptly():
    assert next_restart_delay(1) == pytest.approx(5.0)


def test_repeat_crashes_back_off():
    assert next_restart_delay(3) > next_restart_delay(1)


def test_backoff_is_capped():
    assert next_restart_delay(99) == pytest.approx(60.0)


def test_a_recovered_child_starts_from_the_bottom_again():
    # The caller resets the counter to 0 once a child survives; the delay in
    # that state must not exceed the first-crash delay.
    assert next_restart_delay(0) <= next_restart_delay(1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_supervisor.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'strategy.supervisor'`

- [ ] **Step 3: Implement**

Create `strategy/supervisor.py`:

```python
"""Keep the fleet running when nobody is watching.

On 2026-07-29 the fleet died of a ZeroDivisionError at 13:40 and nobody
noticed for three and a half hours. Everything downstream of that -- the
markout samples, the reward samples, the whole measurement the run exists to
produce -- was simply not collected. A strategy running on a process that
dies silently is worth nothing, however good the strategy is.

This owns both processes and restarts either one when it exits. It does NOT
survive a reboot: that needs Task Scheduler, and is a separate decision.

    set MAKER_DB=run/fleet.db
    python -m strategy.supervisor
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
(ROOT / "logs").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(ROOT / "logs" / "supervisor.log",
                                  encoding="utf-8"),
              logging.StreamHandler()])
log = logging.getLogger("supervisor")

# How long a child must stay up before its next death stops counting as part
# of the same crash loop.
STABLE_SEC = 60.0
POLL_SEC = 2.0

CHILDREN = {
    "fleet": [sys.executable, "-m", "strategy.fleet"],
    "dash": [sys.executable, "-m", "uvicorn", "server.fleet_dash:app",
             "--host", "127.0.0.1", "--port", "8800"],
}


def next_restart_delay(consecutive_crashes: int) -> float:
    """Seconds to wait before restarting a child.

    Flat 5s for an isolated crash -- that is the common case, and the fleet
    should be back before the next sweep. Doubling past that so a child that
    dies on startup (a bad markets.json, a port already bound) does not spin
    the CPU rewriting the same traceback forever. Capped at a minute so an
    outage that lasted hours is still recovered from quickly.
    """
    if consecutive_crashes <= 1:
        return 5.0
    return min(60.0, 5.0 * (2 ** (consecutive_crashes - 1)))


class Child:
    def __init__(self, name: str, cmd: list[str]):
        self.name = name
        self.cmd = cmd
        self.proc: subprocess.Popen | None = None
        self.crashes = 0
        self.started = 0.0
        self.restart_at = 0.0

    def start(self) -> None:
        # stdout/stderr are inherited: each child already writes its own log
        # file, and inheriting means a traceback still reaches the console the
        # supervisor runs in instead of vanishing into a pipe nobody reads.
        self.proc = subprocess.Popen(self.cmd, cwd=str(ROOT))
        self.started = time.time()
        self.restart_at = 0.0
        log.info("started %s (pid %d)", self.name, self.proc.pid)

    def check(self, now: float) -> None:
        if self.proc is None:
            if now >= self.restart_at:
                self.start()
            return
        code = self.proc.poll()
        if code is None:
            # Alive. A child that has been up a while is not in a crash loop.
            if self.crashes and (now - self.started) >= STABLE_SEC:
                log.info("%s stable, clearing crash count", self.name)
                self.crashes = 0
            return
        self.crashes += 1
        delay = next_restart_delay(self.crashes)
        log.error("%s EXITED code=%s after %.0fs (crash #%d) -- restarting "
                  "in %.0fs", self.name, code, now - self.started,
                  self.crashes, delay)
        self.proc = None
        self.restart_at = now + delay

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        log.info("stopped %s", self.name)


def main() -> None:
    if not os.environ.get("MAKER_DB"):
        raise SystemExit("MAKER_DB is not set -- the children would write to a "
                         "different database than the one you are reading. "
                         "Set it (e.g. run/fleet.db) and try again.")
    children = [Child(n, c) for n, c in CHILDREN.items()]
    for ch in children:
        ch.start()
    log.info("supervising %d children | MAKER_DB=%s",
             len(children), os.environ["MAKER_DB"])
    try:
        while True:
            now = time.time()
            for ch in children:
                ch.check(now)
            time.sleep(POLL_SEC)
    except KeyboardInterrupt:
        log.info("interrupted -- stopping children")
    finally:
        for ch in children:
            ch.stop()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_supervisor.py -v`
Expected: 4 passed

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: all pass

---

### Task 6: DEMO run

**Files:** none modified. This is the verification the whole plan exists for.

Everything below is PAPER/DEMO: real books are pulled from the venue, orders
and fills are simulated, and no order is ever sent.

- [ ] **Step 1: Stop anything already running**

Run: `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'strategy.fleet|fleet_dash' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }`
Expected: no output and no error. If nothing matches, nothing happens — that is fine.

- [ ] **Step 2: Start the supervisor in the background**

Run (PowerShell):
```bash
$env:MAKER_DB='run/fleet.db'; python -m strategy.supervisor
```
Expected in `logs/supervisor.log`: a `started fleet` line and a `started dash` line.

- [ ] **Step 3: Confirm both children are alive and the dashboard answers**

Run: `Invoke-RestMethod http://127.0.0.1:8800/api/fleet | ConvertTo-Json -Depth 2 | Select-Object -First 20`
Expected: JSON describing the fleet, not a connection error.

- [ ] **Step 4: Prove the supervisor actually restarts**

Kill only the fleet child, then read the log:

Run: `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'strategy.fleet' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }`
Then wait ~10s and run: `Get-Content logs/supervisor.log -Tail 5`
Expected: an `EXITED` line followed by a `started fleet` line. This is the
whole point of the task — if the restart does not appear, the supervisor is
decorative.

- [ ] **Step 5: Confirm the closes table starts empty**

Run:
```bash
$env:MAKER_DB='run/fleet.db'; python -c "
from strategy import store
with store.db() as c:
    print(list(c.execute('SELECT COUNT(*) FROM closes')))
"
```
Expected: `[(0,)]` on a fresh run. A non-zero count later is the profit-taking
mechanism firing, which is the result being looked for.

- [ ] **Step 6: Let it run, then read the outcome**

After the run has been up long enough to have taken fills, read both tables:

```bash
$env:MAKER_DB='run/fleet.db'; python -c "
from strategy import store
with store.db() as c:
    print('fills  ', c.execute('SELECT COUNT(*) FROM fills').fetchone()[0])
    print('closes ', c.execute('SELECT COUNT(*), COALESCE(SUM(realized_pnl),0) FROM closes').fetchone())
"
```
Expected: a fill count, and a close count with a summed realized P&L. Both are
DEMO figures — simulated fills against real books.

---

## Self-Review

**Spec coverage:** Component A trigger → Tasks 1, 2. Component A storage → Task 3. Component A wiring → Task 4. Component B → Task 5. DEMO run → Task 6. The spec's explicitly deferred items (dashboard tile, live execution) are correctly absent.

**Placeholder scan:** none — every step carries runnable code or an exact command.

**Type consistency:** `should_close` returns exactly the keys Task 4 reads (`take`, `shares`, `cost_basis`, `proceeds`, `fee`, `realized_pnl`, `why`). `store.log_close` takes exactly the keyword names Task 4 passes. `next_restart_delay` takes an int and returns a float in both the test and the implementation.

**Correction against the spec:** the spec wrote the fee as `0.017` per pair and the trigger as `gross >= 0.037`. The fee is charged per LEG and a close sells two legs, so it is `0.034` per pair and the gross move required is `0.054`. The user's stated threshold — 2c net after fees — is preserved exactly; only the gross figure changes. This plan is the authority on that number.

**Known deviation from spec:** the spec did not mention `_inventory_from_db`. Task 4 Step 4 makes it close-aware, because without that a restart resurrects every closed position out of the `fills` table and the mechanism silently undoes itself.
