# Research Log — Maker (Polymarket BTC 5-min)

Running lab notebook. Newest entries at the bottom. Each entry:
**Question → Method → Result → Verdict**. Negative results are kept, not
deleted; most of what we have learned came from things that did not work.

**Conventions**
- Numbers here are measured, not estimated. If a figure is an estimate it says so.
- "Verdict" is a decision, not a summary: DEAD / PARKED / LIVE / OPEN.
- Instrumentation bugs get their own entries. On this project they have
  repeatedly been the difference between a real finding and a fake one.

The taker strategy lives in a separate repo (`polymarket-taker`) with its own
log. This file covers the maker only.

---

## Session 1 — 2026-07-21

**Context.** The taker strategy buys near-certainties at 0.80–0.99 and pays a
fee on every fill. A trader named @powerwinner showed a smooth upward equity
curve on the same markets, so the question was whether he does the same thing
better — or something else entirely.

### Is @powerwinner running our strategy?

**Method.** Pulled 56,768 of his BTC/ETH 5-min fills over 2026-07-14→21
(2,970 markets) and joined them to gamma resolutions.

**Result.** The opposite of ours on every axis.

| | powerwinner | our taker |
|---|---|---|
| entry price | 0.30–0.70 (54% of volume) | 0.80–0.99 |
| entry timing | 57% in the FIRST 40% of the window | last 40% only |
| trades at 0.98+ | zero | biggest size tier |
| market win rate | 41.4% (needs 56.1%) | ~92% |

His per-bucket win rates track breakeven within 1–3 points, mostly below it.
On direction alone he loses.

**Verdict.** DEAD — he is not a predictor, so copying his entries as a taker
would copy a losing bet.

### Then where does his money come from?

**Method.** Decomposed gross P&L against redeem records (ground truth) and
against the taker fee curve.

**Result.**

```
gross (payout − cost)              +$39,884 / week   ≈ +$171k/month
same trades charged our taker fee  −$32,501 / week
reported profile P&L                            +$182,797 / month
```

The entire difference between profit and loss is whether he pays taker fees.
He does not — he rests limit orders. Confirmations: his volume concentrates
where `fee ∝ p(1−p)` peaks (0.30–0.70), he enters early (passive orders need
time to be hit), and he never touches 0.98+.

An earlier hypothesis that his "buy both sides" behaviour was locked
arbitrage was **tested and rejected**: the pair costs 0.9990 against a $1.00
payout (0.10% margin) and is favourable only 51.1% of the time — a coin flip.
Spread capture, not arbitrage, is ~68% of his gross.

**Verdict.** LIVE — the mechanism is *rest, do not cross*. Built a maker sim
to test it.

### An earlier analysis pass that was wrong, and why

**Method.** First P&L join reported him at −$23,828.

**Result.** Resolution coverage was 59.1% for BTC versus 98.2% for ETH — 809
fetch failures from rate-limiting, dropped non-randomly. After recovering
809/810, computed payout matched his actual on-chain redeems to 0.1%
($2,745,682 vs $2,748,422).

**Verdict.** DEAD (fixed). Recorded because the wrong number looked entirely
plausible and contradicted the source of truth.

### How do you honestly simulate getting filled?

**Method.** First attempt keyed fills off the trade tape's `side` field: a
taker SELL lifts a resting bid.

**Result.** 194 of 200 tape rows are "BUY" — data-api reports each
*participant's* own side, not the aggressor's, so a maker's own fills appear as
BUYs. Aggressor direction is unrecoverable from that feed, and a SELL-only rule
would report almost no fills and wrongly kill the strategy.

Rebuilt on **book deltas**: the size resting at our price level is directly
observable, and its decrease is exactly the queue moving. Verified live —
levels move materially every few seconds (60 → 0 in 6s). Optimistic biases
(cancels counted as queue progress; assumed price-time priority) are documented
in `strategy/fills.py` rather than hidden; output is an upper bound.

**Verdict.** LIVE — seven unit tests cover queue precedence, sweeps, and
overfill.

### What actually drives maker P&L?

**Method.** 44 settled markets, grouped by inventory balance (min/max of the
two legs; 1.00 = perfectly hedged).

**Result.**

| balance | n | avg P&L | swing |
|---|---|---|---|
| 1.00 perfect | 12 | +$30.70 | 9.8 |
| 0.85–0.99 | 2 | +$20.31 | 6.4 |
| 0.60–0.85 | 17 | −$10.94 | 55.4 |
| <0.60 | 13 | −$50.95 | 7.3 |

Monotonic. Hedged markets totalled +$409; unbalanced −$848 — that gap is the
run's −$389. The tiny swing on the unbalanced group is the important part:
they lose *consistently*, not randomly. Our fills are adversely selected, so we
end up systematically heavy on the side that loses.

Two guards were blocking the balancing trade: the per-market cost cap returned
no quotes at all (logged ×1043), and the pair-cost cap rejected the hedge
whenever `avg(other) + price` reached $1.00.

**Verdict.** LIVE — both caps now apply only when adding to the heavy side.

### Regression introduced by that fix — OPEN

**Method.** Post-migration run in the new repo, 12 fills.

**Result.** Pair costs came out **above** $1.00 (1.0900, 1.0650, 1.0275,
1.0167) for something that pays exactly $1.00 — a guaranteed loss on the
hedged portion. Pre-fix runs were 0.93–0.96. Cause: allowing the balancing side
past the pair cap means that when both sides' asks are wide, buying both at
ask−1tick can sum above 1.00.

**Verdict.** OPEN — not fixed during the repo migration (structural work only).
Needs its own change and a fresh data run. Candidate fix: cap the hedge at a
price where the resulting pair stays under 1.00, and skip the hedge if no such
price exists.

### Instrumentation bugs found

- Four `maker.main` processes once ran concurrently against one database. Each
  keeps its own in-memory inventory, so the DB held the *sum* of several
  independent strategies — silently invalid data that still looked plausible.
  Added a single-instance pid guard; that run was archived as contaminated.
- Decision-log run-collapsing keyed on `reason`, whose text embeds live values
  (`t_remaining 4s`), so it collapsed almost nothing: 2.0× versus the taker's
  15×, 17,490 rows/day. Keying on `(market, action, side)` and logging once per
  cycle rather than per side took it to 7.8×.

**Verdict.** Both DEAD (fixed).

### Sample size reality

With σ ≈ 60× the per-market edge, the honest target is thousands of settled
markets, not dozens. The dashboard shows live progress toward 90/95/99%
confidence so this cannot be quietly forgotten.

**Verdict.** OPEN.

### Session 2 — 2026-07-22 (afternoon): the decisive experiment is now running

**Context.** The n=53 run measured a *contaminated* config (the hedge exemption let pairs run above $1.00), so it proved "the bug loses" and nothing about the maker mechanism. The single number that actually decides saveable-vs-dead — **how often do these 5-min BTC markets offer a fillable sub-$1.00 hedged pair at ask−1tick?** — had never been measured. This session fixes the bug and runs a fresh, instrumented experiment to measure exactly that.

**Method.**
1. *Fix the pair-cost cap.* In `strategy/quotes.py` the hedge exemption was removed: the cap `max_pair_cost = 0.995` now applies to **every** side (hedge or not). Additionally a fresh-market guard blocks opening *any* position unless BOTH sides can be filled at ask−1tick into a pair strictly under $1.00 — the bot now sits out wide markets instead of taking a guaranteed-loss directional leg.
2. *Add the decisive census.* `strategy/store.py` got a `hedge_census` table (one row per distinct market): was a fillable sub-$1.00 pair available at touch? `strategy/main.py` records it every cycle; `strategy/kpi.py` aggregates the fillable rate and median pair-at-touch.
3. *Define where the experiment ends.* `strategy/config.py` adds the stop condition:
   - **Phase A — Census:** observe `experiment_census_markets = 60` distinct markets. If the fillable-sub-$1.00 rate is `< hedge_fillable_min_rate = 0.50`, the instrument *cannot* be made profitably → **stop, DEAD**.
   - **Phase B — Verdict:** once census passes, settle `experiment_verdict_markets = 120` markets under the corrected strategy and read P&L sign + confidence → final save/lost call.
   The dashboard renders both the phase banner and the census progress live, so the run ends on evidence, not vibes.
4. Fresh DB, single instance, bot + dashboard started locally (venv rebuilt — a Hermes `PYTHONPATH` leak had been shadowing the project venv, so deps installed into the wrong site-packages; fixed with `env -u PYTHONPATH`).

**Result.** (Live, as started — n=1 so far, will replace with measured totals as it settles.)
- Bot running, single instance, fresh `maker.db`.
- Census immediately recorded its first market as **fillable** with `median_pair_at_touch = 0.99` (under $1.00) — i.e. *this* market is makeable, contradicting the contaminated run's 4% figure. That is exactly the hypothesis under test.
- Pair cost now cannot exceed $1.00 by construction; the −4.2%-on-hedge loss mode is structurally closed.

**Verdict.** LIVE — the experiment is the decider, not this commit. Two outcomes are now possible and each is actionable:
- Census fillable rate `< 50%` → **DEAD**: the 5-min BTC series does not offer enough sub-$1.00 pairs at a fillable price; the mechanism cannot transfer from powerwinner.
- Census passes and Phase B P&L is positive with confidence → **the strategy is SAVEABLE** (though still below powerwinner's volume-driven edge).
A third outcome — census passes but Phase B loses — would point at the optimistic fill model (documented bias: cancels counted as fills), not the pair cost.

### Instrumentation bug found this session

- **Venv shadowing via `PYTHONPATH` leak.** The project's `.venv` was created, but because Hermes's `PYTHONPATH` was exported into the session, `pip install` and `python` resolved packages against Hermes's site-packages. `requests`/`uvicorn` import-failed at runtime while `pip` reported "already satisfied". Cured by launching every run with `env -u PYTHONPATH ./.venv/Scripts/python.exe ...`. General lesson for this Windows host: always unset `PYTHONPATH` before invoking a project venv.

**Verdict.** DEAD (fixed).

### Sample size reality

With σ ≈ 60× the per-market edge, the honest target is thousands of settled markets, not dozens. The dashboard shows live progress toward 90/95/99% confidence so this cannot be quietly forgotten.

**Verdict.** OPEN.

### Session 3 — 2026-07-22 (night): balance enforcement built, clean re-run

**Context.** A pure "sit out unless both sides fillable at entry" guard was rejected — the loss is a *partial fill*, not an entry decision. One side rests and fills, the other never does before the 5-min window closes → market settles one-sided and eats the full resolution loss. A passive maker cannot UN-fill an already-rested side, so the only enforceable balance rule is to **cross the spread for the missing leg near resolution**.

**Method.** In `strategy/main.py`, when `t_remaining ≤ balance_hedge_sec (20s)` and inventory balance `< target_balance (0.92)`, the bot cancels resting quotes and posts a *crossing* buy of exactly the missing shares to match the held leg. The fill is tagged `crossed=1` so `kpi`/`settlements` can distinguish a settlement hedge from a maker fill. `store` is self-healing (adds the `crossed` column to older DBs). Dashboard gained a **HEDGE X** card (count of settlement crossings). `config.balance_hedge_sec` documents the rationale.

**Result (design, pre-run).** Every settled market now settles *balanced by construction*. So when Phase B concludes, win/loss is an unambiguous verdict on the **instrument** — not on our execution. This converts the previously confounded result (was it imbalance or the instrument?) into an interpretable one.

**Re-run hygiene.** Per AGENTS.md a code change invalidates the sample, so the partial-DB runs were archived (`maker.db.archived_20260722_225907/230650/235454/235939`) and `maker.db` wiped. Bot + dashboard relaunched fresh on the new code with `env -u PYTHONPATH` (the Hermes venv-leak fix from Session 2 still required). Verified: equity $5,000, realized $0, hedges 0, Phase A_CENSUS, census median pair 0.995 (cap fix holding).

**Verdict.** LIVE — the run is the decider. Two outcomes, both actionable:
- Census `fillable_rate < 0.50` at 60 markets → **DEAD** (instrument doesn't fit these markets).
- Census passes AND Phase B (120 balanced markets) is positive with confidence → **SAVEABLE**.
- The HEDGE X count is now the leading indicator: if it climbs, partial-fill was the driver and we're fixing it; if it stays ~0, the books were already balancing and the loss is structural.

### Instrumentation bug found this session

- **`kpi.report()` scope error.** The `balance_hedges` query referenced a bare `c` cursor outside its `with db()` block → dashboard returned `{"error": "cannot access local variable 'c'"}` (HTTP 200, so the failure was silent in the UI). Fixed by routing through the existing `_rows()` helper. Lesson: a 200-with-error JSON is a real failure mode here; always probe the parsed state dict, not just the HTTP code.
- **MSYS PID vs native PID mismatch.** `ps`/`kill` in git-bash show translated PIDs that `taskkill` rejects ("not found"). The reliable kill path on this host is `powershell Get-CimInstance Win32_Process` to get the *native* PID, then `taskkill /F /PID <native>`. The port listener PID from `netstat -ano` (column 5) is also native and directly killable.

**Verdict.** DEAD (fixed).

### Session 4 — 2026-07-28: the fill rate, measured against the trade tape

**Question.** With the phantom-fill bug fixed, what is the real fill rate? Every
maker number recorded before the fix came from an engine that granted a full
fill to any order resting above the best bid, so "37.6% against real queue
depth" is void and the question was open.

**Method.** Two new scripts, so the answer is reproducible rather than a
one-off observation. `scripts/record_books.py` polls the live BTC 5-min market
(~1.7s/poll, both outcomes, full depth) into a standalone `books.db`.
`scripts/measure_fill_rate.py` replays those books through the fixed engine.
Raw books are strategy-independent, so every quoting rule is compared on the
SAME market data instead of on different hours.

An EPISODE — one resting order, held until repriced, cancelled, or the window
closes — is the denominator. Repost events are not: an order that sits at 0.52
for two minutes is one attempt to be filled, however many times the code
re-sends it.

**Result — the book-only model was still inventing most of the edge.** On the
first recorded windows it reported a **50% share fill rate**, of which
**100% came from a single branch**: "the level emptied and the best bid fell
below us, so credit our whole remainder". From bid-side deltas alone a mass
CANCELLATION is indistinguishable from a mass TRADE, so that branch is an
assumption, not a measurement — and it was carrying the entire result.

`scripts/fetch_trades.py` backfills the trade tape (~1,800 prints per 5-min
window) from data-api. The tape cannot recover the aggressor (it reports each
participant's own side — the reason `fills.py` abandoned it in Session 1), but
it does not need to: it says directly whether volume printed at our price.
`QueueFillEngine.on_book` now takes an optional `traded` map, giving

    book delta = trades + cancellations   (observed only as the sum)
    tape       = trades                   (measured)

Both advance our queue position; only trades can fill us.

| model | share fill rate |
|---|---|
| book-only (all prior maker numbers) | 50.0% |
| **tape-confirmed** | **3.1%** |

Same 4 windows, same books, same strategy — only the evidence standard changed.
A **16x** overstatement.

The tape also removes the opposite error: resting alone inside the spread makes
no bid-side delta at all, so those fills were previously invisible. Tape volume
at our price is visible either way.

**Verdict.** OPEN, trending bad. 3% is the honest passive fill rate at
ask-1tick. A hedged pair only pays if BOTH legs fill, so pair completion, not
single-leg fill rate, is the number that decides the strategy.

### Session 4 — the balance hedge never worked

**Question.** `main.py` placed the settlement crossing via `engine.post()` at
the best ask — a passive post, not a cross. Does it fill?

**Method.** Direct reproduction: post 150sh at the ask into a book that then
trades straight down through the level.

**Result.** **0 of 150 shares.** A bid resting alone at the ask has nothing
queued at its price, so no bid-side delta can be attributed to it. Before the
phantom-fill fix it filled instantly and completely — which is the only reason
the hedge ever appeared to work. Every market that went unbalanced stayed
unbalanced, and partial fills are the documented main loss driver.

**Fix.** `QueueFillEngine.cross()` models taking: walk real ask depth, at real
prices, in order, stop when the book runs out. A partial cross is a REAL
outcome — if the depth is not there the leg stays unhedged. `max_price` caps
how far up the book the hedge will walk so a thin book cannot drag it to 0.99.

**The fix has a price.** A cross is a TAKER order, and per
`research/market_spec.md` `taker_fee = shares * 0.07 * p * (1-p)`. At p=0.50
that is **1.75c/share against a ~1c edge on a hedged pair** — the fee peaks
exactly where this strategy trades. `kpi.py` charged nothing for it
(`pnl = payout - c  # maker pays no taker fee`), so realized P&L would have
read high once crossing actually worked. It now charges the fee, excludes
crossed shares from the fill-rate numerator, and stops crediting them a maker
rebate.

**Verdict.** OPEN — the hedge is honest now, but it costs more than the edge it
protects. Whether to hedge at all is a live question, not a settled one.

### Session 4 — two instrumentation bugs in this session's own tooling

Both were caught before they produced a "finding", which is the only reason
they are cheap.

- **Replay harness stopped quoting after a complete fill.** A fully-filled
  order is done, not resting, but the harness kept the episode open until the
  price happened to move — silently cutting how much was ever posted. Fixed by
  treating `not order.is_open` as stale.
- **Tape cursor advanced after the loop's `continue` paths.** Would have
  re-credited the same prints to the next interval. Moved to immediately after
  the fill step.

Tests went from 8 to 31 (`test_fills.py`, `test_quotes.py`,
`test_measure_fill_rate.py`). The harness is tested against scripted books with
hand-computed answers BEFORE being pointed at real data — twice on this project
a "finding" turned out to be the measuring code.

**Verdict.** DEAD (fixed).

### Session 4 — powerwinner's two missing rules, and the dashboard

**Price band** (`price_band_low/high`, 0.30-0.70) and **quote timing**
(`quote_window_frac`, first 40% of the window) are now enforced in
`decide_quotes`. Each has its own switch (`enforce_price_band`,
`enforce_quote_window`) so their effects are measured ONE AT A TIME — a
previous run changed two things together and the result could not be read.
`window_frac=None` means "clock unknown" and skips the timing rule rather than
gating every quote on a guess.

**Dashboard** gained the maker metrics it was missing: fill rate vs queue depth
at post, fill PROVENANCE (tape-confirmed vs inferred vs crossed), pair-cost
distribution, quote uptime with the top skip reasons, partial-fill exposure,
taker fees paid, and spread capture per share. `fills.reason` is persisted, so
provenance traces to a row. The schema migration moved out of `log_fill`'s
except branch into an idempotent step at connect time — the old one only ran if
a write failed first.

**Verdict.** LIVE.

### Archive commit — port-8788 single-bot pipeline parked

**Question.** The fleet on port 8800 has rendered the legacy single-market
pipeline on port 8788 (server/dashboard, server/kanban, strategy/main.py's
single-bot loop, scripts/run_fleet.py, deploy/run_service.py) functionally
dead on this host — the supervisor launches fleet + fleet_dash, and nothing
else. Should this code stay on the live branch, or be parked?

**Method.** Moved all five legacy files into `archive/legacy-bot-8788/`
(preserving their original subpaths), and split `strategy/main.py` into:
  (a) the full original, preserved at `archive/legacy-bot-8788/strategy/main.py`,
  (b) a slim shim exposing only `full_book` + `recent_trades` — the two
      functions `strategy.fleet` imports. `tests/test_dashboard_page.py`
      drops the kanban-PAGE check (now archived). `.gitignore` opts the
      `archive/legacy-bot-8788/` subtree back IN while keeping the rest of
      `archive/` (DB snapshots, NEXT_SESSION notes) gitignored runtime
      data.

**Result.** Single commit. `feat/live-readiness` and the new
`archive/legacy-bot-8788` git branch both point at this commit. Legacy
code is git-recoverable from either branch via checkout into the archive
subdirectory. The live fleet pipeline runs unchanged on port 8800.

**Verdict.** PARKED — preserved, not deleted; not running on the live host;
recoverable from the archive branch by checkout.

### Session 5 — 2026-07-31: fresh $1,000 paper fleet and dashboard audit

**Question.** Can the live fleet be restarted with a clean $1,000 simulated wallet while keeping the dashboard's headline numbers honest and easy to scan?

**Method.** Reduced the dashboard to a hierarchy of liquidation P&L, projected reward, wallet commitment, naked risk, realized P&L, and data health. Replaced the hard-coded $800 naked-budget display with config, changed projected return to use total committed capital rather than resting offers alone, added per-market committed exposure, and made the heartbeat use both fleet state and recent DB writes. Added `-FreshRun` startup behavior that archives the prior SQLite database/sidecars and stale state file before launching the supervisor.

**Result.** The previous run remains preserved; the new process is started against a clean `run/fleet.db` with `bankroll_usd = $1,000`, `allocation_budget = $900`, `max_committed_usd = $1,000`, and `max_fleet_naked_usd = $400`. Dashboard parser and focused strategy/dashboard tests pass. Projected reward remains explicitly labeled as a model, while realized P&L remains zero until a close or settlement exists.

**Verdict.** LIVE — clean paper run and dashboard are operational; profitability remains OPEN pending verified fills and realized outcomes.

### Session 6 — 2026-07-31: committed-cap overshoot caught and bounded

**Question.** Does the new $1,000 cap remain true when an allocation changes an existing order size or an emergency hedge crosses the book?

**Method.** Audited the first fresh-run sweep and found $1,036.80 committed despite the $1,000 cap: the allocator sized new quotes, but the live order reservation used a stale pre-visit total and retained old-size orders. Added post-cancellation reservation immediately before resting each order, resized stale orders, and applied the same affordable-notional calculation to emergency crossed hedges.

**Result.** After the guard, a fresh validation run stayed below the cap: the observed committed total fell from $1,036.80 to $367 and then $256 as the fleet reallocated, with no inventory fills. The dashboard exposes both committed total and any over-cap amount instead of hiding a violation.

**Verdict.** LIVE — cap enforcement is now bounded in the paper engine; the fill and realized-P&L experiment remains OPEN.

### Session 7 — 2026-07-31: fleet-wide cap context made explicit

**Question.** Does the hard cap still work when `visit()` is called from the fleet loop rather than a single-market test helper?

**Method.** Reviewed the emergency-hedge and resting-order reservation paths after the cap fix. The affordability calculation was made explicit about its state scope: the fleet runner passes all `MarketState` objects, while direct single-market callers retain a safe one-state default. Recompiled the strategy and reran all tests before restarting the paper sample.

**Result.** The cap calculation no longer depends on an undefined local `states` name, and both crossed hedges and new resting orders use the complete fleet committed total when invoked by `main()`. The single-market compatibility path remains bounded to that market rather than silently guessing a fleet total.

**Verdict.** LIVE — scope bug fixed; fill and realized-P&L evidence remains OPEN.

### Session 8 — 2026-07-31: heartbeat threshold aligned with sweep cadence

**Question.** Does the dashboard mark a healthy 20-market fleet stale while it is still completing its normal sweep?

**Method.** Compared the dashboard heartbeat threshold with the observed paper-run cadence. A complete sweep was taking roughly 50–70 seconds, while the 45-second threshold was shorter than one normal sweep and could flash STALE between state-file writes. Increased the threshold to 120 seconds, leaving the state-file heartbeat as the primary liveness signal.

**Result.** The dashboard now allows one slow sweep to complete before declaring the fleet stale, while still exposing state age and DB age for diagnosis. This changes presentation/health classification only; it does not relax the strategy's capital or risk limits.

**Verdict.** LIVE — health indicator aligned with observed polling cadence; fill and realized-P&L evidence remains OPEN.

### Session 9 — 2026-07-31: cancellation lifecycle kept in the ledger

**Question.** Do hard-cap cancellations and partial emergency hedges leave the database's historical quote state consistent with the paper engine?

**Method.** Attached each simulated resting order to its persisted quote ID, marked released orders cancelled when emergency hedging or requoting removes them, and marked the residual of a shallow crossed hedge cancelled after the filled portion is logged. Recompiled and reran fill, quote, and dashboard tests.

**Result.** The in-memory order lifecycle and the `quotes` table now agree: cancelled offers are not presented as still open, while filled crossed portions remain recorded as fills. The final paper run must be restarted because this lifecycle change invalidates the prior sample.

**Verdict.** LIVE — ledger lifecycle is aligned; fill and realized-P&L evidence remains OPEN.

### Session 10 — 2026-07-31: filled quote rows retain their source order

**Question.** Does the quote ledger stay accurate when a resting simulated order fills before it is cancelled or resized?

**Method.** Propagated each resting order's persisted quote ID into tape-confirmed `Fill` records, passed that ID to `store.log_fill`, and changed cancellation to preserve the `filled` amount while marking the remaining lifecycle closed. Recompiled and reran the full test suite.

**Result.** Maker fills now update the originating quote row instead of leaving it open with `filled = 0`; cancellation also works for partially filled rows. The next fresh paper run will therefore keep quote, fill, and active-order metrics aligned.

**Verdict.** LIVE — quote attribution and partial-cancel accounting are aligned; fill and realized-P&L evidence remains OPEN.

### Session 11 — 2026-07-31: launcher waits for a clean handoff

**Question.** Can a `-FreshRun` or restart accidentally archive a live database or race the old dashboard on port 8800?

**Method.** Replaced the launcher’s hard-coded checkout path with a path derived from `$PSScriptRoot`, and added a bounded wait after process termination. The script now refuses to archive or restart if fleet children remain or port 8800 is still listening.

**Result.** Startup is checkout-portable and fails closed instead of mixing old and new writers. The active paper run was restarted without `-FreshRun` after the final UI edit, preserving its clean sample while reloading the dashboard process.

**Verdict.** LIVE — restart handoff is guarded; fill and realized-P&L evidence remains OPEN.

### Session 12 — 2026-07-31: quote-row cancellation behavior covered by a regression test

**Question.** Does cancelling a partially filled quote preserve the filled quantity while closing only its remaining lifecycle?

**Method.** Added an isolated store test that creates a 100-share quote, records a 25-share tape-confirmed fill, calls `mark_cancelled`, and reads the row back from SQLite. Ran the full test suite and compile checks.

**Result.** The row remains `filled = 25` and becomes `cancelled = 1`; the database no longer loses the executed portion when an order is released. This protects the crossed-hedge and requote accounting used by the live paper process.

**Verdict.** LIVE — partial-cancel accounting is now directly tested; fill and realized-P&L evidence remains OPEN.
