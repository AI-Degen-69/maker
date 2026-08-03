# Research Summary — Maker

Condensed, dated view of [RESEARCH_LOG.md](RESEARCH_LOG.md). Newest at the
bottom. One bullet per concrete thing done, tried, found, or broken.

---

## 21/07/2026

* Pulled **56,768 of @powerwinner's BTC/ETH 5-min fills** (14–21 Jul, 2,970 markets) to test whether his smooth equity curve came from doing our strategy better.
* Found he does the **opposite**: enters at 0.30–0.70 (we use 0.80–0.99), in the **first** 40% of the window (we use the last 40%), and has **zero** trades at 0.98+.
* His market win rate is **41.4%** against a 56.1% breakeven — on direction alone he loses. He is not a predictor.
* Decomposed his P&L: gross **+$39,884/week**, but **−$32,501** if charged our taker fee. The entire difference is that he pays no taker fee — he rests limit orders. His volume concentrates where `fee ∝ p(1−p)` peaks.
* **Tested and rejected** the theory that his both-sides buying is locked arbitrage: the pair costs 0.9990 for a $1.00 payout and is favourable only 51.1% of the time. Spread capture is ~68% of his gross.
* Caught my own analysis error first: BTC resolution coverage was 59.1% vs ETH 98.2% (rate-limit failures, dropped non-randomly), which produced a plausible but wrong −$23,828. After recovery, computed payout matched his on-chain redeems to **0.1%**.
* Built a maker sim with a **queue-aware fill model**. First attempt keyed off the trade tape's `side`, but 194/200 rows read "BUY" — data-api reports each participant's own side, not the aggressor's. Rebuilt on **book deltas**, where queue movement is directly observable.
* Documented the fill model's optimistic biases in the module rather than hiding them; output is an upper bound. Seven unit tests cover queue precedence, sweeps and overfill.
* Found **balance is the dominant P&L driver** across 44 settled markets: perfectly hedged +$30.70/market, badly unbalanced −$50.95. Hedged total +$409, unbalanced −$848 — that gap is the whole loss. Unbalanced markets lose *consistently* (swing 7.3), which is adverse selection, not luck.
* Fixed two guards that were blocking the balancing trade: the cost cap returned no quotes at all (×1043), and the pair cap rejected the hedge at $1.00.
* **OPEN regression:** that fix now lets pair costs exceed $1.00 (1.09, 1.07, 1.03) for a $1.00 payout — a guaranteed loss on the hedged portion. Deliberately not fixed during the repo migration; needs its own change and a fresh run.
* Added a single-instance pid guard after finding **four concurrent bots** writing to one database, summing independent inventories into silently invalid data.
* Split the repo out of the taker's, with identical structure and its own EN/HE research.

* Deployed online: own Railway service, own volume at `/data`, own domain. Preflight verified the region (Binance reachable) and that the volume is writable before the bot starts.

## 22/07/2026

* The OPEN pair-cost regression is now **confirmed and quantified** over 53 settled markets: median pair cost **1.0419** for a $1.00 payout, only **4%** of pairs clearing under $1.00, realized **−$1,172.07**, ROI **−4.1%**.
* The proof is that those two numbers match: paying 1.0419 for a $1.00 payout is a ~4.2% guaranteed loss on the hedged portion, and observed ROI is −4.1%. This is **not** adverse selection or variance — the bot is systematically overpaying for its hedge.
* Everything else is working: fill rate **37.6%** against real queue depth (median 72 shares ahead), **0.48¢** captured versus a 0.50¢ theoretical half-spread, inventory balance **0.99** against a 0.92 target. Execution is sound; price discipline is not.
* Root cause is the earlier fix that let the balancing side bypass the pair-cost cap. It achieved balance (0.99) at the cost of price discipline (4% of pairs under $1.00). Fix direction: cap the hedge at a price keeping the pair under $1.00, and skip the hedge when no such price exists — accept some imbalance rather than guaranteed loss.
* The dashboard reports 90/95/99% confidence **reached** at n=53 (mean −$22.11, σ $16.01). Arithmetically correct, but what is proven is that **the bug loses money** — already known. It says nothing about whether market making works here. **Verdict: DEAD for this configuration; the strategy itself remains OPEN** pending a fresh run after the fix.

## 22/07/2026 (afternoon)

* **Fixed the pair-cost cap bug that caused the 53-market loss.** In `strategy/quotes.py` the hedge exemption was removed — `max_pair_cost = 0.995` now applies to EVERY side, and a fresh-market guard blocks opening any position unless BOTH sides fill into a sub-$1.00 pair at ask−1tick. The bot now sits out wide markets instead of taking a guaranteed-loss leg.
* **Added the decisive census instrumentation.** `hedge_census` table records, per distinct market, whether a fillable sub-$1.00 pair existed at touch. `kpi` aggregates the fillable rate + median pair-at-touch; this was the one number never measured on clean data.
* **Defined where the experiment ends** in `strategy/config.py`: Phase A census of 60 markets — if fillable-sub-$1.00 rate `< 50%`, stop (DEAD); else Phase B settles 120 markets and reads P&L sign + confidence. The dashboard renders the phase banner + census progress live.
* **Started a fresh run** (single instance, fresh `maker.db`, local bot + dashboard). First census market came back **fillable at 0.99** — i.e. makeable — directly contradicting the contaminated run's 4% figure. That is the hypothesis now under test.
* **Instrumentation bug fixed:** a Hermes `PYTHONPATH` leak shadowed the project `.venv`, so `pip` "satisfied" deps into Hermes's site-packages while runtime import-failed. Fixed by launching with `env -u PYTHONPATH ./.venv/Scripts/python.exe ...`. General lesson on this Windows host: always unset `PYTHONPATH` before invoking a project venv.

## 22/07/2026 (night)

* **Built the balance enforcer** — the actual loss driver was PARTIAL fills (one side fills, the other never does before the 5-min window closes → one-sided settlement loss), not the entry cap bug. In `strategy/main.py`, when `t_remaining ≤ 20s` and balance `< 0.92`, the bot cancels rests and CROSSES the spread to buy the missing leg, exactly matching the held side. Every settled market now settles balanced by construction.
* **Crossed fills tagged** `crossed=1` in `store` (self-healing column) so `kpi`/`settlements` separate a settlement hedge from a maker fill. Dashboard gained a **HEDGE X** card (settlement-crossing count) — the leading indicator of whether partial-fill was the driver.
* **Clean re-run after the code change** (AGENTS.md: params change → sample invalid). Archived the 4 partial-DB runs, wiped `maker.db`, relaunched fresh. Verified: equity $5,000, realized $0, hedges 0, Phase A_CENSUS, census median pair 0.995 (cap fix holding).
* **Two bugs caught:** (1) `kpi.report()` referenced a bare `c` cursor out of scope → dashboard silently returned `{"error": ...}` under HTTP 200; fixed via `_rows()`. (2) MSYS `ps`/`kill` PIDs are translated and `taskkill` rejects them — the reliable kill path is `powershell Get-CimInstance Win32_Process` for the native PID, then `taskkill /F /PID <native>`.

## 28/07/2026

* **The fill rate was overstated 16x, and the old number measured nothing.** Built `scripts/record_books.py` (raw live books to a standalone `books.db`) and `scripts/measure_fill_rate.py` (replay any quoting rule against the same books). The book-only model reported a **50%** share fill rate — and **100% of those fills came from one branch**: "the level emptied, so credit our whole remaining order". From bid-side deltas a mass cancel is indistinguishable from a mass trade, so that branch was an assumption carrying the entire result.
* **Fixed by measuring instead of assuming.** `scripts/fetch_trades.py` backfills the trade tape (~1,800 prints per 5-min window); `QueueFillEngine.on_book` now takes a `traded` map. Book delta = trades + cancels (seen only as the sum); the tape gives trades alone. Both advance queue position, only trades fill us. **Tape-confirmed fill rate: 3.1%** vs 50.0% book-only, same windows, same books, same strategy.
* **The balance hedge never worked.** It posted a *bid at the ask* — passive, not a cross. Reproduced: **0 of 150 shares** fill even as the book trades straight down through the level. It only ever looked like it worked because of the phantom-fill bug. So every unbalanced market stayed unbalanced, and partial fills are the documented main loss driver.
* **Fixing it costs more than the edge it protects.** Added `QueueFillEngine.cross()` (walks real ask depth, partial fills are a real outcome, `max_price` caps the walk). But a cross is a TAKER order: `taker_fee = shares * 0.07 * p * (1-p)` = **1.75c/share at p=0.50, against a ~1c pair edge** — the fee peaks exactly where this strategy trades. `kpi.py` was charging zero for it, so P&L would have read high; it now charges the fee, excludes crossed shares from the fill-rate numerator, and pays them no maker rebate.
* **Added powerwinner's two missing rules** — price band 0.30-0.70 and quoting only in the first 40% of the window — each behind its own switch so they can be measured one at a time.
* **Dashboard now shows maker metrics:** fill rate vs queue depth, fill provenance (tape-confirmed vs inferred vs crossed), pair-cost distribution, quote uptime + top skip reasons, partial-fill exposure, taker fees, spread capture per share. `fills.reason` is persisted so provenance traces to a real row.
* **Two bugs in this session's own tooling, caught before they produced a finding:** the replay harness stopped quoting a side after a complete fill (a filled order is done, not resting); the tape cursor advanced after `continue` paths and would have re-credited prints. Tests 8 -> 31; the harness is verified against scripted books with hand-computed answers before touching real data.

## 31/07/2026
* **Parked the port-8788 single-bot pipeline on a sibling git branch
  (`archive/legacy-bot-8788`).** The fleet on port 8800 has rendered
  the legacy single-market code (server/dashboard, server/kanban,
  strategy/main.py's loop, scripts/run_fleet.py, deploy/run_service.py)
  functionally dead on this host. All five moved to
  `archive/legacy-bot-8788/<original subpath>`. `strategy/main.py` keeps
  only `full_book` + `recent_trades` — the two helpers `strategy.fleet`
  still imports; the dead tail is preserved at the archive path. The
  fleet dashboard test now validates the live fleet page only.
  `.gitignore` was loosened to track `archive/legacy-bot-8788/`; the rest
  of `archive/` (DB snapshots, NEXT_SESSION notes) stays gitignored.
  Both `feat/live-readiness` and `archive/legacy-bot-8788` point at this
  single commit, so the live branch is clean and the historical version
  is recoverable.
* **Prepared a fresh $1,000 paper run and simplified the dashboard.** Configured a $1,000 simulated wallet with $900 allocation headroom, a $1,000 committed-cap ceiling, and a $400 fleet naked-risk ceiling. The dashboard now foregrounds liquidation P&L, projected reward, committed wallet, naked risk, realized P&L, and heartbeat health; projected return uses total committed capital instead of offers alone. The startup script gained `-FreshRun` to archive the old DB/sidecars and stale state file before restart.
* **Caught and bounded a $1,036.80 committed-cap overshoot.** The first fresh sweep retained old-size orders and reserved new offers against a stale total. Post-cancellation reservation now resizes existing orders, caps new resting notional, and caps emergency crossed hedges; validation fell to $367 then $256 committed, below the $1,000 ceiling.
* **Made fleet-wide cap scope explicit.** `visit()` now accepts the complete fleet state list from `main()` for emergency-hedge and resting-order affordability, while direct single-market calls remain safely bounded to one state; the undefined-local scope bug is gone.
* **Aligned dashboard heartbeat health with observed sweep cadence.** Raised the stale threshold from 45 to 120 seconds because a normal 20-market sweep takes roughly 50–70 seconds; state age and DB age remain visible for diagnosis.
* **Kept cancellation lifecycle in the ledger.** Simulated orders now retain their quote IDs so hard-cap releases and requotes mark database rows cancelled; shallow emergency hedges also close their unfilled residual quote row.
* **Linked maker fills back to their quote rows.** Tape-confirmed resting fills now carry the originating quote ID, and cancellation preserves partial fill quantities while closing the remaining quote lifecycle.
* **Corrected the health label to match the 120-second stale threshold.** The dashboard now says `heartbeat < 120s` instead of retaining the old 45-second copy.
* **Hardened the launch path:** `fleet-start.ps1` מחשב את ה-checkout מתוך `$PSScriptRoot` וממתין שתהליכי-הבן ופורט 8800 יתפנו לפני הפעלה מחדש, ונכשל-סגור אם אחד מהם נשאר. נוספה בדיקת רגרסיה לשימור מזהה ההצעה המקורי במילויי טייפ מאושרים.
* **התנהגות ביטול הצעה שמולאה חלקית קיבלה כיסוי ישיר.** בדיקת store מוודאת ש־25 מניות שמולאו נשארות ב־`filled` כאשר שורת ההצעה מסומנת `cancelled = 1`.

## 03/08/2026

* **Hardened process ownership, database recreation handling, and ranking concurrency.** Updated `fleet-procs.ps1` to require exact start-time tick matching. Added per-run unique temp files, try/finally cleanup, and marker ownership checks to `rank_markets.py`. Updated `store.py` schema-readiness tracking to compare file stat identities so DB file recreation triggers schema init. Extracted `_atomic_write_json` helper in `fleet.py` and exposed `stale_after_sec` in `fleet_dash.py`.
