"""One process, many markets.

Breadth beats depth here. Income is `rate * ours/(ours+theirs)` per market, so
piling capital into one book fights yourself over a fixed pot -- measured,
$3,000 into a single market returns ~1.0%/day while the same money spread over
20 markets returns ~5%/day, because each market is a separate pot with its own
competition.

Why one process rather than 20 copies of strategy.main: 20 markets x 2 books
polled every second is 40 requests/second against a public API. That gets
rate-limited, and the failure mode is silent -- the bots keep reporting uptime
while scoring nothing. Here markets are visited on a rotation with a fixed
request budget, so adding markets lengthens the sweep instead of raising the
request rate.

    python -m scripts.rank_markets --top 20    # choose markets first
    python -m strategy.fleet
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import replace
from pathlib import Path

from strategy import gate, markout, profit_take, rewards, store
from strategy.allocate import allocate, capital_scarcity, shares_for
from strategy.config import load as load_cfg
from strategy.fills import QueueFillEngine
from strategy.main import full_book, recent_trades
from strategy.markets import fetch_pinned_market
from strategy.net_config import load_net as load_bot_cfg
from strategy.quotes import Inventory, decide_quotes, mid_price

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "run"
(ROOT / "logs").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.FileHandler(ROOT / "logs" / "fleet.log", encoding="utf-8"),
              logging.StreamHandler()])
log = logging.getLogger("fleet")

# Two book requests per market visit. This budget keeps us far under any sane
# public rate limit even at 40 markets.
REQ_PER_SEC = 2.0


def _inventory_from_db(cid: str) -> Inventory:
    """Rebuild a market's share position from its persisted fills.

    The fills table is the ledger; Inventory was only ever a running total of
    it held in memory. Recomputing from the ledger on startup makes the two
    agree, which is the difference between a dashboard that says "no position"
    and one that shows the shares we are actually holding.

    Returns an empty Inventory on any failure -- a fresh DB, a missing table.
    That is the same state as before this function existed, so a broken read
    degrades to the old behaviour rather than stopping the fleet.
    """
    inv = Inventory()
    try:
        with store.db() as c:
            for side, size, price in c.execute(
                    "SELECT side, size, price FROM fills WHERE condition_id=?",
                    (cid,)):
                if side == "UP":
                    inv.up_shares += size or 0.0
                    inv.up_cost += (size or 0.0) * (price or 0.0)
                else:
                    inv.down_shares += size or 0.0
                    inv.down_cost += (size or 0.0) * (price or 0.0)
                inv.fills += 1
            for shares, cost_basis, up_removed, dn_removed in c.execute(
                    "SELECT shares, cost_basis, up_cost_removed, "
                    "dn_cost_removed FROM closes WHERE condition_id=?",
                    (cid,)):
                # A close removed one UP and one DOWN share per pair, each at
                # its OWN average cost at close time -- not in proportion to
                # share counts, which only coincides with the true split when
                # both legs happen to share the same average price. The exact
                # per-leg amounts removed are recorded on the row, so use them
                # directly instead of re-deriving (and getting wrong) a split.
                n = shares or 0.0
                inv.up_shares -= n
                inv.down_shares -= n
                if up_removed is not None and dn_removed is not None:
                    inv.up_cost -= up_removed
                    inv.down_cost -= dn_removed
                else:
                    # Row written before up_cost_removed/dn_cost_removed
                    # existed: fall back to the old (approximate) even split
                    # rather than crashing on a NULL.
                    inv.up_cost -= (cost_basis or 0.0) * 0.5
                    inv.down_cost -= (cost_basis or 0.0) * 0.5
    except Exception as e:
        log.warning("inventory rehydrate failed for %s: %s", cid[:10], e)
    return inv


def _gate_from_db(cid: str) -> str:
    """The persisted gate verdict, defaulting to NORMAL.

    Only EXITED is honoured. A stored NORMAL/WIDENED is treated as absent, so
    the state machine recomputes it from this run's markout instead of carrying
    a mid-graduation position across a restart it knows nothing about.

    Degrades to NORMAL on any failure -- a fresh DB, a table that predates this
    feature. That is the behaviour from before gate persistence existed, so a
    broken read costs us the old bug rather than the whole fleet.
    """
    try:
        return gate.EXITED if store.get_gate_state(cid) == gate.EXITED else gate.NORMAL
    except Exception as e:
        log.warning("gate rehydrate failed for %s: %s", cid[:10], e)
        return gate.NORMAL


class MarketState:
    """Per-market state -- everything that was module-level in the single loop."""

    def __init__(self, spec: dict, base_cfg):
        self.spec = spec
        self.cid = spec["cid"]
        self.title = spec["title"]
        self.daily = spec["daily"]
        # Each market publishes its own reward window, minimum order size and
        # tick. Quoting under a market's min_size scores exactly zero, so these
        # are load-bearing, not cosmetic.
        self.cfg = replace(
            base_cfg,
            objective="rewards",
            min_quote_shares=int(spec["min_size"]),
            quote_shares=int(spec["shares"]),
            max_spread_from_mid=spec["max_spread"] / 100.0,
            price_tick=float(spec["tick"]),
            min_t_remaining_sec=0.0,
            market_title=spec["title"],
            market_daily_rate=spec["daily"],
        )
        self.market = None
        self.engine = QueueFillEngine()
        # Rehydrate from the fills table instead of starting at zero. Fills are
        # persisted, inventory was not, so every restart silently dropped the
        # position while the DB kept the fills -- the dashboard then reported
        # "no position" against 77 recorded fills. Open orders are NOT restored
        # (the venue would not have them after a restart either); only shares
        # already bought, which are the part that can still lose money.
        self.inv = _inventory_from_db(self.cid)
        self.seen_trades: set = set()
        self.tape_primed = False
        self.err = ""
        # Rehydrate an EXITED verdict, and only an EXITED verdict.
        #
        # This used to start every market at NORMAL on the argument that a
        # restart must not inherit a stale judgement. The argument is backwards
        # for this particular state. EXITED is not a guess -- it is the
        # conclusion of `markout_min_sample` fills proving the market takes
        # money off us, and a process restart is not new information about the
        # market. Starting fresh meant every restart re-entered every toxic
        # market and bought that same evidence a second time, which is the one
        # cost the gate exists to stop us paying twice.
        #
        # NORMAL and WIDENED are deliberately not restored: they are cheap to
        # recompute (one sample, at an offset that still earns rent) and both
        # are recoverable, so inheriting them buys nothing. EXITED is the
        # asymmetric one, and it is terminal by design -- `next_state` never
        # leaves it -- so there is no stale-verdict risk to trade off.
        self.gate = _gate_from_db(self.cid)
        self.markout: dict = {"verdict": "insufficient_sample",
                              "mean_per_share": None, "n": 0}


def load_specs() -> list[dict]:
    f = RUN / "markets.json"
    if not f.exists():
        raise SystemExit("run/markets.json missing -- run: "
                         "python -m scripts.rank_markets")
    return json.loads(f.read_text(encoding="utf-8"))


def fleet_naked_cost(states) -> float:
    """Dollars of unhedged inventory across the whole fleet.

    The unpaired leg is the only thing that can lose: a matched pair always
    pays $1. Cost is used rather than share count because $1 of exposure at
    0.85 and at 0.26 are the same amount of money at risk, while 100 shares of
    each are not.
    """
    total = 0.0
    for s in states:
        naked = abs(s.inv.up_shares - s.inv.down_shares)
        if naked <= 0:
            continue
        avg = (s.inv.avg("UP") if s.inv.up_shares > s.inv.down_shares
               else s.inv.avg("DOWN"))
        total += naked * (avg or 0.0)
    return total


def reallocate(states, base) -> dict:
    """Resize every market by marginal return instead of a flat 120 shares.

    Measured 2026-07-29, the flat size produced returns spanning 27.58%/day to
    0.28%/day on identical $115 stakes -- because income is pot x share, and
    share is set by the competition, not by the pot. Big pots are big precisely
    because makers crowd them.

    Runs only on markets that have reported a live share; a market we have not
    measured yet keeps its current size rather than being sized off a guess.
    Markets the allocator funds below their min_size get 0 and stop quoting,
    which is the intended outcome -- capital in a market returning 0.3%/day is
    worse than capital sitting idle.
    """
    obs = []
    for s in states:
        live = s.spec.get("_live") or {}
        share, capital = live.get("share"), live.get("capital")
        if not share or not capital:
            continue
        obs.append({"cid": s.cid, "daily": s.daily,
                    "capital": capital, "share": share})
    if not obs:
        return {}

    dollars = allocate(obs, base.allocation_budget,
                       base.marginal_return_floor)

    # Whether the BUDGET, rather than the floor, is what stopped the water-fill
    # while a market was still returning well above the floor. That is the only
    # condition under which holding a stagnant pair has a measurable
    # alternative use, and it is what licenses profit_take's relaxed threshold.
    # Computed once per sweep, on the same observation set the sizing used, so
    # the flag and the allocation cannot disagree.
    scarce = capital_scarcity(obs, dollars, base.allocation_budget,
                              base.marginal_return_floor,
                              base.scarcity_marginal_multiple)

    out = {}
    for s in states:
        # Every market learns the fleet-wide flag, including the ones the
        # allocator did not fund this sweep -- an unfunded market is precisely
        # one whose locked capital we most want released.
        s.cfg = replace(s.cfg, capital_scarce=scarce)
        if s.cid not in dollars:
            continue
        n = shares_for(dollars[s.cid], int(s.spec["min_size"]))
        out[s.cid] = n
        # quote_shares drives size; min_quote_shares is the venue's scoring
        # floor and must not be raised above it, or we would quote below our
        # own threshold and score zero.
        s.cfg = replace(s.cfg, quote_shares=max(n, 0))
    return out


def visit(st: MarketState, bot_cfg, now: float,
          fleet_naked_usd: float = 0.0) -> None:
    """One poll of one market: books -> fills -> requote -> reward sample."""
    cfg = st.cfg
    if st.market is None:
        st.market = fetch_pinned_market(st.cid)
        if st.market is None:
            st.err = "not funded / not accepting orders"
            return
    m = st.market

    try:
        up = full_book(bot_cfg.clob_host, m.up_token)
        dn = full_book(bot_cfg.clob_host, m.down_token)
    except Exception as e:
        st.err = f"book fetch: {e}"
        return
    st.err = ""

    # Fills are decided by the TAPE, not by the book emptying: a level that
    # vanishes on cancellations must fill us nothing.
    tape = recent_trades(m.condition_id, st.seen_trades)
    first_pass = not st.tape_primed
    st.tape_primed = True
    for book in (up, dn):
        traded = tape.get(book["token_id"]) if tape else None
        fills = st.engine.on_book(book["token_id"], book["bids"], now,
                                  traded=None if first_pass else traded)
        for f in fills:
            if f.side == "UP":
                st.inv.up_shares += f.size
                st.inv.up_cost += f.size * f.price
            else:
                st.inv.down_shares += f.size
                st.inv.down_cost += f.size * f.price
            st.inv.fills += 1
            store.log_fill(
                market_slug=m.market_slug, condition_id=m.condition_id,
                token_id=f.token_id, side=f.side, price=f.price, size=f.size,
                quote_id=None, mid_at_post=None, edge_vs_mid=None,
                queue_waited=getattr(f, "queue_waited", 0.0),
                seconds_to_fill=0.0, crossed=False, reason=f.reason,
            )
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

    # Price every fill whose horizon has just matured, then re-read this
    # market's verdict. Both are cheap: sample_due touches only rows already
    # due, and the verdict is a mean over rows we have.
    mids = {m.condition_id: {
        "UP": mid_price(up.get("best_bid"), up.get("best_ask")),
        "DOWN": mid_price(dn.get("best_bid"), dn.get("best_ask"))}}
    markout.sample_due(mids, now, cfg.markout_horizons)

    stats = markout.per_market_stats(cfg.markout_min_sample).get(
        m.condition_id,
        {"verdict": "insufficient_sample", "mean_per_share": None, "n": 0})
    prev_gate = st.gate
    st.gate = gate.next_state(st.gate, stats, cfg)
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
        log.info("GATE EXIT %-28s markout %.4f/sh on n=%d",
                 st.title[:28], stats.get("mean_per_share") or 0.0,
                 stats.get("n", 0))
    # Fleet exposure is a property of every OTHER market as well, so it has to
    # be injected here rather than derived from this market's inventory.
    cfg = replace(cfg, gate_state=st.gate,
                  fleet_naked_usd=fleet_naked_usd)

    # Take profit on the paired portion, if the market has moved far enough to
    # cover selling both legs and still pay. Wrapped for the same reason
    # `reallocate` is: a bug in a money-making refinement must not stop the
    # data collection the whole run exists for.
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

    # Requote. Long-dated markets never expire mid-session, so t_remaining is
    # effectively infinite and every 5-min timing rule is inert by construction.
    intents, why = decide_quotes(cfg, up, dn, st.inv, 1e9, None)

    # An emergency-hedge intent is a TAKER order and must not be posted as a
    # resting bid. Under the queue fill model a lone bid at the ask has nothing
    # queued at its price, so no bid-side delta can ever be attributed to it --
    # it would fill 0 shares while the book traded straight through, and the
    # stop-loss would silently do nothing at all. That exact bug is documented
    # on QueueFillEngine.cross(), which is the correct primitive here: consume
    # real ask depth at real prices and accept a partial fill as a real result.
    crossing = [qi for qi in intents if qi.crossed]
    intents = [qi for qi in intents if not qi.crossed]
    for qi in crossing:
        book = up if qi.side == "UP" else dn
        got = 0.0
        qid = store.log_quote(
            market_slug=m.market_slug, condition_id=m.condition_id,
            token_id=qi.token_id, side=qi.side, price=qi.price, size=qi.size,
            queue_ahead=0.0, mid=qi.mid, edge_vs_mid=qi.edge_vs_mid,
            t_remaining=None,
        )
        for f in st.engine.cross(qi.token_id, qi.side, qi.size,
                                 book.get("asks") or {}, now):
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
        store.log_decision(
            market_slug=m.market_slug, condition_id=m.condition_id,
            action="EMERGENCY_HEDGE", side=qi.side, price=qi.price,
            mid=qi.mid, edge_vs_mid=qi.edge_vs_mid, t_remaining=None,
            balance=st.inv.balance, pair_cost=st.inv.pair_cost(),
            reason=f"{qi.reason}; filled {got:.0f}/{qi.size:.0f}sh",
        )
        log.info("EMERGENCY_HEDGE %-28s %-4s %.0f/%.0fsh bal=%.2f",
                 st.title[:28], qi.side, got, qi.size, st.inv.balance)

    want = {qi.side: round(qi.price, 4) for qi in intents}
    keep = set()
    for o in st.engine.open_orders():
        if want.get(o.side) == o.price:
            keep.add(o.side)      # leave it alone: requoting loses queue position
        else:
            o.cancelled = True
    for qi in intents:
        if qi.side in keep:
            continue
        book = up if qi.side == "UP" else dn
        o = st.engine.post(qi.token_id, qi.side, qi.price, qi.size, book["bids"], now)
        store.log_quote(
            market_slug=m.market_slug, condition_id=m.condition_id,
            token_id=qi.token_id, side=qi.side, price=qi.price, size=qi.size,
            queue_ahead=o.queue_ahead, mid=qi.mid, edge_vs_mid=qi.edge_vs_mid,
            t_remaining=None,
        )

    bq1, bq2 = rewards.book_scores(up, dn, cfg.max_spread_from_mid,
                                   cfg.min_quote_shares)
    oq1, oq2 = rewards.our_scores(st.engine.open_orders(), up, dn,
                                  cfg.max_spread_from_mid, cfg.min_quote_shares)
    ours, theirs, share = rewards.share_of_pool(oq1, oq2, bq1, bq2)
    store.log_reward_sample(
        ts=now, market_slug=m.market_slug, condition_id=m.condition_id,
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
        "share": share, "ours": ours, "theirs": theirs,
        "income": share * st.daily,
        "capital": sum(o.price * (o.size - o.filled) for o in orders),
        "quotes": [{"side": o.side, "price": round(o.price, 4), "size": o.size}
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
        "gate": st.gate,
        # Surfaced because it silently changes the close threshold: a close
        # booked at -0.3c/sh is correct under scarcity and a bug without it,
        # and the dashboard cannot tell the two apart from the P&L alone.
        "capital_scarce": cfg.capital_scarce,
        "markout": st.markout.get("mean_per_share"),
        "markout_n": st.markout.get("n", 0),
        "close_why": pt.get("why", ""),
        "fills": st.inv.fills, "err": st.err, "ts": now,
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
    }


def main() -> None:
    RUN.mkdir(exist_ok=True)
    base = load_cfg()
    bot_cfg = load_bot_cfg()
    specs = load_specs()
    states = [MarketState(s, base) for s in specs]
    log.info("fleet starting | %d markets | $%.0f/day funded | offset %.1fc",
             len(states), sum(s["daily"] for s in specs), 100 * base.reward_offset)

    gap = 2.0 / REQ_PER_SEC
    i = 0
    while True:
        st = states[i % len(states)]
        i += 1
        try:
            visit(st, bot_cfg, time.time(), fleet_naked_cost(states))
        except Exception as e:
            log.warning("%s: %s", st.title[:30], e)
            st.err = str(e)

        if i % len(states) == 0:
            live = [s for s in states if s.spec.get("_live", {}).get("ours", 0) > 0]
            inc = sum(s.spec.get("_live", {}).get("income", 0) for s in states)
            cap = sum(s.spec.get("_live", {}).get("capital", 0) for s in states)
            # Resize once per full sweep, when every market has a fresh share
            # reading. Doing it mid-sweep would size half the fleet off this
            # cycle's data and half off the last one's.
            # Wrapped because a sizing bug must never stop data collection.
            # Unwrapped, a ZeroDivisionError in the allocator killed the whole
            # fleet mid-sweep on 2026-07-29 and nothing was collected for 3.5
            # hours. Quoting at the previous size is an acceptable degraded
            # mode; being dead is not.
            try:
                sizes = reallocate(states, base)
            except Exception as e:
                log.warning("reallocate failed, keeping previous sizes: %s: %s",
                            type(e).__name__, e)
                sizes = {}
            funded = sum(1 for n in sizes.values() if n > 0)
            log.info("sweep | %d/%d scoring | est $%.2f/day | capital $%.0f "
                     "| naked $%.0f | funded %d/%d",
                     len(live), len(states), inc, cap,
                     fleet_naked_cost(states), funded, len(states))
            (RUN / "fleet_state.json").write_text(
                json.dumps(specs, default=str), encoding="utf-8")
        time.sleep(gap)


if __name__ == "__main__":
    main()
