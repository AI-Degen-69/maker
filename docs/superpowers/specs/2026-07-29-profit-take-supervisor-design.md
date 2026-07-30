# Profit-Taking + Supervisor — Design

**Goal:** Close paired positions early when the market has moved enough to
lock in profit beyond the taker fee, converting locked capital back into
working capital. Separately, keep both fleet processes running unattended and
recover automatically from crashes.

**Architecture:** Two independent units, same shape as `strategy/gate.py` and
`strategy/allocate.py` — pure decision logic with no I/O, wired into
`strategy/fleet.py` at the call site. The supervisor is a new top-level
process, `strategy/supervisor.py`, that owns two subprocesses.

---

## Component A: Profit-taking

**Scope:** Applies only to PAIRED shares (`min(up_shares, down_shares)`).
Naked (unhedged) shares are left to the existing skew/cap machinery — this
mechanism does not touch directional risk, only the already-hedged, already
profitable-at-settlement portion of a position.

**Trigger:**
```
paired = min(inv.up_shares, inv.down_shares)
cost_per_share = inv.avg("UP") + inv.avg("DOWN")
exit_value_per_share = up_bid + dn_bid      # selling both legs now, taker side
gross_gain = exit_value_per_share - cost_per_share
net_gain = gross_gain - cfg.profit_take_fee_per_share
take = net_gain >= cfg.profit_take_net_threshold
```

Config (new fields on `MakerConfig`):
- `profit_take_fee_per_share: float = 0.017` — taker fee assumption per leg sold.
- `profit_take_net_threshold: float = 0.02` — net profit required beyond fee, per share, before closing. Combined: trigger needs `gross_gain >= 0.037`.

**`strategy/profit_take.py`** — pure, no I/O:
```python
def should_close(inv, up_bid: float, dn_bid: float, cfg) -> dict:
    """Returns {'take': bool, 'shares': float, 'proceeds': float,
    'fee': float, 'realized_pnl': float}. Never mutates inv."""
```
- Returns `take: False` if either bid is missing, or `paired <= 0`.
- Pure arithmetic only — the caller applies the result.

**Wiring into `strategy/fleet.py:visit()`:** after book fetch and fill
processing, before the requote. On `take=True`:
1. Subtract `shares` from both `inv.up_shares`/`down_shares` and their `_cost`
   fields proportionally (average cost per remaining share unchanged).
2. `store.log_close(...)` — new table, one row per close.
3. `log.info("CLOSE %-28s %.0fsh realized $%.2f", ...)` — same shape as the
   existing `FILL` log line.
4. Wrapped in try/except, matching the existing `reallocate` guard — a bug
   here must degrade to "skip this cycle's close check," never kill the
   fleet.

**Storage — new table in `strategy/store.py`:**
```sql
CREATE TABLE IF NOT EXISTS closes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    condition_id TEXT,
    market_slug TEXT,
    shares REAL,
    up_price REAL, dn_price REAL,
    cost_basis REAL,
    proceeds REAL,
    fee REAL,
    realized_pnl REAL
);
```
`log_close(**kw) -> None` writer, following the existing `log_fill` pattern.

**Not in scope (deferred):** dashboard surfacing of `closes`/realized P&L.
The mechanism is fully functional and logged without it; adding a dashboard
tile is a follow-up once the DEMO run shows it firing.

**Testing — `tests/test_profit_take.py`:** pure-function tests on
`should_close`, mirroring `tests/test_gate.py`'s style: below-threshold does
not trigger, above-threshold triggers with correct `realized_pnl`, missing
bid returns `take=False`, zero paired shares returns `take=False`.

---

## Component B: Supervisor

**Scope:** Watches both `python -m strategy.fleet` and
`python -m uvicorn server.fleet_dash:app --host 127.0.0.1 --port 8800` as
subprocesses. Restarts either one on crash. Does NOT survive a machine
reboot (that needs Task Scheduler / a Windows service — not requested, out of
scope).

**`strategy/supervisor.py`:**
- Launches both children via `subprocess.Popen`, inheriting `MAKER_DB` from
  the environment (required — supervisor exits early with a clear message if
  unset, same contract `fleet.py` already has via `load_specs()`).
- Poll loop (~2s): for each child, if `proc.poll() is not None`, that child
  crashed — log to `logs/supervisor.log` with timestamp, exit code, and which
  child, then restart it after a delay.
- Restart delay is a pure, testable function:
  ```python
  def next_restart_delay(consecutive_crashes: int) -> float:
      """5s normally; escalates only on rapid repeat crashes so a genuine
      crash loop doesn't spin the CPU, capped at 60s."""
  ```
  `consecutive_crashes` resets to 0 once a child has stayed up >= 60s.
- Ctrl+C (SIGINT/KeyboardInterrupt) terminates both children cleanly before
  exiting.
- Each child's own stdout/stderr keeps going to its own existing log file
  (`logs/fleet.log`, dashboard's own logging) — the supervisor does not
  duplicate that, it only logs its own crash/restart events.

**Testing — `tests/test_supervisor.py`:** only `next_restart_delay` is unit
tested (pure function). The subprocess-management loop itself is glue code,
untested by convention — `fleet.main()`'s own `while True` loop is likewise
not unit tested in this codebase.

---

## Self-Review

**Placeholders:** none.
**Consistency:** profit-taking touches only paired shares, consistent with
the "we do not want fills" strategy framing in HANDOFF.md — this is the
mechanism that lets locked-until-2027 capital become working capital again,
exactly as described there.
**Scope:** two independent, small units; each testable in isolation; no
dashboard changes, no live-execution wiring (both explicitly deferred).
**Ambiguity check:** "moved enough" is now a single numeric inequality
(`gross_gain >= 0.037`), not a vague description.
