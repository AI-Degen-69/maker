# Final fix report -- profit-take code review follow-up

## Fix 1 -- depth-aware should_close

Files: `strategy/profit_take.py`, `strategy/fleet.py` (call site), `tests/test_profit_take.py`.

`should_close(inv, up_bids, dn_bids, cfg)` now takes the full bid LADDER for
each side (`{price: size}`, the same shape `full_book()` in `strategy/main.py`
returns as `book["bids"]`), not a single top-of-book price.

Design decision: **walk the ladder** rather than price everything at the top
tick. Reasoning documented in the module docstring: pricing a 600-pair close
at a 50-share top bid is fiction the same way top-of-book was before; walking
is the honest number, and because deeper levels are worse, the achieved
average price falls as size grows -- so the profitability test is run on the
ACHIEVED average for the size actually closed, not the top tick. This was
pinned in a new test (`test_walked_average_price_can_fail_where_top_of_book_would_pass`)
where the top level alone clears the 2.0c threshold but the size-weighted
average across two levels does not, and the close is correctly rejected.

Sellable quantity = `min(paired, depth_up, depth_dn)` -- a pair needs one UP
AND one DOWN sold, so the tighter leg caps it. Zero sellable (e.g. an empty
ladder on one leg) returns the existing "no" shape with an explanatory `why`.
A partial close (book absorbs less than `paired`) is treated as the intended
behaviour: `shares` is the actually-closable quantity, and `cost_basis` /
`proceeds` / `fee` / `realized_pnl` are all computed on that same quantity.

`strategy/fleet.py`'s call was updated from `up.get("best_bid")` /
`dn.get("best_bid")` to `up.get("bids")` / `dn.get("bids")`. The
cost-before-shares mutation ordering in the caller (`avg()` divides by share
count) is preserved.

`tests/test_profit_take.py`: existing tests converted to a `_ladder(price,
size=1000)` helper (deep enough to never cap them). New tests added:
- `test_zero_depth_on_one_leg_closes_nothing` -- empty ladder on one leg closes 0.
- `test_thin_book_closes_only_the_sellable_part` -- 600 paired vs a 50-share
  bid closes exactly 50, with cost/proceeds/fee computed on 50.
- `test_walked_average_price_can_fail_where_top_of_book_would_pass` -- pins
  the walk-the-ladder decision (see above).
- `test_forgone_vs_settlement_is_hold_value_minus_realized` -- pins Fix 2.

## Fix 2 -- forgone_vs_settlement

Files: `strategy/profit_take.py`, `strategy/store.py`, `strategy/fleet.py`.

Added `forgone_vs_settlement REAL` to the `closes` schema in `SCHEMA`, added
it to the `_MIGRATIONS["closes"]` entry (alongside the existing
`up_cost_removed`/`dn_cost_removed`) so an existing `run/fleet.db` gains the
column via `ALTER TABLE` instead of the INSERT failing, added the parameter
to `log_close`, and computed it in `should_close` as
`(1.00 - cost_per_share) * shares - realized_pnl` -- what holding to
settlement would have netted minus what closing actually netted. A comment
at the computation site explains why it is recorded: the close is justified
by capital velocity (money freed ~1.5 years early to earn daily rent
elsewhere), not by nominal value, and without this number a reader cannot
check that claim. `strategy/fleet.py` passes `pt["forgone_vs_settlement"]`
through to `store.log_close` unchanged.

## Fix 3 -- dashboard closes-awareness

File: `server/fleet_dash.py`.

- `_db_stats()`: added a `closes` aggregate query (count, summed shares,
  summed `realized_pnl`, summed `forgone_vs_settlement`) per market, alongside
  the existing `fills` aggregate.
- `_realized()`: for each resolved market, subtracts closed shares from the
  winning token's fill count before applying the $1 resolution credit
  (resolution now only pays for shares still held), backs the already-removed
  `cost_basis` out of the cost total (or it would be charged twice: once
  inside the close's own `realized_pnl`, again against a payout those shares
  no longer collect), and adds the close's `realized_pnl` into the market's
  P&L. Closes on markets that have not yet resolved are folded into the
  fleet-wide `realized` total and counts too, since that money is already
  booked independent of resolution.
- `/api/fleet`: each row now carries `closes`, `closed_pnl`,
  `closed_forgone`, and `close_why` (read from `_live["close_why"]`, which
  `strategy/fleet.py` already wrote but nothing read). Totals carry
  `closes`, `closed_pnl`, `closed_forgone`.
- UI: added an `EARLY CLOSES (SIM)` tile next to `REALIZED P&L`, both
  following the existing tile convention (label/value/subtitle/color class).
  The tile and its subtitle are explicit that this is simulated -- "(SIM)" in
  the label, "never a real payout" in the surrounding comment -- consistent
  with the page's existing `PAPER - NO REAL ORDERS` chip and its convention
  of never presenting a projection as a settled figure. The per-market
  POSITION column now appends a line showing close count/booked pnl/forgone
  when `m.closes > 0`, and the latest `close_why` string when present, so an
  operator watching positions shrink sees why.

`tests/test_dashboard_page.py` was not modified (it re-parses `PAGE`/`FLEET_PAGE`
as JS and checks for duplicate top-level `const`s -- no natural place to pin
dashboard *data* behaviour, only that the page still parses). It still
passes: no duplicate consts were introduced.

## Fix 4 -- write-then-mutate ordering

File: `strategy/fleet.py`, close block inside `visit()`.

`store.log_close(...)` now runs BEFORE the four `st.inv.*` mutation lines
(previously after). `up_removed`/`dn_removed` are still captured before any
mutation (needed by both the log call and the mutations, and `avg()` divides
by the pre-mutation share count). If `log_close` throws, `st.inv` is
untouched and the position still matches what the DB says -- consistent with
`_inventory_from_db`'s invariant that the `closes` table is authoritative.

## Verification

```
python -m pytest tests/ -q
```

```
........................................................................ [ 75%]
.......................                                                  [100%]
95 passed in 5.14s
```

91 before, 95 after (4 new tests added in Fix 1, all passing; no existing
test was deleted or weakened).

## Noticed but deliberately not fixed

- `_walk()` in `profit_take.py` sorts the whole bids dict on every call
  (O(n log n) per should_close invocation). Books here are thin (a handful of
  levels), so this is not a real cost, but it's a spot a hot loop could
  revisit later.
- `_realized()`'s treatment of unresolved-market closes assumes
  `cost_basis` on a `closes` row equals `up_cost_removed + dn_cost_removed`
  exactly, which is true by construction in `strategy/fleet.py`'s current
  call site but is not independently enforced by the schema. Not touched --
  out of scope for these four fixes.
- Did not touch `_markout_stats()` or anything else in `fleet_dash.py`
  outside `_realized()`, `_db_stats()`, and the UI additions requested.
- Did not start the fleet, dashboard, or supervisor, per instructions.

## Follow-up fix

Scoped re-review finding: `closes` rows no longer self-reconciled after Fix 1
started walking the ladder. `proceeds` was based on the achieved average
price, but `strategy/fleet.py` still logged `up_price=up.get("best_bid")` /
`dn_price=dn.get("best_bid")` -- top-of-book -- so a partial close could
record a price column that contradicted the proceeds column on the one table
that claims to book realized money.

Fix: `should_close` in `strategy/profit_take.py` now exposes the achieved
per-leg average sale price it already computed while walking each ladder, as
new keys `up_avg_price` / `dn_avg_price` on the returned dict (and on the
`NO` / `_no(...)` shape, both defaulted to `0.0`, so a caller reading them
unconditionally cannot hit a `KeyError`). `_walk()` was changed to return
`(proceeds, avg_price)` instead of just `proceeds`, computed from the same
walk rather than recomputed. `strategy/fleet.py`'s `store.log_close` call now
passes `pt["up_avg_price"]` / `pt["dn_avg_price"]` for `up_price` / `dn_price`
instead of `up.get("best_bid")` / `dn.get("best_bid")`. No schema change: the
`up_price`/`dn_price` column names are unchanged, only their comment in
`strategy/store.py` was updated to say they hold the size-weighted average
achieved across the levels consumed, and why that is the honest figure to
store next to `proceeds`.

Added `test_proceeds_equals_shares_times_achieved_average_price` to
`tests/test_profit_take.py`: a partial close (300 paired, 200 sellable) that
walks a second, worse UP level so top-of-book (0.60) and the achieved average
(0.575) genuinely differ, then asserts
`proceeds == pytest.approx(shares * (up_avg_price + dn_avg_price))` directly
-- the exact property that had broken.

```
python -m pytest tests/ -q
```
```
........................................................................ [ 75%]
........................                                                 [100%]
96 passed in 2.25s
```

95 before this follow-up, 96 after (one new test, all passing).
