"""Replay recorded books through the FIXED fill engine and measure fill rate.

WHY THIS EXISTS
---------------
Every maker number recorded before the phantom-fill fix came from an engine that
granted a full fill to any order resting above the best bid. The headline
"37.6% fill rate against real queue depth" was produced by that engine and is
void. The strategy's whole economics -- a hedged pair bought for $0.99 that pays
$1.00 -- only exists if BOTH legs actually fill, so the fill rate is the number
that decides whether anything else here is worth building.

WHAT IT MEASURES
----------------
An EPISODE is one resting order: posted at a price, held until it is repriced,
cancelled, or the window closes. Episodes, not repost events, are the honest
denominator -- an order that sits at 0.52 for two minutes is one attempt to get
filled, however many times the code re-sends it.

That distinction is itself a measurement. Cancelling and re-posting at the same
price puts us at the BACK of the queue again, so `--requote always` (what
strategy/main.py does today, every 2s) and `--requote on-change` (reprice only
when the target price actually moves) can differ enormously. Both are reported.

RULES
-----
  strategy  replay strategy/quotes.py decide_quotes verbatim -- measures the
            actual bot, including its pair-cost and balance gates, and picks up
            any change made to it
  ask1      unconditional rest at best_ask - 1 tick (the raw mechanism)
  bid       unconditional rest at best_bid (join the touch queue)
  bid1      unconditional rest at best_bid - 1 tick (deeper, cheaper, slower)

BIASES (inherited from strategy/fills.py, all OPTIMISTIC -- upper bound)
  * a level shrinking may be cancellations, not trades, and we still credit the
    post-queue remainder to ourselves
  * strict price-time priority is assumed
One PESSIMISTIC bias is ours: resting alone inside the spread is invisible in
bid-side deltas, so those fills are never credited. Where the spread is 1 tick
-- the measured median -- ask-1tick IS the bid, so this rarely binds.
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategy.config import load as load_cfg              # noqa: E402
from strategy.fills import QueueFillEngine                # noqa: E402
from strategy.quotes import Inventory, decide_quotes      # noqa: E402


# --------------------------------------------------------------------------
# data loading
# --------------------------------------------------------------------------

@dataclass
class Poll:
    ts: float
    up: dict          # {'token_id','bids','asks','best_bid','best_ask'}
    dn: dict


@dataclass
class Window:
    condition_id: str
    market_slug: str
    start_ts: float
    end_ts: float
    polls: list[Poll] = field(default_factory=list)
    # token_id -> sorted [(ts, price, size)] from the tape, if backfilled
    tape: dict[str, list[tuple[float, float, float]]] = field(default_factory=dict)

    @property
    def coverage(self) -> float:
        """Fraction of the window we actually recorded, 0..1."""
        if not self.polls or self.end_ts <= self.start_ts:
            return 0.0
        span = self.end_ts - self.start_ts
        return max(0.0, min(1.0, (self.polls[-1].ts - self.polls[0].ts) / span))

    @property
    def first_frac(self) -> float:
        """How far into the window our first poll landed, 0..1."""
        span = self.end_ts - self.start_ts
        return (self.polls[0].ts - self.start_ts) / span if span > 0 and self.polls else 1.0


def _side(bids: dict, asks: dict, token_id: str) -> dict:
    live_b = {float(p): s for p, s in bids.items() if s > 1e-9}
    live_a = {float(p): s for p, s in asks.items() if s > 1e-9}
    return {
        "token_id": token_id,
        "bids": {float(p): s for p, s in bids.items()},
        "asks": {float(p): s for p, s in asks.items()},
        "best_bid": max(live_b) if live_b else None,
        "best_ask": min(live_a) if live_a else None,
    }


def load_windows(path: Path) -> list[Window]:
    c = sqlite3.connect(str(path))
    rows = c.execute(
        "SELECT condition_id, market_slug, start_ts, end_ts, ts, token_id, side, "
        "bids, asks FROM snapshots ORDER BY condition_id, ts"
    ).fetchall()
    by_cond: dict[str, Window] = {}
    staging: dict[tuple[str, float], dict] = {}
    for cond, slug, st, et, ts, tok, side, bids, asks in rows:
        by_cond.setdefault(cond, Window(cond, slug, st, et))
        staging.setdefault((cond, ts), {})[side] = _side(
            json.loads(bids), json.loads(asks), tok)
    for (cond, ts), sides in staging.items():
        if "UP" in sides and "DOWN" in sides:      # need both legs to be usable
            by_cond[cond].polls.append(Poll(ts, sides["UP"], sides["DOWN"]))
    for w in by_cond.values():
        w.polls.sort(key=lambda p: p.ts)

    # Tape, if scripts/fetch_trades.py has been run against this books.db.
    try:
        for cond, tok, ts, price, size in c.execute(
                "SELECT condition_id, token_id, ts, price, size FROM trades "
                "ORDER BY ts"):
            w = by_cond.get(cond)
            if w is not None:
                w.tape.setdefault(str(tok), []).append(
                    (float(ts), round(float(price), 4), float(size)))
    except sqlite3.OperationalError:
        pass          # no trades table yet -- fall back to the book-only model

    return [w for w in by_cond.values() if len(w.polls) >= 3]


def traded_between(w: Window, token_id: str, lo: float, hi: float
                   ) -> Optional[dict[float, float]]:
    """Volume by price that printed in (lo, hi]. None when we have no tape.

    The API timestamps trades to the second while polls land on sub-second
    boundaries, so a trade stamped exactly at a poll boundary is attributed to
    the interval that CLOSES at it -- consistently, so no interval double
    counts and none is skipped.
    """
    rows = w.tape.get(token_id)
    if rows is None:
        return None
    out: dict[float, float] = {}
    i = bisect.bisect_right(rows, (lo, float("inf"), float("inf")))
    while i < len(rows) and rows[i][0] <= hi:
        _, price, size = rows[i]
        out[price] = out.get(price, 0.0) + size
        i += 1
    return out


# --------------------------------------------------------------------------
# episodes
# --------------------------------------------------------------------------

@dataclass
class Episode:
    condition_id: str
    side: str
    price: float
    size: float
    queue_ahead: float
    posted_ts: float
    posted_frac: float          # where in the window we posted, 0..1
    filled: float = 0.0
    # Split by how the engine decided. 'sweep' shares are credited off a level
    # emptying, which a mass cancel produces just as well as a mass trade, so a
    # fill rate has to be readable with them excluded.
    filled_queue: float = 0.0
    filled_sweep: float = 0.0
    first_fill_ts: Optional[float] = None
    closed_ts: Optional[float] = None
    ended: str = "open"         # repriced | window_end | halted

    @property
    def rested(self) -> float:
        return (self.closed_ts or self.posted_ts) - self.posted_ts

    @property
    def time_to_fill(self) -> Optional[float]:
        return None if self.first_fill_ts is None else self.first_fill_ts - self.posted_ts


# --------------------------------------------------------------------------
# quoting rules
# --------------------------------------------------------------------------

def _passive_target(cfg, book: dict, rule: str) -> Optional[float]:
    bb, ba = book.get("best_bid"), book.get("best_ask")
    if rule == "ask1":
        if ba is None:
            return None
        p = round(ba - cfg.ticks_below_ask * cfg.tick_size, 4)
    elif rule == "bid":
        if bb is None:
            return None
        p = round(bb, 4)
    elif rule == "bid1":
        if bb is None:
            return None
        p = round(bb - cfg.tick_size, 4)
    else:
        raise ValueError(rule)
    return p if 0.0 < p < 1.0 else None


def targets_for(cfg, rule: str, up: dict, dn: dict, inv: Inventory,
                t_rem: float, w_frac: Optional[float] = None
                ) -> dict[str, tuple[str, float, int]]:
    """side -> (token_id, price, size). Empty dict = quote nothing."""
    if rule == "strategy":
        intents, _ = decide_quotes(cfg, up, dn, inv, t_rem, w_frac)
        return {q.side: (q.token_id, q.price, q.size) for q in intents}
    out: dict[str, tuple[str, float, int]] = {}
    for side, bk in (("UP", up), ("DOWN", dn)):
        p = _passive_target(cfg, bk, rule)
        if p is not None:
            out[side] = (bk["token_id"], p, cfg.quote_shares)
    return out


# --------------------------------------------------------------------------
# the replay
# --------------------------------------------------------------------------

@dataclass
class WindowResult:
    condition_id: str
    market_slug: str
    coverage: float
    episodes: list[Episode]
    up_shares: float = 0.0
    down_shares: float = 0.0
    up_cost: float = 0.0
    down_cost: float = 0.0
    crossed_shares: float = 0.0      # taken by the balance hedge, not rested
    taker_fees: float = 0.0          # what those crossed shares cost in fees
    markouts: list[float] = field(default_factory=list)   # settle-proxy, USD

    @property
    def hedged(self) -> float:
        return min(self.up_shares, self.down_shares)

    @property
    def pair_cost(self) -> Optional[float]:
        if self.up_shares <= 0 or self.down_shares <= 0:
            return None
        return self.up_cost / self.up_shares + self.down_cost / self.down_shares

    @property
    def unhedged(self) -> float:
        return abs(self.up_shares - self.down_shares)


def replay(w: Window, cfg, rule: str, requote: str,
           price_band: Optional[tuple[float, float]],
           time_frac: Optional[float],
           requote_interval: float,
           hedge: bool = False,
           use_tape: bool = True) -> WindowResult:
    hedged = False
    prev_ts: Optional[float] = None
    engine = QueueFillEngine()
    inv = Inventory()
    res = WindowResult(w.condition_id, w.market_slug, w.coverage, [])
    open_ep: dict[str, Episode] = {}
    open_ord: dict[str, object] = {}
    last_requote = 0.0
    span = max(1e-9, w.end_ts - w.start_ts)
    mid_hist: dict[str, list[tuple[float, float]]] = {"UP": [], "DOWN": []}

    def close(side: str, why: str, ts: float) -> None:
        ep = open_ep.pop(side, None)
        open_ord.pop(side, None)
        if ep is not None:
            ep.closed_ts = ts
            ep.ended = why
            res.episodes.append(ep)

    for poll in w.polls:
        ts = poll.ts
        t_rem = w.end_ts - ts
        frac = (ts - w.start_ts) / span

        for lbl, bk in (("UP", poll.up), ("DOWN", poll.dn)):
            if bk["best_bid"] is not None and bk["best_ask"] is not None:
                mid_hist[lbl].append((ts, (bk["best_bid"] + bk["best_ask"]) / 2.0))

        # 1. book -> fills (same order as strategy/main.py)
        for lbl, bk in (("UP", poll.up), ("DOWN", poll.dn)):
            tp = (traded_between(w, bk["token_id"], prev_ts, ts)
                  if use_tape and prev_ts is not None else None)
            # U1 made the tape the only crediting path, so a no-tape replay --
            # which is this tool's whole book-only mode -- now returns nothing
            # and records shadow candidates instead. Read them back here so the
            # comparison this script exists to make still works. What changed is
            # the label, not the arithmetic: in book-only mode these are the
            # shares the old model CLAIMED, not shares anybody can stand behind.
            shadow_mark = len(engine.unverified)
            observed = engine.on_book(bk["token_id"], bk["bids"], ts, tp)
            if tp is None:
                observed = engine.unverified[shadow_mark:]
            for f in observed:
                if f.side == "UP":
                    inv.up_shares += f.size; inv.up_cost += f.size * f.price
                    res.up_shares += f.size; res.up_cost += f.size * f.price
                else:
                    inv.down_shares += f.size; inv.down_cost += f.size * f.price
                    res.down_shares += f.size; res.down_cost += f.size * f.price
                inv.fills += 1
                ep = open_ep.get(f.side)
                if ep is not None:
                    ep.filled += f.size
                    if f.reason in ("sweep", "unverified_sweep"):
                        ep.filled_sweep += f.size
                    else:
                        # 'queue' (book-only) and 'tape' (tape-confirmed) are
                        # both observed consumption past our queue position.
                        ep.filled_queue += f.size
                    if ep.first_fill_ts is None:
                        ep.first_fill_ts = ts
        # Advance the tape cursor HERE, not at the bottom of the loop: the
        # paths below `continue`, and a stale cursor would re-credit the same
        # prints to the next interval as well.
        prev_ts = ts

        # 1b. balance hedge: cross for the missing leg near the close, once.
        # This is a TAKER order and pays the fee, which is netted out below.
        if (hedge and not hedged and t_rem <= cfg.balance_hedge_sec
                and (res.up_shares > 0 or res.down_shares > 0)):
            hi = max(res.up_shares, res.down_shares)
            bal = (min(res.up_shares, res.down_shares) / hi) if hi > 0 else 1.0
            if bal < cfg.target_balance:
                need_side = "UP" if res.up_shares < res.down_shares else "DOWN"
                have = res.up_shares if need_side == "UP" else res.down_shares
                need_sh = max(0.0, hi - have)
                if need_sh >= cfg.min_quote_shares:
                    hedged = True
                    bk = poll.up if need_side == "UP" else poll.dn
                    other = (res.down_cost / res.down_shares if need_side == "UP"
                             and res.down_shares > 0 else
                             (res.up_cost / res.up_shares if res.up_shares > 0 else 0.0))
                    capp = min(cfg.max_pair_cost - other, 1.0) if other > 0 else 1.0
                    for f in engine.cross(bk["token_id"], need_side, need_sh,
                                          bk["asks"], ts, max_price=capp):
                        if f.side == "UP":
                            res.up_shares += f.size; res.up_cost += f.size * f.price
                        else:
                            res.down_shares += f.size; res.down_cost += f.size * f.price
                        res.crossed_shares += f.size
                        res.taker_fees += (f.size * cfg.fee_rate
                                           * f.price * (1.0 - f.price))

        # 2. stop quoting near the close, exactly as the bot does
        if t_rem < cfg.min_t_remaining_sec:
            for s in list(open_ep):
                close(s, "halted", ts)
            engine.cancel()
            continue

        if ts - last_requote < requote_interval:
            continue
        last_requote = ts

        want = targets_for(cfg, rule, poll.up, poll.dn, inv, t_rem, frac)

        # optional powerwinner filters, applied on top of whatever the rule said
        if time_frac is not None and frac > time_frac:
            want = {}
        if price_band is not None:
            lo, hi = price_band
            want = {s: v for s, v in want.items() if lo <= v[1] <= hi}

        for side in ("UP", "DOWN"):
            tgt = want.get(side)
            ep = open_ep.get(side)
            if ep is None:
                if tgt is None:
                    continue
            else:
                # A fully-filled order is DONE, not resting. Without this the
                # side went quiet after every complete fill until the price
                # happened to move, silently cutting how much we ever post.
                stale = (tgt is None
                         or requote == "always"
                         or abs(ep.price - tgt[1]) > 1e-9
                         or not open_ord[side].is_open)
                if not stale:
                    continue
                done = not open_ord[side].is_open
                engine.cancel(open_ord[side].token_id)
                close(side, "filled" if done else "repriced", ts)
                if tgt is None:
                    continue
            token, price, size = tgt
            bk = poll.up if side == "UP" else poll.dn
            o = engine.post(token, side, price, size, bk["bids"], ts)
            open_ord[side] = o
            open_ep[side] = Episode(
                condition_id=w.condition_id, side=side, price=price, size=size,
                queue_ahead=o.queue_ahead, posted_ts=ts, posted_frac=frac,
            )

    end_ts = w.polls[-1].ts
    for s in list(open_ep):
        close(s, "window_end", end_ts)

    # adverse selection proxy: last observed mid vs our average fill price
    for lbl, sh, cost in (("UP", res.up_shares, res.up_cost),
                          ("DOWN", res.down_shares, res.down_cost)):
        if sh > 0 and mid_hist[lbl]:
            res.markouts.append((mid_hist[lbl][-1][1] - cost / sh) * sh)
    return res


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{100*x:5.1f}%"


def report(results: list[WindowResult], label: str) -> dict:
    eps = [e for r in results for e in r.episodes]
    posted = sum(e.size for e in eps)
    filled = sum(e.filled for e in eps)
    filled_q = sum(e.filled_queue for e in eps)
    filled_s = sum(e.filled_sweep for e in eps)
    any_fill = [e for e in eps if e.filled > 1e-9]
    full_fill = [e for e in eps if e.filled >= e.size - 1e-9]
    ttf = [e.time_to_fill for e in any_fill if e.time_to_fill is not None]

    both = [r for r in results if r.up_shares > 0 and r.down_shares > 0]
    one = [r for r in results if (r.up_shares > 0) != (r.down_shares > 0)]
    none = [r for r in results if r.up_shares <= 0 and r.down_shares <= 0]
    pcs = [r.pair_cost for r in both if r.pair_cost is not None]

    hedged_edge = sum(r.hedged * (1.0 - (r.pair_cost or 1.0)) for r in both)
    unhedged_sh = sum(r.unhedged for r in results)
    fees = sum(r.taker_fees for r in results)
    crossed = sum(r.crossed_shares for r in results)

    out = {
        "label": label,
        "windows": len(results),
        "episodes": len(eps),
        "posted_shares": posted,
        "filled_shares": filled,
        "share_fill_rate": (filled / posted) if posted else None,
        "share_fill_rate_queue_only": (filled_q / posted) if posted else None,
        "filled_shares_queue": filled_q,
        "filled_shares_sweep": filled_s,
        "sweep_share_of_fills": (filled_s / filled) if filled else None,
        "episode_any_fill_rate": (len(any_fill) / len(eps)) if eps else None,
        "episode_full_fill_rate": (len(full_fill) / len(eps)) if eps else None,
        "median_time_to_fill_s": statistics.median(ttf) if ttf else None,
        "windows_both_legs": len(both),
        "windows_one_leg": len(one),
        "windows_no_fill": len(none),
        "pair_completion_rate": (len(both) / len(results)) if results else None,
        "median_pair_cost": statistics.median(pcs) if pcs else None,
        "hedged_shares_total": sum(r.hedged for r in both),
        "hedged_edge_usd": hedged_edge,
        "unhedged_shares_total": unhedged_sh,
        "crossed_shares": crossed,
        "taker_fees_usd": fees,
        "hedged_edge_net_of_fees_usd": hedged_edge - fees,
    }

    print(f"\n=== {label} ===")
    print(f"windows {out['windows']}   episodes {out['episodes']}   "
          f"posted {posted:,.0f}sh   filled {filled:,.0f}sh")
    print(f"  share fill rate          {pct(out['share_fill_rate'])}   <- filled / posted")
    print(f"    of which observed queue{pct(out['share_fill_rate_queue_only'])}   "
          f"<- the defensible number")
    print(f"    credited from sweeps   {pct(out['sweep_share_of_fills'])} of fills "
          f"({filled_s:,.0f}sh) -- a mass cancel is indistinguishable from a trade")
    print(f"  episodes with any fill   {pct(out['episode_any_fill_rate'])}")
    print(f"  episodes fully filled    {pct(out['episode_full_fill_rate'])}")
    if out["median_time_to_fill_s"] is not None:
        print(f"  median time to fill      {out['median_time_to_fill_s']:.1f}s")
    print(f"  windows both legs filled {len(both)}/{len(results)} "
          f"({pct(out['pair_completion_rate'])})   one leg only {len(one)}   "
          f"no fill {len(none)}")
    if pcs:
        under = sum(1 for p in pcs if p < 1.0) / len(pcs)
        print(f"  median pair cost         ${out['median_pair_cost']:.4f}   "
              f"under $1.00 {pct(under)}")
    print(f"  hedged shares            {out['hedged_shares_total']:,.0f}"
          f"   locked edge ${hedged_edge:,.2f}")
    print(f"  UNHEDGED shares          {unhedged_sh:,.0f}   "
          f"(exposed to the full resolution outcome)")
    if crossed > 0:
        print(f"  balance hedge            crossed {crossed:,.0f}sh, "
              f"taker fees ${fees:,.2f}  -> edge net of fees "
              f"${hedged_edge - fees:,.2f}")

    # fill rate vs queue depth at post -- the maker metric the dashboard needs
    buckets = [(0, 1), (1, 50), (50, 150), (150, 400), (400, 1e12)]
    print("  fill rate by queue ahead at post:")
    for lo, hi in buckets:
        b = [e for e in eps if lo <= e.queue_ahead < hi]
        if not b:
            continue
        bp = sum(e.size for e in b); bf = sum(e.filled for e in b)
        nm = f"{lo:.0f}-{hi:.0f}" if hi < 1e11 else f"{lo:.0f}+"
        print(f"    queue {nm:>10}sh  n={len(b):4d}  fill {pct(bf/bp if bp else None)}")

    mk = [m for r in results for m in r.markouts]
    if mk:
        print(f"  adverse selection: markout to last mid  "
              f"${sum(mk):,.2f} over {len(mk)} filled legs")
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--books", default=str(ROOT / "books.db"))
    p.add_argument("--rule", default="strategy",
                   choices=["strategy", "ask1", "bid", "bid1"])
    p.add_argument("--requote", default="on-change", choices=["always", "on-change"])
    p.add_argument("--requote-interval", type=float, default=None,
                   help="seconds between requote decisions (default: config)")
    p.add_argument("--price-band", default=None, help="e.g. 0.30,0.70")
    p.add_argument("--time-frac", type=float, default=None,
                   help="only quote in the first FRAC of the window, e.g. 0.40")
    p.add_argument("--min-coverage", type=float, default=0.5,
                   help="skip windows we recorded less of than this")
    p.add_argument("--json", default=None, help="write the summary list here")
    p.add_argument("--all-rules", action="store_true",
                   help="run strategy/ask1/bid/bid1 under both requote modes")
    p.add_argument("--hedge", action="store_true",
                   help="also cross for the missing leg near the close")
    p.add_argument("--no-tape", action="store_true",
                   help="ignore the trade tape and use the book-only fill "
                        "model (the optimistic upper bound), for comparison")
    # One variable at a time: these switch the two powerwinner rules off
    # INDIVIDUALLY so their effects can be read apart instead of bundled.
    p.add_argument("--no-band", action="store_true",
                   help="disable the 0.30-0.70 price band (rule=strategy)")
    p.add_argument("--no-window", action="store_true",
                   help="disable the first-40%% quoting window (rule=strategy)")
    a = p.parse_args()

    cfg = load_cfg()
    if a.no_band or a.no_window:
        import dataclasses
        cfg = dataclasses.replace(
            cfg,
            enforce_price_band=not a.no_band,
            enforce_quote_window=not a.no_window,
        )
    ri = a.requote_interval if a.requote_interval is not None else cfg.requote_interval_sec
    band = None
    if a.price_band:
        lo, hi = (float(x) for x in a.price_band.split(","))
        band = (lo, hi)

    wins = load_windows(Path(a.books))
    usable = [w for w in wins if w.coverage >= a.min_coverage]
    print(f"loaded {len(wins)} windows, {len(usable)} with >= "
          f"{100*a.min_coverage:.0f}% coverage "
          f"({sum(len(w.polls) for w in usable):,} polls)")
    if not usable:
        print("not enough data yet")
        return

    combos = ([(r, q) for r in ("strategy", "ask1", "bid", "bid1")
               for q in ("on-change", "always")]
              if a.all_rules else [(a.rule, a.requote)])

    summaries = []
    for rule, requote in combos:
        results = [replay(w, cfg, rule, requote, band, a.time_frac, ri,
                          a.hedge, not a.no_tape)
                   for w in usable]
        lbl = f"rule={rule} requote={requote}"
        lbl += " tape=OFF(book-only)" if a.no_tape else " tape=on"
        if band:
            lbl += f" band={band[0]}-{band[1]}"
        if a.time_frac:
            lbl += f" first{100*a.time_frac:.0f}%"
        if rule == "strategy":
            lbl += (f" price_band={'on' if cfg.enforce_price_band else 'OFF'}"
                    f" window={'on' if cfg.enforce_quote_window else 'OFF'}")
        if a.hedge:
            lbl += " +hedge"
        summaries.append(report(results, lbl))

    if a.json:
        Path(a.json).write_text(json.dumps(summaries, indent=2))
        print(f"\nwrote {a.json}")


if __name__ == "__main__":
    main()
