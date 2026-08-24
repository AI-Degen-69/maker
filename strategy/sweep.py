"""The market sweep -- one pass of the fleet engine over one market.

Second slice of the sweep extraction (issue #12) completes the module: the
book gate, quote decision, size caps and event recording now live behind the
sweep's ONE interface -- `sweep(state, ctx) -> outcome` -- and the fleet loop
in `strategy.fleet` keeps orchestration only (pulse, reallocation,
scheduling, settlement). The settle-and-cancel step moved here first (issue
#11); the rest of the former `fleet.visit` body followed behind the private
seams below.

The vocabulary is defined in `CONTEXT.md`: the sweep is the per-market unit
of engine work; the book gate is its first decision; the settle-and-cancel
step is the first internal seam. Everything under `sweep()` is a private
step: identity gate, market load, book gate, fill processing, gate advance,
exits, requote, score-and-publish. Feed a state and a context (books come
from the context's fetchers), assert the outcome -- that is the interface
the tests drive.

Exceptions propagate to the caller: the fleet loop owns catching them and
keeping the heartbeat alive, exactly as it did around `visit`.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from strategy import gate, markout, merge, profit_take, rewards, store
from strategy.markets import (fetch_pinned_market, full_book, recent_trades)
from strategy.quotes import decide_quotes, mid_price
from strategy.selector import identity_allowed, pair_books_allowed

if TYPE_CHECKING:
    from strategy.fleet import MarketState

log = logging.getLogger("sweep")

# How long a book-readiness failure (book fetch or the depth/spread gate) must
# persist before the fleet acts on it. The venue's books flicker: a live
# match's top-3 depth routinely dips under the $1K bar for a single poll and
# refills a second later. Acting on the FIRST failure made the fleet cancel
# and re-post its quotes on every such blip -- the dashboard flashed
# STALE/ERROR plus a fake full-cost unrealized loss every few seconds. This
# window is the confirmation delay: a blip that recovers never accumulates
# toward a cancel, while a genuinely dead book still gets the full
# cancel+stamp treatment within a few rotations.
BOOK_GATE_CONFIRM_SEC = 15.0

# Cooldown before re-attempting a market whose metadata would not load. Long
# enough that a closed market stops costing a request per rotation, short
# enough that a market recovering from a venue blip is picked back up within
# a sweep or two.
MARKET_RETRY_SEC = 60.0


@dataclass(frozen=True)
class SweepContext:
    """Everything the sweep needs beyond the market state itself.

    `bot_cfg` carries the venue endpoints the fetchers talk to (`clob_host`);
    `now` is the sweep's clock so a test can pin it. The fleet-level numbers
    -- naked cost, committed cost, posture -- are injected rather than
    re-derived inside the sweep because they are properties of every OTHER
    market as well: a cap evaluated against this market's own inventory alone
    lets the overshoot through. `states` is the complete market list so
    emergency-hedge affordability and resting-order reservation use the same
    fleet-wide committed total; the single-market helper in tests passes None
    and the sweep falls back to `[state]`. `resolved_cids` lets the sweep
    settle a market the venue has already closed before touching its book.
    """

    bot_cfg: Any
    now: float
    fleet_naked_usd: float = 0.0
    committed_usd: float = 0.0
    states: Any = None
    fleet_posture: str = gate.NORMAL
    resolved_cids: frozenset = frozenset()


@dataclass(frozen=True)
class SweepOutcome:
    """What one sweep did, so a test can assert it without engine internals.

    `status` is one of: SETTLED (venue already closed it), IDENTITY_BLOCKED
    (selector refused), COOLDOWN / UNLOADABLE (market metadata unreadable),
    BOOK_HOLDING (book failure inside the confirmation window -- nothing
    acted on yet), BOOK_FAILED (confirmed -- quotes cancelled and error
    stamped), QUOTING / BLOCKED / WAITING (the requote verdict). `prev_gate`
    and `gate` frame the state-machine transition, `why` the reason, `fills`
    how many fills were applied, `requoted` whether orders are resting, and
    `released` the dollars of committed cost freed by a SETTLED sweep.
    """

    status: str
    prev_gate: str
    gate: str
    why: str
    fills: int
    requoted: bool
    released: float = 0.0


def fleet_committed_cost(states) -> float:
    """Every dollar that has left the wallet or is spoken for.

    Inventory cost -- BOTH legs, paired and naked -- plus the notional resting
    in unfilled offers. `fleet_naked_cost` deliberately counts only the
    unhedged residue because that is what can lose money; this counts what is
    committed, which is a different question and the one nobody was asking.

    Measured 2026-07-30, the gap between them was the whole problem: $767
    naked (inside its $800 cap, looking healthy) against $9,588 committed.
    """
    total = 0.0
    for s in states:
        total += (s.inv.up_cost or 0.0) + (s.inv.down_cost or 0.0)
        # Resting offers are not spent yet, but they are promised: the venue
        # holds collateral against an open bid, and a fill converts the promise
        # into inventory without asking. Excluding them would let the fleet sit
        # exactly at the cap with thousands more already in flight.
        for o in s.engine.open_orders():
            total += o.price * max(0.0, o.size - o.filled)
    return total


def _affordable_cross_size(book_asks: dict, requested: float,
                           available_usd: float) -> float:
    """Maximum taker-hedge size whose ask notional fits the cap."""
    remaining = min(float(requested), sum(float(v) for v in book_asks.values()))
    budget = max(float(available_usd), 0.0)
    size = 0.0
    for price in sorted(book_asks):
        if remaining <= 1e-9 or budget <= 1e-9:
            break
        depth = max(float(book_asks.get(price, 0.0)), 0.0)
        take = min(depth, remaining, budget / price) if price > 0 else 0.0
        size += take
        remaining -= take
        budget -= take * price
    return size


def _pair_completion_size(asks: dict, requested: float, fill_cost: float,
                          max_pair_cost: float, available_usd: float) -> float:
    """Shares of the missing leg buyable at a price that keeps the completed
    pair under `max_pair_cost`, within the wallet's remaining committed room.

    The pairs-only rule (U35) buys the light leg at the ask to complete a
    one-sided fill. Two caps bind at once, and both are walked here like
    `_affordable_cross_size` walks the emergency-hedge ask ladder: the pair
    must stay under `max_pair_cost` (a completion that costs more than the
    $1.00 payout is a guaranteed loss), and the wallet's committed budget must
    not be exceeded. Stops the walk at the first ask level where
    `fill_cost + price >= max_pair_cost` -- deeper, worse levels would break
    the completion bound even if the wallet could afford them.
    """
    remaining = max(float(requested), 0.0)
    budget = max(float(available_usd), 0.0)
    size = 0.0
    for price in sorted(asks):
        if remaining <= 1e-9 or budget <= 1e-9:
            break
        if fill_cost + price >= max_pair_cost:
            break
        depth = max(float(asks.get(price, 0.0)), 0.0)
        take = min(depth, remaining, (budget / price) if price > 0 else 0.0)
        size += take
        remaining -= take
        budget -= take * price
    return size


def _affordable_rest_size(requested: float, price: float,
                          available_usd: float, market_room_usd: float) -> int:
    """Largest resting order that fits BOTH the wallet and this market's cap.

    Pure, and module-level rather than inline in the sweep, for the same
    reason `_affordable_cross_size` above is: the arithmetic is the whole fix
    and it has to be testable without standing up a market, a book and a fill
    engine.

    `market_room_usd` is `max_cost_per_market - inv.cost`. That cap used to be
    enforced only in quotes.py, against inventory ALREADY held, and only on the
    heavy side -- both readings are post-hoc, so a market holding nothing had
    room for an order of any size. Measured 2026-08-02: a 900-share order
    rested and filled in one print for $792 against a $400 cap, on a market
    whose inventory was empty when it was posted.

    Floors at 0 rather than going negative: an inventory already over its cap
    has no room, and `int()` on a negative would round toward zero and quietly
    hand back a positive-looking size.
    """
    if price <= 0:
        return 0
    room = max(float(market_room_usd), 0.0)
    wallet = max(float(available_usd), 0.0)
    return int(min(float(requested), wallet / price, room / price))


def _gate_with_fleet_fallback(prev_gate: str, own_stats: dict, cfg):
    """Advance one market's gate, borrowing the fleet verdict if it has none.

    Returns `(new_gate, stats_used)`; the caller stores the stats it was
    actually judged on so the dashboard reports the number behind the state.

    Two rules, and the second is the load-bearing one:

      * A market with no matured sample of its own inherits the POOLED verdict
        instead of holding `insufficient_sample` forever. Without this the gate
        is unreachable in practice: markets here rotate daily and, measured
        2026-08-02, the best-sampled market of 19 matured 7 markouts against a
        threshold of 8. Lowering the per-market minimum alone changed nothing.
      * A borrowed verdict is capped at WIDENED. EXITED is terminal by design,
        and a pooled reading is not evidence about THIS market -- the pooled
        mean on that run is -4.75c/share, past the catastrophic threshold, so
        an uncapped fallback would permanently blacklist all 19 markets at
        once, including the three that were individually EARNING (+4.4c,
        +5.0c, +5.3c) and had simply never reached a sample. Backing off on a
        bad universe is right; sentencing an unmeasured market on someone
        else's evidence is not.

    A market already EXITED stays EXITED: the cap must never become a route
    back into the book. `next_state` guarantees that independently, and the
    `prev_gate != EXITED` guard here keeps it true even if it stops.
    """
    stats = own_stats
    borrowed = False
    if stats.get("verdict") == "insufficient_sample":
        pooled = markout.fleet_stats(cfg.markout_fleet_min_sample)
        if pooled.get("verdict") != "insufficient_sample":
            stats, borrowed = pooled, True

    nxt = gate.next_state(prev_gate, stats, cfg)
    if borrowed and nxt == gate.EXITED and prev_gate != gate.EXITED:
        nxt = gate.WIDENED
    return nxt, stats


def _record_event(st: "MarketState", now: float, kind: str,
                  reason: str = "", side: str | None = None,
                  price: float | None = None, size: float | None = None,
                  reason_code: str | None = None, force: bool = False) -> None:
    """Write a meaningful operator event, collapsing routine repeats."""
    code = reason_code or store.reason_code(reason)
    key = (kind, side, code)
    previous_key = getattr(st, "event_key", None)
    previous_ts = getattr(st, "event_ts", 0.0)
    if not force and previous_key == key and now - previous_ts < 30.0:
        return
    # A fill/exit/hedge is more informative than the routine requote that
    # follows it in the same visit. Keep the event visible for one short window.
    if (not force and kind == "QUOTING" and previous_key
            and previous_key[0] in {"FILLED", "HEDGED", "MERGED", "EXITED"}
            and now - previous_ts < 30.0):
        return
    try:
        store.log_event(market_slug=getattr(st, "spec", {}).get("slug", ""),
                        condition_id=getattr(st, "cid", None), kind=kind, reason=reason,
                        reason_code=code, side=side, price=price, size=size,
                        ts=now)
        st.event_key, st.event_ts = key, now
    except Exception as e:
        log.warning("event log failed for %s: %s", getattr(st, "title", "market")[:30], e)


def _cancel_live_orders(st: "MarketState") -> None:
    """Cancel simulated resting quotes when a market loses eligibility.

    Inventory remains owned and continues to be monitored, but stale offers may
    not survive a hard selector failure or consume committed-capacity budget.
    """
    released = st.engine.open_orders()
    for order in released:
        order.cancelled = True
    try:
        store.mark_cancelled([order.quote_id for order in released
                              if order.quote_id is not None])
    except Exception as e:
        log.warning("selector cancellation not recorded for %s: %s", st.title[:30], e)
    live = st.spec.get("_live")
    if isinstance(live, dict):
        live["quotes"] = []
        live["capital"] = 0.0
        live["stale"] = True
        for field in ("up_bid", "up_ask", "dn_bid", "dn_ask", "mid_up",
                      "our_up", "our_dn_as_up", "dn_bid_as_up", "pair_cost"):
            live[field] = None


def _settle_resolved(st: "MarketState", now: float) -> float:
    """The venue has already reported a winner for this market (`resolutions`
    carries a row for it). Its book is gone from the venue -- every further
    fetch is a guaranteed 404 -- and its true settled P&L already comes out
    of `fills` + `resolutions` on the dashboard side (`_settled_positions` /
    `_realized`). Leaving the position "open" here double-books it as both
    realized and still-naked, and is exactly why a resolved market kept
    showing up as a permanent "unreadable" market with capital that never
    freed.

    `ts` IS touched, unlike `_stamp_failure`: these zeroed figures are
    correct as of right now, not stale ones the fleet failed to refresh.

    Returns the dollars of committed cost released (both legs) -- the
    outcome of the step, so the interface is self-describing. The engine
    ignores it; a test can assert it.
    """
    _cancel_live_orders(st)
    if st.inv.up_shares or st.inv.down_shares:
        _record_event(
            st, now, "RESOLVED",
            f"venue resolved; released {st.inv.up_shares:.2f} UP / "
            f"{st.inv.down_shares:.2f} DOWN", reason_code="RESOLVED",
            force=True)
    freed = (st.inv.up_cost or 0.0) + (st.inv.down_cost or 0.0)
    st.inv.up_shares = st.inv.down_shares = 0.0
    st.inv.up_cost = st.inv.down_cost = 0.0
    st.err = ""
    live = st.spec.get("_live")
    if not isinstance(live, dict):
        live = {}
        st.spec["_live"] = live
    live.update({
        "up_sh": 0.0, "dn_sh": 0.0, "up_avg": 0.0, "dn_avg": 0.0,
        "paired": 0.0, "naked_side": "", "naked_sh": 0.0, "naked_cost": 0.0,
        "pair_paid": 0.0, "fills": st.inv.fills,
        # `stale` back to False, because `_cancel_live_orders` above set it
        # True on the way in. That flag means "these figures are older than
        # the fleet's last look at the market", and these figures are the
        # opposite: measured now, against a settlement that is final. Left
        # True the dashboard renders a settled market as permanently STALE
        # while carrying a fresh `ts`, which is the page disagreeing with
        # itself about the one market whose numbers can no longer move.
        "stale": False,
        "err": "", "ts": now,
    })
    return freed


def _settle_startup_resolved(states, resolved_cids,
                             now: float) -> tuple[int, float]:
    """Zero inventory for any market the venue has already settled.

    `MarketState.__init__` rebuilds each market's inventory from the fills
    ledger, and the fills ledger never learns about resolutions -- so a market
    that resolved while the fleet was down comes back holding phantom shares
    that count as committed capital from the very first heartbeat. The first
    sweep would settle it, but only after its turn in the rotation comes
    around -- and if the ranker drops the market before that turn, the re-rank
    retention rule ("still holding inventory") keeps it in `states` forever on
    exactly the phantom position this pass clears.

    Settling here, before the first sweep, releases that capital at startup.
    The sweep will still find the cid in `resolved_cids` and settle it again,
    which is a no-op on an already-zeroed inventory.

    Scoped to `states` on purpose: only markets in the universe can display
    phantom inventory, and a resolved market the ranker has already dropped
    simply has no MarketState -- the dashboard's `_settled_positions` /
    `_realized` already reports its P&L straight from `fills` + `resolutions`.

    Returns (markets that held inventory and were settled, dollars of
    committed cost released) for the startup log line. `freed` is the FULL
    two-leg cost basis (`up_cost + down_cost`), so a paired position counts
    both legs -- that is the committed capital being released. One bad market
    must not stop the rest of the pass.
    """
    settled = 0
    freed = 0.0
    for st in states:
        if st.cid not in resolved_cids:
            continue
        try:
            if st.inv.up_shares or st.inv.down_shares:
                settled += 1
                freed += (st.inv.up_cost or 0.0) + (st.inv.down_cost or 0.0)
            _settle_resolved(st, now)
        except Exception as e:
            log.warning("startup settle failed for %s: %s: %s",
                        st.title[:30], type(e).__name__, e)
    return settled, freed


def _stamp_failure(st: "MarketState", now: float, err: str) -> None:
    """Record on the market's live payload that this sweep produced nothing.

    `sweep` returns early on several paths -- identity refusal, retry
    cooldown, an unloadable market, a failed book fetch, an unconfirmed
    book-gate failure (still inside BOOK_GATE_CONFIRM_SEC), and a confirmed
    one -- and all of them are ABOVE the `_live` write at the end of the
    function. A market that has been closed since yesterday therefore kept
    whatever `_live` it last succeeded with, or none at all, and the dashboard
    rendered it as data that was merely old rather than as a market the fleet
    cannot read.

    `ts` is deliberately NOT touched: it dates the FIGURES, and stamping it
    here would make a market that has failed for six hours look freshly
    measured. `err_ts` dates the failure, so the page can say both things.
    """
    live = st.spec.get("_live")
    if not isinstance(live, dict):
        live = {}
        st.spec["_live"] = live
    live["err"] = err
    live["err_ts"] = now
    _record_event(st, now, "ERROR", err, reason_code="ERROR")


def _book_gate_confirmed(st: "MarketState", now: float) -> bool:
    """Hysteresis for the book-readiness gates: is this failure real yet?

    The venue's books flicker. A live match's top-3 depth can dip under the
    $1K bar for a single poll and refill a second later; a fetch can time out
    once and succeed on the next rotation. Acting on the FIRST such failure
    made the fleet cancel its resting orders, blank the last-known bids and
    stamp STALE/ERROR on the dashboard's next poll -- then re-quote and clear
    it on the one after. The dashboard read the blanked bids as a $0 exit and
    flashed a fake full-cost unrealized loss every few seconds, and the
    constant cancel/re-post churn was exactly what read as "broken" to the
    venue.

    The first failure records when it started and returns False, so the caller
    returns early with orders and marks untouched. Only once the failure has
    persisted for BOOK_GATE_CONFIRM_SEC -- well beyond one or two blips, still
    short enough that a genuinely dead book gets the full treatment within a
    few rotations -- does this return True and let the caller cancel, stamp
    the error and blank the marks.

    A successful gate pass resets the clock in the book-gate step, so a blip
    that recovers never accumulates toward a false confirmation.
    """
    if st.book_gate_fail_since is None:
        st.book_gate_fail_since = now
    held = now - st.book_gate_fail_since
    if held < BOOK_GATE_CONFIRM_SEC:
        log.debug("%s: book gate failed %.0fs ago, holding (confirm in %.0fs)",
                  st.title[:30], held, BOOK_GATE_CONFIRM_SEC - held)
        return False
    return True


# --- private step seams ------------------------------------------------------


def _identity_gate(st: "MarketState", now: float) -> bool:
    """Refuse a market the selector would never have admitted.

    Defense in depth against stale or hand-edited markets.json. The ranker
    applies the same identity rule, but no stale universe may bypass it and
    reach a live quote merely because the ranker has not rewritten yet.

    A spec written BEFORE the selector existed carries a title and a slug and
    none of the five metadata fields the sports and macro keywords are read
    from, so judged normally every matchup in it fails -- "Yankees vs Red
    Sox" has no league word in its own title -- and the fleet cancels quotes
    across the entire universe until the next rank rewrites the file. That is
    a gap in the DATA, and refusing on it asserts something the data never
    said. `require_primary=False` keeps every rejection arm (the blocked
    keywords read title and slug, so Game 1 / Map 2 / live / in-play still
    go) and drops only the positive confirmation those absent fields would
    have carried. Bounded by the re-rank interval: once the file is rewritten
    the fields are there and the full rule applies again.

    Returns True to continue; on refusal, records the reason, cancels the
    market's resting quotes and stamps the failure, then returns False.
    """
    has_selector_meta = any(
        st.spec.get(k) for k in ("category", "market_type", "market_group",
                                 "series_title", "event_title"))
    identity_ok, identity_reason = identity_allowed(
        st.title, st.spec.get("slug"), st.spec.get("category"),
        st.spec.get("market_type"), st.spec.get("market_group"),
        st.spec.get("series_title"), st.spec.get("event_title"),
        require_primary=has_selector_meta)
    if not identity_ok:
        st.err = identity_reason
        _cancel_live_orders(st)
        _stamp_failure(st, now, st.err)
        return False
    return True


def _load_market(st: "MarketState", now: float):
    """The market's metadata, loading it on a cooldown when absent.

    Returns the market object, or one of the early-exit statuses after
    stamping: "COOLDOWN" when the market is still cooling down from a failed
    load (return without spending a request: the loop's time budget is what
    the dashboard measures as liveness), "UNLOADABLE" when the venue cannot
    produce it (closed, not accepting orders, or a token count other than 2).

    Reward funding is deliberately NOT a loadability condition: the ranker
    already decided this market belongs in the universe, and half of it now
    pays spread rather than rent.
    """
    if st.market is not None:
        return st.market
    if now < st.market_retry_ts:
        # Still cooling down from a failed load. Return without spending a
        # request: the loop's time budget is what the dashboard measures as
        # liveness, and a market that was closed ten seconds ago is not worth
        # re-asking about on every rotation.
        _stamp_failure(st, now, st.err or "market unloadable (cooling down)")
        return "COOLDOWN"
    st.market_retry_ts = now + MARKET_RETRY_SEC
    st.market = fetch_pinned_market(st.cid, require_rewards=False)
    if st.market is None:
        # Funding is no longer a rejection cause here: `require_rewards` is
        # False, so an unfunded market comes back fine. What is left is
        # closed, not accepting orders, or a token count other than 2 -- and
        # the dashboard renders this string as the market's `err`, so naming
        # rewards sent an operator hunting a pot that was never the problem.
        st.err = "closed / not accepting orders"
        _stamp_failure(st, now, st.err)
        return "UNLOADABLE"
    return st.market


def _load_books(st: "MarketState", m, cfg, ctx: SweepContext):
    """The two books, gated for depth and spread with the confirmation window.

    Returns `(up, dn)` when both sides are readable and healthy. On an early
    exit returns "BOOK_HOLDING" (a fetch or gate failure still inside the
    confirmation window -- nothing acted on, orders and marks untouched) or
    "BOOK_FAILED" (persisted past the window -- quotes cancelled, the error
    stamped and the last-known bids blanked).
    """
    try:
        up = full_book(ctx.bot_cfg.clob_host, m.up_token)
        dn = full_book(ctx.bot_cfg.clob_host, m.down_token)
    except Exception as e:
        # A single failed fetch is usually a venue blip (timeout, 5xx), not a
        # dead book. Hold for the confirmation window before cancelling.
        if not _book_gate_confirmed(st, ctx.now):
            return "BOOK_HOLDING"
        st.err = f"book fetch: {e}"
        _cancel_live_orders(st)
        _stamp_failure(st, ctx.now, st.err)
        return "BOOK_FAILED"
    st.err = ""

    # Fills are decided by the TAPE, not by the book emptying: a level that
    # vanishes on cancellations must fill us nothing.
    # Defense in depth: ranker output may be stale while the live books have
    # already dried up. Require both YES and NO to retain at least
    # `select_min_top3_depth_usd` in the top three bid levels and no more than
    # `select_max_book_spread` of two-sided spread before any fill or quote
    # handling. Stated by config name, not by number: the bars moved to $1,000
    # and 0.06 on 2026-08-06 to match `risk.book_health`, and a hardcoded
    # number here would go stale the next time they move.
    books_ok, books_reason = pair_books_allowed(
        [("YES", up["bids"], up["asks"]), ("NO", dn["bids"], dn["asks"])],
        cfg.select_min_top3_depth_usd, cfg.select_max_book_spread)
    if not books_ok:
        # A live match's depth dips under the bar for a second and refills.
        # Hold for the confirmation window instead of cancelling quotes and
        # stamping STALE/ERROR on the first blip.
        if not _book_gate_confirmed(st, ctx.now):
            return "BOOK_HOLDING"
        st.err = books_reason
        _cancel_live_orders(st)
        _stamp_failure(st, ctx.now, st.err)
        return "BOOK_FAILED"
    # The book is readable again: a transient dip that recovered must not
    # accumulate toward a future false confirmation.
    st.book_gate_fail_since = None
    return up, dn


def _process_fills(st: "MarketState", m, up, dn, now: float) -> int:
    """Reconcile the tape against the books and apply every fill.

    Fills are decided by the TAPE, not by the book emptying: a level that
    vanishes on cancellations must fill us nothing. Returns how many fills
    were applied, so the sweep's outcome reports it.
    """
    tape = recent_trades(m.condition_id, st.seen_trades)
    first_pass = not st.tape_primed
    st.tape_primed = True
    applied = 0
    for book in (up, dn):
        # A token with NO trades this poll must read as an empty tape, not a
        # missing one. `tape.get(...)` returns None in both cases, and before
        # U1 that None sent the engine down the cancel-ambiguous delta path --
        # so the quietest markets, where nothing traded at all, were exactly
        # the ones generating phantom fills. `{}` says measured-and-empty;
        # None is reserved for a tape we genuinely could not read.
        traded = None if tape is None else (tape.get(book["token_id"]) or {})
        if first_pass:
            traded = None      # a startup backlog is not evidence about us
        mark = len(st.engine.unverified)
        recon_mark = len(st.engine.reconciliation)
        fills = st.engine.on_book(book["token_id"], book["bids"], now,
                                  traded=traded)
        new_unverified = st.engine.unverified[mark:]
        new_recon = st.engine.reconciliation[recon_mark:]

        # U6. Classify the outcome now, while the queue position that produced
        # it is still known. Reconstructing "were we behind the queue?" later
        # from `fill_evidence` is not possible -- the blob records the book,
        # never our place in it.
        try:
            store.log_fill_recon([
                (r.ts, m.condition_id, r.token_id, r.side, r.price,
                 r.tape_volume, r.queue_ahead, r.remaining, r.credited,
                 r.outcome) for r in new_recon])
        except Exception as e:
            log.warning("fill recon not recorded for %s: %s", st.title[:30], e)
        # The engine's list is an append-only log and this loop runs every
        # poll for the life of the process; without draining it the fleet
        # leaks a row per order per poll for as long as it runs.
        del st.engine.reconciliation[:]

        # Persist the decision inputs so a later engine change can be replayed
        # offline -- the capability whose absence forced Phase A to verify by
        # forward running instead of replaying the 18.7h run.
        try:
            store.log_fill_evidence(
                ts=now, condition_id=m.condition_id,
                token_id=book["token_id"],
                bids_json=json.dumps({str(p): s for p, s in book["bids"].items()}),
                tape_json=(None if traded is None
                           else json.dumps({str(p): v for p, v in traded.items()})),
                credited=sum(f.size for f in fills),
                unverified=sum(f.size for f in new_unverified))
        except Exception as e:
            log.warning("fill evidence not recorded for %s: %s", st.title[:30], e)

        for f in new_unverified:
            # Recorded, never applied. These shares were not bought.
            store.log_unverified_fill(
                ts=now, market_slug=m.market_slug,
                condition_id=m.condition_id, token_id=f.token_id,
                side=f.side, price=f.price, size=f.size,
                queue_waited=f.queue_waited, reason=f.reason)
        for f in fills:
            if f.side == "UP":
                st.inv.up_shares += f.size
                st.inv.up_cost += f.size * f.price
            else:
                st.inv.down_shares += f.size
                st.inv.down_cost += f.size * f.price
            st.inv.fills += 1
            applied += 1
            # U35 pairs-only rule: the 15-minute action window is dated off
            # the most recent fill, so a fill that lands now opens (or
            # re-opens) the window.
            st.last_fill_ts = now
            store.log_fill(
                market_slug=m.market_slug, condition_id=m.condition_id, token_id=f.token_id,
                side=f.side, price=f.price, size=f.size,
                quote_id=f.quote_id, mid_at_post=None, edge_vs_mid=None,
                queue_waited=getattr(f, "queue_waited", 0.0),
                seconds_to_fill=0.0, crossed=False, reason=f.reason,
            )
            _record_event(st, now, "FILLED",
                          f"{f.side} {f.size:.0f}sh @ {f.price:.3f}",
                          side=f.side, price=f.price, size=f.size,
                          reason_code="FILL", force=True)

            # Open the markout clock. `ref_mid_source` is the load-bearing
            # field: in paper mode our quotes never reach the venue, so this
            # book is already clean of our own size. A LIVE run must pass
            # 'contaminated' unless it subtracts our resting size first --
            # otherwise markout measures our own footprint and hands it back
            # as edge.
            store.log_markout_open(
                ts=now, condition_id=m.condition_id,
                market_slug=m.market_slug, side=f.side,
                fill_price=f.price, size=f.size,
                ref_mid=mid_price(book.get("best_bid"), book.get("best_ask")),
                ref_mid_source="venue_clean")
            log.info("FILL %-28s %-4s %.0fsh @ %.3f",
                     st.title[:28], f.side, f.size, f.price)
    return applied


def _apply_pairs_rule(st: "MarketState", m, up, dn, cfg, ctx: SweepContext,
                      committed_states) -> dict:
    """U35 pairs-only rule: resolve a one-sided fill within its window.

    Measured on the 112h clean sample: merged pairs were 7/7 profitable at
    +16.3c/share, while a naked leg held past 15 minutes drifted -18.5c/share
    by the 1h mark. The rule therefore turns every recent one-sided fill into
    either a COMPLETED pair -- cross the missing leg at ask when
    `heavy_avg + ask < max_pair_cost` (the pair is guaranteed sub-$1.00, and
    the merge step later in this same sweep redeems it at parity) -- or an
    EXIT of the naked leg at the best bid (pay the ~3c half-spread instead of
    the drift). A fill older than `pairs_exit_window_sec` is left alone; the
    rule's license is the 15-minute window, not naked-position management in
    general.

    Returns the verdict dict for the live payload. Side effects: the pair
    completion crosses the book (fills + inventory + close accounting via the
    subsequent merge), the exit books a `closes` row with method='naked_exit'
    (one leg only -- the readers in stats.py are side-aware for it), and both
    arms record a market event so the EV KPI can count them.
    """
    if not cfg.enable_pairs_rule:
        return {"action": "disabled"}
    naked = abs(st.inv.up_shares - st.inv.down_shares)
    if naked <= 1e-9:
        return {"action": "balanced"}
    if st.last_fill_ts is None:
        # A naked position with no fill clock (pre-U35 inventory) is out of
        # scope: the rule acts on fills, not on whatever predates it.
        return {"action": "no_fill_clock"}

    age = ctx.now - st.last_fill_ts
    if age > cfg.pairs_exit_window_sec:
        # Window expired. Record the expiry ONCE per fill -- the handled
        # stamp is per fill, so a later fill re-opens the window.
        if st.pair_rule_handled_ts != st.last_fill_ts:
            st.pair_rule_handled_ts = st.last_fill_ts
            heavy_side = ("UP" if st.inv.up_shares > st.inv.down_shares
                          else "DOWN")
            _record_event(st, ctx.now, "PAIR_WINDOW_EXPIRED",
                          f"one-sided fill {age / 60:.0f}m old rode out the "
                          f"{cfg.pairs_exit_window_sec / 60:.0f}min window "
                          f"({naked:.0f}sh {heavy_side} still held)",
                          side=heavy_side, size=naked,
                          reason_code="PAIR_EXPIRED", force=True)
            log.info("PAIR_WINDOW_EXPIRED %-28s %.0fsh %s after %.0fm",
                     st.title[:28], naked, heavy_side, age / 60.0)
        return {"action": "expired", "age": round(age, 1)}

    heavy_side = "UP" if st.inv.up_shares > st.inv.down_shares else "DOWN"
    light_side = "DOWN" if heavy_side == "UP" else "UP"
    heavy_book = up if heavy_side == "UP" else dn
    light_book = up if light_side == "UP" else dn
    fill_cost = st.inv.avg(heavy_side)
    ask = light_book.get("best_ask")

    # COMPLETE: buy the missing leg at ask when the pair stays under the cap.
    # The completion is a TAKER order (the mirror of the emergency-hedge
    # crossing in `_requote`), capped by both the pair-cost bound and the
    # wallet's committed room. A partial completion is a real outcome -- the
    # residue stays naked and the rule re-runs next sweep, still inside the
    # window.
    if ask is not None and ask > 0:
        available = max(cfg.max_committed_usd
                        - fleet_committed_cost(committed_states), 0.0)
        cross_size = _pair_completion_size(
            light_book.get("asks") or {}, naked, fill_cost,
            cfg.max_pair_cost, available)
        if cross_size >= 1.0:
            asks = light_book.get("asks") or {}
            qid = store.log_quote(
                market_slug=m.market_slug, condition_id=m.condition_id,
                token_id=light_book["token_id"], side=light_side,
                price=ask, size=cross_size, queue_ahead=0.0,
                mid=mid_price(light_book.get("best_bid"), ask),
                edge_vs_mid=None, t_remaining=None,
            )
            got = 0.0
            # max_price is defense-in-depth on top of _pair_completion_size's
            # walk: the size math already refuses levels where
            # fill_cost + price >= max_pair_cost, but the cross primitive's
            # own documented guard must also refuse them, so a future change
            # to the size math cannot silently walk a completion past the cap
            # (a pair that costs >= $1.00 is a guaranteed loss after gas).
            max_price = max(cfg.max_pair_cost - fill_cost, 0.0)
            for f in st.engine.cross(light_book["token_id"], light_side,
                                     cross_size, asks, ctx.now,
                                     max_price=max_price):
                if f.side == "UP":
                    st.inv.up_shares += f.size
                    st.inv.up_cost += f.size * f.price
                else:
                    st.inv.down_shares += f.size
                    st.inv.down_cost += f.size * f.price
                st.inv.fills += 1
                got += f.size
                # crossed=True is load-bearing downstream: kpi.py excludes
                # these from the maker fill rate and charges the taker fee.
                store.log_fill(
                    quote_id=qid, market_slug=m.market_slug,
                    condition_id=m.condition_id, token_id=f.token_id,
                    side=f.side, price=f.price, size=f.size,
                    mid_at_post=ask, edge_vs_mid=None, queue_waited=0.0,
                    seconds_to_fill=0.0, crossed=True, reason=f.reason,
                )
            if got + 1e-9 < cross_size:
                store.mark_cancelled([qid])
            pair_cost = fill_cost + ask
            _record_event(st, ctx.now, "PAIR_COMPLETE",
                          f"completed pair: bought {light_side} {got:.0f}sh "
                          f"@ ~{ask:.3f} (fill {fill_cost:.3f} + ask "
                          f"{ask:.3f} < {cfg.max_pair_cost:.3f})",
                          side=light_side, size=got,
                          reason_code="PAIR_COMPLETE", force=True)
            log.info("PAIR_COMPLETE %-28s %-4s %.0fsh @ %.3f (pair %.4f)",
                     st.title[:28], light_side, got, ask, pair_cost)
            return {"action": "complete", "side": light_side, "size": got,
                    "price": ask, "pair_cost": round(pair_cost, 4)}

    # EXIT: the pair is not fillable under the cap (or the wallet cannot
    # afford it), so sell the naked leg at the best bid rather than hold it
    # into the drift. Capped at the bid ladder's depth, like profit_take.
    bid = heavy_book.get("best_bid")
    if bid and bid > 0:
        bids = heavy_book.get("bids") or {}
        size = min(naked, sum(bids.values()))
        if size >= 1.0:
            proceeds, avg_price = profit_take._walk(bids, size)
            fee = size * cfg.profit_take_fee_per_share
            cost_basis = size * fill_cost
            realized = proceeds - cost_basis - fee
            # Ledger first, memory second -- same discipline as `_manage_exits`:
            # a close must never exist in memory without also existing on disk,
            # or a restart rebuilds a position the live process already sold.
            if heavy_side == "UP":
                up_price, dn_price = avg_price, None
                up_removed, dn_removed = cost_basis, 0.0
            else:
                up_price, dn_price = None, avg_price
                up_removed, dn_removed = 0.0, cost_basis
            store.log_close(
                condition_id=m.condition_id, market_slug=m.market_slug,
                method="naked_exit", shares=size, up_price=up_price,
                dn_price=dn_price, cost_basis=cost_basis, proceeds=proceeds,
                fee=fee, realized_pnl=realized,
                # The leg would have paid $1 or $0 at resolution -- unknown,
                # so no forgone figure is recorded rather than a guessed one.
                forgone_vs_settlement=None,
                up_cost_removed=up_removed, dn_cost_removed=dn_removed)
            if heavy_side == "UP":
                st.inv.up_shares -= size
                st.inv.up_cost -= cost_basis
            else:
                st.inv.down_shares -= size
                st.inv.down_cost -= cost_basis
            _record_event(st, ctx.now, "NAKED_EXIT",
                          f"exited naked {heavy_side} {size:.0f}sh @ "
                          f"{avg_price:.3f} (pair not fillable under "
                          f"{cfg.max_pair_cost:.3f})",
                          side=heavy_side, size=size,
                          reason_code="NAKED_EXIT", force=True)
            log.info("NAKED_EXIT %-28s %-4s %.0fsh @ %.3f pnl %+.2f",
                     st.title[:28], heavy_side, size, avg_price, realized)
            return {"action": "exit", "side": heavy_side, "size": size,
                    "price": round(avg_price, 4), "realized_pnl": realized}

    return {"action": "deferred", "why": "no fillable pair and no exit bid"}


def _advance_gate(st: "MarketState", m, up, dn, cfg, ctx: SweepContext):
    """Price matured fills, then advance this market's gate.

    Prices every fill whose horizon has just matured, re-reads the market's
    markout verdict (with the fleet fallback when it has none), persists the
    moment the gate gives up on the market, and folds the fleet-wide exposure
    figures into cfg for the decision steps downstream.

    Returns `(cfg, prev_gate)` -- the cfg with gate_state / fleet exposure
    bound in, and the gate state before this sweep advanced it, so the
    outcome can report the transition.
    """
    # Price every fill whose horizon has just matured, then re-read this
    # market's verdict. Both are cheap: sample_due touches only rows already
    # due, and the verdict is a mean over rows we have.
    mids = {m.condition_id: {
        "UP": mid_price(up.get("best_bid"), up.get("best_ask")),
        "DOWN": mid_price(dn.get("best_bid"), dn.get("best_ask"))}}
    markout.sample_due(mids, ctx.now, cfg.markout_horizons)

    stats = markout.per_market_stats(cfg.markout_min_sample).get(
        m.condition_id,
        {"verdict": "insufficient_sample", "mean_per_share": None, "n": 0})
    # FLEET FALLBACK. A market with no verdict of its own used to hold
    # `insufficient_sample` forever, and `gate.next_state` returns the state
    # unchanged on that verdict -- so it sat at NORMAL for its whole life
    # however badly the fleet as a whole was being picked off. Markets here
    # rotate daily and almost none of them individually reach the sample.
    #
    # Only ever consulted when this market has nothing to say. A market with
    # its own matured sample keeps its own verdict, including a GOOD one:
    # the fleet reading must not overrule a market that has demonstrably
    # earned, or one bad universe would evict its own survivors.
    # The fleet fallback and its WIDENED cap live in `_gate_with_fleet_fallback`
    # rather than inline here, and outside `gate.next_state` -- the state
    # machine stays a pure function of one market's stats and knows nothing
    # about where they came from.
    prev_gate = st.gate
    st.gate, stats = _gate_with_fleet_fallback(st.gate, stats, cfg)
    st.markout = stats
    # Persist the moment we give up on a market, and only that moment. Writing
    # every cycle would be one DB write per market per sweep for a value that
    # almost never changes; writing on the transition costs one write, ever,
    # and is the only write a restart actually needs to read back.
    if st.gate == gate.EXITED and prev_gate != gate.EXITED:
        try:
            store.save_gate_state(m.condition_id, st.gate)
        except Exception as e:
            # An unpersisted EXIT still holds for this process. Losing it on a
            # restart is the old behaviour, not a reason to stop trading.
            log.warning("gate persist failed for %s: %s", st.title[:30], e)
        _record_event(st, ctx.now, "EXITED",
                      f"gate EXITED: markout {stats.get('mean_per_share') or 0.0:.4f}/sh "
                      f"on n={stats.get('n', 0)}",
                      reason_code="MARKOUT_EXIT", force=True)
        log.info("GATE EXIT %-28s markout %.4f/sh on n=%d",
                 st.title[:28], stats.get("mean_per_share") or 0.0,
                 stats.get("n", 0))
    # Fleet exposure is a property of every OTHER market as well, so it has to
    # be injected here rather than derived from this market's inventory. The
    # posture is the same kind of fact and arrives the same way -- computed
    # once per sweep from the POOLED markout, because a per-market verdict
    # cannot see a universe where every book is individually fine and every
    # fill is still being bought from someone better informed.
    cfg = replace(cfg, gate_state=st.gate,
                  fleet_naked_usd=ctx.fleet_naked_usd,
                  committed_usd=ctx.committed_usd,
                  fleet_posture=ctx.fleet_posture)
    return cfg, prev_gate


def _manage_exits(st: "MarketState", m, up, dn, cfg,
                  ctx: SweepContext) -> tuple[dict, dict]:
    """Merge and take-profit: the two ways out of a position.

    Returns `(mg, pt)` -- the merge and profit-take verdicts, both always
    set (an exception in either path degrades to a no-take dict carrying the
    error), because the score-and-publish step reports their `why` on the
    live payload.
    """
    # MERGE FIRST, then consider selling. A matched pair redeems for exactly
    # 1.00 through the collateral adapter with no spread and no taker fee, so
    # whenever both exits are available merge strictly dominates: selling the
    # same pair pays 3.4c of fees into a bid sum bounded by 1.00. Running the
    # sell path first would occasionally book a worse exit for no reason.
    #
    # Simulation only in Phase A -- the on-chain executor is U6, and the fleet
    # deliberately does not import it. What this records is what a merge WOULD
    # realize, on the same terms the real one will.
    try:
        # Projected rent comes from this market's MEASURED income, not an
        # assumed rate -- the velocity exception is only as honest as the
        # number backing it. None when we have not scored here yet, which
        # blocks the exception rather than assuming it favourable.
        prev_live = st.spec.get("_live") or {}
        mg = merge.should_merge(
            st.inv, cfg, gas_cost=cfg.merge_gas_usd,
            projected_rent_per_day=prev_live.get("income"),
            hold_days=cfg.merge_velocity_hold_days)
        if mg["take"]:
            n = mg["shares"]
            up_removed, dn_removed = mg["up_cost_removed"], mg["dn_cost_removed"]

            # Ledger first, memory second -- same ordering discipline as the
            # sell path below, and for the same reason: _inventory_from_db
            # rebuilds from this table on restart, so a merge must never exist
            # in memory without also existing on disk.
            store.log_close(
                condition_id=m.condition_id, market_slug=m.market_slug,
                method="merge", gas=mg["gas"], shares=n,
                cost_basis=mg["cost_basis"], proceeds=mg["proceeds"],
                realized_pnl=mg["realized_pnl"],
                # Merging forgoes nothing: parity IS the settlement value, so
                # there is no concession against holding, only the gas.
                forgone_vs_settlement=0.0,
                up_cost_removed=up_removed, dn_cost_removed=dn_removed)
            _record_event(st, ctx.now, "MERGED",
                          f"merged {n:.0f} pairs for ${mg['proceeds']:.2f}",
                          size=n, reason_code="MERGE", force=True)

            # Cost before shares: avg() divides by the share count, so
            # decrementing shares first would rewrite the basis of the residue.
            st.inv.up_cost -= up_removed
            st.inv.down_cost -= dn_removed
            st.inv.up_shares -= n
            st.inv.down_shares -= n
            st.merged_shares += n
            log.info("MERGE %-28s %.0f pairs realized $%+.2f | %s",
                     st.title[:28], n, mg["realized_pnl"], mg["why"])
    except Exception as e:
        log.warning("merge failed on %s: %s: %s",
                    st.title[:30], type(e).__name__, e)
        mg = {"take": False, "why": f"error: {e}"}

    # Take profit on the paired portion, if the market has moved far enough to
    # cover selling both legs and still pay. Wrapped for the same reason the
    # allocator is: a bug in a money-making refinement must not stop the data
    # collection the whole run exists for.
    try:
        # The scarcity flag is the allocator's, computed once per sweep, and it
        # relaxes the close threshold to a slightly negative number. It is
        # passed rather than read off cfg inside should_close so the decision
        # stays a pure function of its arguments.
        pt = profit_take.should_close(st.inv, up.get("bids"),
                                      dn.get("bids"), cfg,
                                      capital_scarce=cfg.capital_scarce)
        if pt["take"]:
            n = pt["shares"]
            # Cost removed must be captured BEFORE the mutations below, since
            # avg("UP")/avg("DOWN") divide by the current share counts.
            up_removed = n * st.inv.avg("UP")
            dn_removed = n * st.inv.avg("DOWN")

            # Write the ledger FIRST, mutate memory SECOND. If log_close
            # throws (disk full, DB locked), the position must still be
            # exactly what the DB says it is -- _inventory_from_db rebuilds
            # from this table on every restart, and that rebuild is only
            # correct if a close is never reflected in memory without also
            # landing in the database first.
            store.log_close(
                condition_id=m.condition_id, market_slug=m.market_slug,
                shares=n, up_price=pt["up_avg_price"],
                dn_price=pt["dn_avg_price"], cost_basis=pt["cost_basis"],
                proceeds=pt["proceeds"], fee=pt["fee"],
                realized_pnl=pt["realized_pnl"],
                forgone_vs_settlement=pt["forgone_vs_settlement"],
                up_cost_removed=up_removed, dn_cost_removed=dn_removed)
            _record_event(st, ctx.now, "EXITED", pt.get("why", ""),
                          size=n, reason_code="EXIT", force=True)

            # Remove the closed pairs at their own average cost, which leaves
            # the average cost of whatever remains unchanged -- the naked
            # residue keeps the basis it actually has.
            #
            # Order matters: avg("UP") divides by up_shares, so the cost must
            # be decremented BEFORE the share count. Reversing these two lines
            # silently rewrites the basis of the remaining shares.
            st.inv.up_cost -= up_removed
            st.inv.down_cost -= dn_removed
            st.inv.up_shares -= n
            st.inv.down_shares -= n
            log.info("CLOSE %-28s %.0f pairs realized $%+.2f | %s",
                     st.title[:28], n, pt["realized_pnl"], pt["why"])
    except Exception as e:
        log.warning("profit_take failed on %s: %s: %s",
                    st.title[:30], type(e).__name__, e)
        pt = {"take": False, "why": f"error: {e}"}
    return mg, pt


def _requote(st: "MarketState", m, up, dn, cfg, ctx: SweepContext,
             committed_states) -> str | None:
    """The quote decision and the size caps that shape it.

    Decides the resting intents, executes emergency-hedge crossings, cancels
    stale or resized orders, and posts the new batch sized to the wallet and
    per-market caps. Records the BLOCKED / QUOTING / WAITING events that
    follow, and returns the final `why` for the live payload.
    """
    # Requote. Long-dated markets never expire mid-session, so t_remaining is
    # effectively infinite and every 5-min timing rule is inert by construction.
    # Short-dated markets (within 1 day of resolution) switch to the spread
    # objective: they pay no rewards worth capturing and the book is about to
    # converge, so resting inside the touch to skim the spread is the only
    # edge left. Driven by the live market clock, not a static ranker tag.
    try:
        _t_rem = m.t_remaining(ctx.now)
    except Exception:
        _t_rem = 1e9
    if 0.0 < _t_rem <= 86400.0 and cfg.objective != "spread":
        cfg = replace(cfg, objective="spread")
    intents, why = decide_quotes(cfg, up, dn, st.inv, 1e9, None)

    # QUOTE UPTIME INSTRUMENTATION (Plan 04, harden-to-reality). When the
    # decision layer rests NOTHING and names a reason, record it so the
    # dashboard can show WHY a market is dark -- not just that it is. The
    # pair-cost guard and emergency hedge already log their own decisions;
    # this catches the remaining resting-quote skips (band, cost cap, timing,
    # fresh-market, hedged-out) that previously vanished into a returned
    # string. log_decision collapses consecutive identical skips per market.
    if not intents and why:
        store.log_decision(
            market_slug=m.market_slug, condition_id=m.condition_id,
            action="SKIP_REST", side="", price=None, mid=None,
            edge_vs_mid=None, t_remaining=None,
            balance=st.inv.balance, pair_cost=st.inv.pair_cost(),
            reason=why, reason_code="NO_QUOTE",
        )

    # An emergency-hedge intent is a TAKER order and must not be posted as a
    # resting bid. Under the queue fill model a lone bid at the ask has nothing
    # queued at its price, so no bid-side delta can ever be attributed to it --
    # it would fill 0 shares while the book traded straight through, and the
    # stop-loss would silently do nothing at all. That exact bug is documented
    # on QueueFillEngine.cross(), which is the correct primitive here: consume
    # real ask depth at real prices and accept a partial fill as a real result.
    crossing = [qi for qi in intents if qi.crossed]
    intents = [qi for qi in intents if not qi.crossed]
    if crossing:
        # A taker hedge is an exit action, not an additional resting position.
        # Release every open bid before measuring affordability so stale offers
        # cannot consume capacity and incorrectly block the hedge. The next
        # requote pass below may restore only the intents that still qualify.
        released = st.engine.open_orders()
        for o in released:
            o.cancelled = True
        store.mark_cancelled([o.quote_id for o in released
                              if o.quote_id is not None])

    for qi in crossing:
        book = up if qi.side == "UP" else dn
        asks = book.get("asks") or {}
        # Emergency hedges are the only path that can add inventory without
        # going through the resting-order reservation below. Cap them too:
        # the stop-loss may take a partial hedge, but it must never turn a
        # $1,000 wallet into a larger simulated position.
        available = max(cfg.max_committed_usd
                       - fleet_committed_cost(committed_states), 0.0)
        cross_size = _affordable_cross_size(asks, qi.size, available)
        if cross_size <= 1e-9:
            block_reason = f"{qi.reason}; committed cap leaves no affordable hedge"
            store.log_decision(
                market_slug=m.market_slug, condition_id=m.condition_id,
                action="EMERGENCY_HEDGE_BLOCKED", side=qi.side,
                price=qi.price, mid=qi.mid, edge_vs_mid=qi.edge_vs_mid,
                t_remaining=None, balance=st.inv.balance,
                pair_cost=st.inv.pair_cost(), reason=block_reason,
                reason_code="COMMITTED_CAP",
            )
            _record_event(st, ctx.now, "BLOCKED", block_reason, side=qi.side,
                          price=qi.price, reason_code="COMMITTED_CAP")
            continue
        got = 0.0
        qid = store.log_quote(
            market_slug=m.market_slug, condition_id=m.condition_id,
            token_id=qi.token_id, side=qi.side, price=qi.price, size=cross_size,
            queue_ahead=0.0, mid=qi.mid, edge_vs_mid=qi.edge_vs_mid,
            t_remaining=None,
        )
        for f in st.engine.cross(qi.token_id, qi.side, cross_size,
                                 asks, ctx.now):
            if f.side == "UP":
                st.inv.up_shares += f.size
                st.inv.up_cost += f.size * f.price
            else:
                st.inv.down_shares += f.size
                st.inv.down_cost += f.size * f.price
            st.inv.fills += 1
            got += f.size
            # crossed=True is load-bearing downstream: kpi.py excludes these
            # from the maker fill rate and charges them the taker fee. A
            # crossed lot recorded as a maker fill would flatter both numbers.
            store.log_fill(
                quote_id=qid, market_slug=m.market_slug,
                condition_id=m.condition_id, token_id=f.token_id,
                side=f.side, price=f.price, size=f.size, mid_at_post=qi.mid,
                edge_vs_mid=None, queue_waited=0.0, seconds_to_fill=0.0,
                crossed=True, reason=f.reason,
            )
        # A shallow ask can leave a residual portion of the capped cross
        # unfilled. It was never a resting order, so close its quote row ctx.now;
        # otherwise historical open-offer metrics overstate live exposure.
        if got + 1e-9 < cross_size:
            store.mark_cancelled([qid])
        hedge_reason = (f"{qi.reason}; filled {got:.0f}/{cross_size:.0f}sh "
                        f"(requested {qi.size:.0f})")
        store.log_decision(
            market_slug=m.market_slug, condition_id=m.condition_id,
            action="EMERGENCY_HEDGE", side=qi.side, price=qi.price,
            mid=qi.mid, edge_vs_mid=qi.edge_vs_mid, t_remaining=None,
            balance=st.inv.balance, pair_cost=st.inv.pair_cost(),
            reason=hedge_reason, reason_code="HEDGE",
        )
        _record_event(st, ctx.now, "HEDGED", hedge_reason, side=qi.side,
                      size=got, reason_code="HEDGE", force=True)
        log.info("EMERGENCY_HEDGE %-28s %-4s %.0f/%.0fsh bal=%.2f",
                 st.title[:28], qi.side, got, qi.size, st.inv.balance)

    # CLOSE-WINDOW BALANCE HEDGE. When a market is within `balance_hedge_sec`
    # of resolution, resting orders will not have time to fill and flatten a
    # lopsided position, so a naked leg would resolve at a guaranteed $1.00
    # loss on the unhedged side. Cross the LIGHT side to bring the book back
    # toward balance -- a true taker cross via engine.cross() (not a passive
    # bid at the ask, which the queue model never fills), matching the
    # emergency-hedge primitive. Long-dated markets report t_remaining in the
    # billions, so this path is inert there and only fires as close approaches.
    try:
        t_rem = m.t_remaining(ctx.now)
    except Exception:
        t_rem = 1e9
    if 0.0 < t_rem <= cfg.balance_hedge_sec and st.inv.balance < cfg.target_balance:
        light = "UP" if st.inv.up_shares < st.inv.down_shares else "DOWN"
        heavy = "DOWN" if light == "UP" else "UP"
        gap = abs(st.inv.up_shares - st.inv.down_shares)
        book = up if light == "UP" else dn
        asks = book.get("asks") or {}
        available = max(cfg.max_committed_usd
                       - fleet_committed_cost(committed_states), 0.0)
        cross_size = _affordable_cross_size(asks, gap, available)
        if cross_size > 1e-9:
            qid = store.log_quote(
                market_slug=m.market_slug, condition_id=m.condition_id,
                token_id=book.get("token_id"), side=light, price=book.get("best_ask"),
                size=cross_size, queue_ahead=0.0, mid=book.get("best_ask"),
                edge_vs_mid=None, t_remaining=t_rem,
            )
            got = 0.0
            for f in st.engine.cross(book.get("token_id"), light, cross_size,
                                     asks, ctx.now):
                if f.side == "UP":
                    st.inv.up_shares += f.size
                    st.inv.up_cost += f.size * f.price
                else:
                    st.inv.down_shares += f.size
                    st.inv.down_cost += f.size * f.price
                st.inv.fills += 1
                got += f.size
                store.log_fill(
                    quote_id=qid, market_slug=m.market_slug,
                    condition_id=m.condition_id, token_id=f.token_id,
                    side=f.side, price=f.price, size=f.size, mid_at_post=book.get("best_ask"),
                    edge_vs_mid=None, queue_waited=0.0, seconds_to_fill=0.0,
                    crossed=True, reason="close-window balance hedge",
                )
            if got + 1e-9 < cross_size:
                store.mark_cancelled([qid])
            hedge_reason = (f"close-window hedge {light} {got:.0f}/{cross_size:.0f}sh "
                            f"(t_rem {t_rem:.0f}s <= {cfg.balance_hedge_sec:.0f}s, "
                            f"bal {st.inv.balance:.2f})")
            store.log_decision(
                market_slug=m.market_slug, condition_id=m.condition_id,
                action="CROSS_HEDGE", side=light, price=book.get("best_ask"),
                mid=book.get("best_ask"), edge_vs_mid=None, t_remaining=t_rem,
                balance=st.inv.balance, pair_cost=st.inv.pair_cost(),
                reason=hedge_reason, reason_code="HEDGE",
            )
            _record_event(st, ctx.now, "HEDGED", hedge_reason, side=light,
                          size=got, reason_code="HEDGE", force=True)
            log.info("CROSS_HEDGE %-28s %-4s %.0f/%.0fsh t_rem=%.0f",
                     st.title[:28], light, got, cross_size, t_rem)

    # Cancel stale or resized orders before reserving the next batch. Keeping
    # an old-size order when the allocator just reduced `quote_shares` makes
    # the allocation advisory rather than a capital limit.
    want = {qi.side: qi for qi in intents}
    keep = set()
    cancelled = []
    for o in st.engine.open_orders():
        qi = want.get(o.side)
        if (qi is not None and round(qi.price, 4) == o.price
                and o.size == qi.size):
            keep.add(o.side)      # leave it alone: requoting loses queue position
        else:
            o.cancel(ts=ctx.now, reason="requote")
            cancelled.append(o.quote_id)
    store.mark_cancelled([qid for qid in cancelled if qid is not None])

    # `committed_usd` was sampled before this sweep. It is useful for the
    # decision layer, but it cannot reserve the order we are about to add.
    # Enforce the hard wallet cap against the post-cancellation state and size
    # each new order to the remaining dollars. A final remainder below the
    # venue's minimum is left idle rather than creating a quote that scores 0.
    available = max(cfg.max_committed_usd
                       - fleet_committed_cost(committed_states), 0.0)
    # DEFENSIVE WALLET CLAMP (harden-to-reality). The venue holds collateral
    # against every open bid; on live Polymarket you cannot have open quotes
    # whose notional exceeds the wallet. fleet_committed_cost already sums the
    # fleet, but if that sampling ever under-counts (stale state, missed
    # market), this floor guarantees no single sweep can push exposure past
    # the wallet -- matching live margin enforcement at the source.
    available = min(available, cfg.max_committed_usd)
    budget_blocked: list[str] = []
    for qi in intents:
        if qi.side in keep:
            continue
        if qi.price <= 0:
            continue
        # PER-MARKET NOTIONAL CAP. `max_cost_per_market` was enforced only in
        # quotes.py, against `inv.cost` -- the inventory we ALREADY hold -- and
        # additionally only on the heavy side (`and mine >= theirs`). Both
        # readings are post-hoc: a market holding nothing has inv.cost 0, so a
        # first order of any size passes. Measured 2026-08-02, one 900-share
        # order rested and filled in a single print for $792 against a $400
        # cap, on a market whose inventory was empty when it was posted.
        #
        # The binding quantity is what we hold PLUS what this order would add,
        # and it has to be checked here, where the size is actually chosen.
        # Sized down rather than skipped: a market at $380 of $400 can still
        # carry a smaller order, and the min_quote_shares floor below already
        # refuses the remainder if what is left cannot score.
        room = max(cfg.max_cost_per_market - st.inv.cost, 0.0)
        size = _affordable_rest_size(qi.size, qi.price, available, room)
        if size < cfg.min_quote_shares:
            # Name which cap bound. An operator reading "leaves 0sh" has to be
            # able to tell a fleet-wide wallet limit from this market's own
            # cost cap, or the dashboard shows a dead market with no cause.
            which = ("market cost cap" if room <= available else "committed cap")
            budget_blocked.append(f"{qi.side}: {which} leaves "
                                 f"{size:.0f}sh < {cfg.min_quote_shares} minimum")
            continue
        book = up if qi.side == "UP" else dn
        # QUEUE-DEPTH GATE (harden-to-reality). Resting behind a deep queue
        # almost never fills at the front, and when it does it is a sweep
        # through informed flow = adverse selection. Refuse to post when the
        # book is deeper than max_rest_queue_ahead at our price. 0 disables.
        if cfg.max_rest_queue_ahead > 0:
            q_ahead = float(book["bids"].get(round(qi.price, 4), 0.0) or 0.0)
            if q_ahead > cfg.max_rest_queue_ahead:
                budget_blocked.append(
                    f"{qi.side}: queue {q_ahead:.0f}sh > "
                    f"{cfg.max_rest_queue_ahead:.0f}sh max at {qi.price:.3f}")
                continue
        o = st.engine.post(qi.token_id, qi.side, qi.price, size, book["bids"], ctx.now)
        available -= o.price * o.size
        o.quote_id = store.log_quote(
            market_slug=m.market_slug, condition_id=m.condition_id,
            token_id=qi.token_id, side=qi.side, price=qi.price, size=size,
            queue_ahead=o.queue_ahead, mid=qi.mid, edge_vs_mid=qi.edge_vs_mid,
            t_remaining=None,
        )
    if budget_blocked:
        why = "; ".join(x for x in (why, *budget_blocked) if x)

    # Preserve the refusal evidence even when the opposite side remains live.
    # A market can be actively quoting one side while the risk engine refuses
    # the other; showing only QUOTING would hide the gate that shaped it.
    if why:
        _record_event(st, ctx.now, "BLOCKED", why,
                      reason_code=store.reason_code(why))

    open_orders = st.engine.open_orders()
    if open_orders:
        sides = "+".join(sorted({o.side for o in open_orders}))
        # If a fill/exit/hedge happened in this sweep, _record_event
        # deliberately keeps that higher-signal action as the latest visible
        # event.
        _record_event(st, ctx.now, "QUOTING", f"resting {sides} limit orders",
                      reason_code="QUOTE_ACTIVE")
    elif not why:
        _record_event(st, ctx.now, "WAITING", "no eligible quote intent",
                      reason_code="NO_QUOTE")
    return why


def _score_and_publish(st: "MarketState", m, up, dn, cfg, ctx: SweepContext,
                       mg: dict, pt: dict, why, pair: dict | None = None) -> None:
    """Measure the reward share and write the market's live payload."""
    bq1, bq2 = rewards.book_scores(up, dn, cfg.max_spread_from_mid,
                                   cfg.min_quote_shares)
    oq1, oq2 = rewards.our_scores(st.engine.open_orders(), up, dn,
                                  cfg.max_spread_from_mid, cfg.min_quote_shares)
    ours, theirs, share = rewards.share_of_pool(oq1, oq2, bq1, bq2)
    # Feed the rolling window the allocator averages over, so sizing responds
    # to the competition's typical depth rather than to one lucky snapshot.
    st.observe_theirs(ctx.now, theirs, cfg.rank_sample_window_sec)

    # HEDGE CENSUS (U35), recorded every sweep: was a fillable sub-$1.00 pair
    # present at the touch? The table and its Phase A census reader were
    # written for this run and never switched on -- 0 rows through 112 hours
    # -- and the pairs-only rule's completion rate is only interpretable
    # against it. `pair_cost_at_touch = ask + ask - reward_offset`: the pair
    # cost if we rest one offset under each ask, the same basis the census
    # comment defines. A missing ask on either side is a data gap, not a
    # fillable-pair reading -- skipped, exactly like the volume tracker.
    try:
        up_ask = up.get("best_ask")
        dn_ask = dn.get("best_ask")
        if up_ask is not None and dn_ask is not None:
            pair_cost_at_touch = up_ask + dn_ask - cfg.reward_offset
            store.record_hedge_census(
                m.condition_id, m.market_slug, up_ask, dn_ask,
                pair_cost_at_touch,
                pair_cost_at_touch < cfg.max_pair_cost, ctx.now)
    except Exception as e:
        # A telemetry write must never stop the trading loop.
        log.warning("hedge census failed for %s: %s", st.title[:30], e)
    store.log_reward_sample(
        ts=ctx.now, market_slug=m.market_slug, condition_id=m.condition_id,
        our_score=ours, market_score=theirs,
        offset_c=100 * cfg.reward_offset,
        n_sides=len({o.side for o in st.engine.open_orders()}),
    )

    # Everything below is expressed on ONE price axis: the UP price. A bid on
    # DOWN at p is economically an offer to sell UP at 1-p, so folding it onto
    # the UP axis puts both of our orders on the same line and makes the shape
    # of the position visible -- our bid below mid, our effective ask above it,
    # straddling symmetrically. Two separate books hide that.
    orders = st.engine.open_orders()
    our_up = next((o.price for o in orders if o.side == "UP"), None)
    our_dn = next((o.price for o in orders if o.side == "DOWN"), None)
    up_bid, up_ask = up.get("best_bid"), up.get("best_ask")
    dn_bid, dn_ask = dn.get("best_bid"), dn.get("best_ask")

    st.spec["_live"] = {
        "alloc": getattr(st, "alloc_verdict", None),
        "share": share, "ours": ours, "theirs": theirs,
        # Projected income at the CURRENT score share, off whichever pot pays
        # this market. Reading `daily` here reported $0.00/day for every
        # spread market, which is true of its rent and false of its income.
        "income": share * st.pot,
        "pot": st.pot, "source": st.source,
        "capital": sum(o.price * (o.size - o.filled) for o in orders),
        "quotes": [{"side": o.side, "price": round(o.price, 4),
                     "size": o.size, "filled": o.filled,
                     "remaining": max(0.0, o.size - o.filled),
                     "notional": round(o.price * max(0.0, o.size - o.filled), 4)}
                    for o in orders],
        "up_sh": st.inv.up_shares, "dn_sh": st.inv.down_shares,
        "up_avg": st.inv.avg("UP"), "dn_avg": st.inv.avg("DOWN"),
        # Paired shares are safe: one YES + one NO always pays exactly $1.00,
        # so what matters is the leftover. NAKED shares are the only thing that
        # can lose -- they pay $1 or $0 on resolution, nothing in between.
        "paired": min(st.inv.up_shares, st.inv.down_shares),
        "naked_side": ("UP" if st.inv.up_shares > st.inv.down_shares
                       else ("DOWN" if st.inv.down_shares > st.inv.up_shares else "")),
        "naked_sh": abs(st.inv.up_shares - st.inv.down_shares),
        "naked_cost": (abs(st.inv.up_shares - st.inv.down_shares)
                       * (st.inv.avg("UP") if st.inv.up_shares > st.inv.down_shares
                          else st.inv.avg("DOWN"))),
        "pair_paid": (min(st.inv.up_shares, st.inv.down_shares)
                      * (st.inv.avg("UP") + st.inv.avg("DOWN"))),
        # U35: what the pairs-only rule did with a one-sided fill this sweep
        # (complete | exit | expired | deferred | balanced | disabled).
        "pairs_rule": pair or {"action": "none"},
        "gate": st.gate,
        # Surfaced because it silently changes the close threshold: a close
        # booked at -0.3c/sh is correct under scarcity and a bug without it,
        # and the dashboard cannot tell the two apart from the P&L alone.
        "capital_scarce": cfg.capital_scarce,
        "markout": st.markout.get("mean_per_share"),
        "markout_n": st.markout.get("n", 0),
        "close_why": pt.get("why", ""),
        # Merge, reported separately from the sell path. Recycled capital is
        # the number that distinguishes this strategy from a carry trade: it
        # is money that went back to work rather than sitting until 2027.
        "merge_why": mg.get("why", ""),
        "merged_shares": st.merged_shares,
        "recycled_usd": st.merged_shares * merge.PARITY,
        # Merged pairs against shares filled -- the assumption merge economics
        # rest on. None until something fills; no observation is not a zero.
        "pairing_rate": merge.pairing_rate(
            st.merged_shares, st.engine.filled_shares(include_crossed=False)),
        "fills": st.inv.fills, "err": st.err, "ts": ctx.now,
        "up_bid": up_bid, "up_ask": up_ask,
        "dn_bid": dn_bid, "dn_ask": dn_ask,
        "mid_up": ((up_bid + up_ask) / 2.0) if (up_bid and up_ask) else None,
        "our_up": our_up,
        # our DOWN bid, drawn on the UP axis
        "our_dn_as_up": (round(1.0 - our_dn, 4) if our_dn is not None else None),
        # market's own best DOWN bid, also on the UP axis: this is the price
        # someone else is already willing to sell UP at.
        "dn_bid_as_up": (round(1.0 - dn_bid, 4) if dn_bid else None),
        "max_spread": cfg.max_spread_from_mid,
        "pair_cost": (round(our_up + our_dn, 4)
                      if (our_up is not None and our_dn is not None) else None),
        "why": why,
        "stale": False,
    }


def sweep(state: "MarketState", ctx: SweepContext) -> SweepOutcome:
    """One poll of one market, behind the sweep's one interface.

    Books -> fills -> gate -> exits -> requote -> reward sample, with the
    fleet loop reduced to orchestration around it. Each early-exit path maps
    to an outcome status so the caller (and a test) can assert what happened
    without reaching into the engine. The `state` is mutated in place -- the
    live payload, inventory, gate and engine are all on it -- and the outcome
    is the read-only summary of what that mutation did.
    """
    if state.cid in ctx.resolved_cids:
        # Settled by the venue -- its book is gone, so there is nothing left
        # to poll. Checked before anything else touches the network.
        released = _settle_resolved(state, ctx.now)
        return SweepOutcome(status="SETTLED", prev_gate=state.gate,
                            gate=state.gate, why="", fills=0,
                            requoted=False, released=released)

    cfg = state.cfg
    if not _identity_gate(state, ctx.now):
        return SweepOutcome(status="IDENTITY_BLOCKED", prev_gate=state.gate,
                            gate=state.gate, why=state.err, fills=0,
                            requoted=False)

    # The single-market helper remains callable in tests; the fleet runner
    # passes the complete state list so emergency-hedge affordability and
    # resting-order reservation use the same fleet-wide committed total.
    committed_states = ctx.states if ctx.states is not None else [state]

    m = _load_market(state, ctx.now)
    if isinstance(m, str):
        return SweepOutcome(status=m, prev_gate=state.gate, gate=state.gate,
                            why=state.err, fills=0, requoted=False)

    books = _load_books(state, m, cfg, ctx)
    if isinstance(books, str):
        return SweepOutcome(status=books, prev_gate=state.gate,
                            gate=state.gate, why=state.err, fills=0,
                            requoted=False)
    up, dn = books

    fills = _process_fills(state, m, up, dn, ctx.now)
    # The pairs-only rule runs before the gate and exits so a pair completed
    # here is merged (or a naked leg exited) in the SAME sweep's `_manage_exits`
    # -- waiting a full rotation would leave the completed pair -- or the
    # naked residue -- sitting for another 30-60s for no reason.
    pair = _apply_pairs_rule(state, m, up, dn, cfg, ctx, committed_states)
    cfg, prev_gate = _advance_gate(state, m, up, dn, cfg, ctx)
    mg, pt = _manage_exits(state, m, up, dn, cfg, ctx)
    why = _requote(state, m, up, dn, cfg, ctx, committed_states)
    # Persist any buffered decision rows (log_decision collapses consecutive
    # identical actions into one in-memory run) before the dashboard reads
    # them back -- a CROSS_HEDGE or EMERGENCY_HEDGE logged in _requote must
    # reach the decisions table this sweep, not wait for a later key change.
    store.flush_decision(force=True)
    _score_and_publish(state, m, up, dn, cfg, ctx, mg, pt, why, pair)

    open_orders = state.engine.open_orders()
    if open_orders:
        status = "QUOTING"
    elif why:
        status = "BLOCKED"
    else:
        status = "WAITING"
    return SweepOutcome(status=status, prev_gate=prev_gate, gate=state.gate,
                        why=why or "", fills=fills,
                        requoted=bool(open_orders))
