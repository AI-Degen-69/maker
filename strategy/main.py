"""Maker simulation loop. Posts nothing real -- models resting bids only.

Cycle, once per second:
  1. find the live 5-min market
  2. pull FULL book depth for both outcomes
  3. feed the books to the queue-aware fill engine (which decides what filled)
  4. re-quote: cancel stale bids, post fresh ones
  5. when a window ends, resolve it and bank the outcome

Never touches the taker bot's files, DB, or process.
"""
from __future__ import annotations

import argparse
import logging
import time
from typing import Optional

import requests

from strategy.net_config import load_net as load_bot_cfg
from strategy.markets import fetch_live_market, fetch_pinned_market
from strategy import store
from strategy.config import load as load_cfg
from strategy.fills import QueueFillEngine
from strategy.quotes import Inventory, decide_quotes, reward_score

log = logging.getLogger("maker")

# pid file for the single-instance guard. Per-bot when MAKER_PID is set: the
# fleet runs four bots at once, and a shared pidfile would make each new bot
# look like a duplicate of the last and refuse to start.
ROOT_PID = __import__("pathlib").Path(
    __import__("os").environ.get("MAKER_PID")
    or (__import__("pathlib").Path(__file__).resolve().parent.parent / "maker.pid"))


def full_book(clob_host: str, token_id: str) -> dict:
    """Full depth, not just top-of-book -- queue position needs the level sizes."""
    r = requests.get(f"{clob_host}/book", params={"token_id": token_id}, timeout=10)
    r.raise_for_status()
    b = r.json()
    bids = {round(float(x["price"]), 4): float(x["size"]) for x in (b.get("bids") or [])}
    asks = {round(float(x["price"]), 4): float(x["size"]) for x in (b.get("asks") or [])}
    return {
        "token_id": token_id,
        "bids": bids,
        "asks": asks,
        "best_bid": max(bids) if bids else None,
        "best_ask": min(asks) if asks else None,
    }


TRADES_API = "https://data-api.polymarket.com/trades"


def recent_trades(condition_id: str, seen: set, limit: int = 500) -> dict:
    """Volume by (token_id, price) that has actually TRADED since we last looked.

    The fill model needs this to tell a level that was TRADED from one that was
    CANCELLED -- from the book they are identical, and guessing costs an order
    of magnitude: on recorded books the book-only model reported a 50% fill
    rate where the tape-confirmed rate was 3%, because every fill it produced
    came from the "level emptied, credit the whole remainder" branch.

    De-duplicated by trade identity rather than by timestamp window: the API
    stamps trades to the second while we poll faster than that, so a time-based
    cursor would double-count or skip. `seen` is per-market and is dropped when
    the window rolls.
    """
    out: dict[str, dict[float, float]] = {}
    try:
        r = requests.get(TRADES_API, params={"market": condition_id, "limit": limit},
                         timeout=8)
        r.raise_for_status()
        rows = r.json() or []
    except Exception as e:
        log.debug("tape fetch failed: %s", e)
        return out                      # no tape -> caller falls back to books
    for t in rows:
        key = (str(t.get("transactionHash") or ""), str(t.get("asset")),
               t.get("timestamp"), t.get("price"), t.get("size"))
        if key in seen:
            continue
        seen.add(key)
        tok = str(t.get("asset"))
        p = round(float(t.get("price") or 0), 4)
        out.setdefault(tok, {})[p] = out.setdefault(tok, {}).get(p, 0.0) + \
            float(t.get("size") or 0)
    return out


def resolve_finished(bot_cfg) -> int:
    n = 0
    for cond, slug in store.unresolved():
        try:
            r = requests.get(f"{bot_cfg.gamma_host}/events", params={"slug": slug},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            d = r.json()
            if not d:
                continue
            mk = (d[0].get("markets") or [{}])[0]
            if not mk.get("closed"):
                continue
            import json as _j
            pr = mk.get("outcomePrices"); toks = mk.get("clobTokenIds")
            if isinstance(pr, str): pr = _j.loads(pr)
            if isinstance(toks, str): toks = _j.loads(toks)
            if not pr or not toks or len(pr) != 2:
                continue
            store.record_resolution(cond, str(toks[0 if float(pr[0]) > float(pr[1]) else 1]))
            log.info("resolved %s", slug)
            n += 1
        except Exception as e:
            log.debug("resolve failed %s: %s", slug, e)
    return n


def _single_instance_guard() -> None:
    """Refuse to start if another maker.main is already running.

    Four copies once ran concurrently against the same maker.db. Each keeps its
    OWN in-memory inventory and fill engine, so the DB ends up holding the SUM
    of several independent strategies -- silently invalid data that still looks
    plausible. Cheap guard, expensive bug.
    """
    import os
    import sys
    pid_file = ROOT_PID
    if pid_file.exists():
        try:
            old = int(pid_file.read_text().strip())
        except Exception:
            old = None
        if old and old != os.getpid() and _pid_alive(old):
            sys.exit(f"maker.main already running (pid {old}). Stop it first.")
    pid_file.write_text(str(os.getpid()))


def _pid_alive(pid: int) -> bool:
    import sys
    if sys.platform == "win32":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259
    import os
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def loop() -> None:
    cfg = load_cfg()
    bot_cfg = load_bot_cfg()
    _single_instance_guard()
    if cfg.objective == "rewards":
        log.info("maker sim starting | bankroll $%.0f | objective=rewards | "
                 "quote %dsh %.1fc under mid (reward window %.1fc)",
                 cfg.bankroll_usd, cfg.quote_shares, 100 * cfg.reward_offset,
                 100 * cfg.max_spread_from_mid)
    else:
        log.info("maker sim starting | bankroll $%.0f | objective=pair | "
                 "quote %dsh %d tick under ask",
                 cfg.bankroll_usd, cfg.quote_shares, cfg.ticks_below_ask)

    pinned_cache = None
    pinned_ts = 0.0
    engine = QueueFillEngine()
    inv_by_market: dict[str, Inventory] = {}
    quote_ids: dict[int, dict] = {}       # id(RestingOrder) -> meta for logging
    # Markets we have already crossed to balance. The hedge is a one-shot: it
    # takes real liquidity, so re-firing it every tick inside the final 20s
    # would walk the book repeatedly and buy the same leg many times over.
    hedged_conds: set[str] = set()
    seen_trades: set = set()      # tape rows already accounted for, per market
    tape_primed = False           # first tape pull of a window is backlog
    current_cond: Optional[str] = None
    last_quote = 0.0
    last_resolve = 0.0
    last_dec_flush = 0.0

    while True:
        now = time.time()
        try:
            if cfg.pinned_condition_id:
                # Long-dated funded market: same book all day, so re-fetch is
                # only to notice it closing. Cached between polls.
                if pinned_cache is None or now - pinned_ts > 300:
                    pinned_cache = fetch_pinned_market(cfg.pinned_condition_id)
                    pinned_ts = now
                    if pinned_cache is None:
                        log.error("market %s is NOT funded (rewards.rates null) "
                                  "or not accepting orders -- refusing to quote",
                                  cfg.pinned_condition_id[:12])
                        time.sleep(30.0)
                        continue
                m = pinned_cache
            else:
                m = fetch_live_market(bot_cfg.gamma_host, cfg.series_slug)
        except Exception as e:
            log.warning("market fetch failed: %s", e)
            time.sleep(2.0)
            continue
        if not m:
            time.sleep(1.0)
            continue

        # New window -> drop stale quotes, start a fresh inventory.
        if m.condition_id != current_cond:
            engine.cancel()
            current_cond = m.condition_id
            seen_trades = set()          # tape identity set is per-market
            tape_primed = False
            inv_by_market.setdefault(m.condition_id, Inventory())
            log.info("new market %s", m.market_slug)

        inv = inv_by_market.setdefault(m.condition_id, Inventory())
        t_rem = m.end_ts - now

        try:
            up = full_book(bot_cfg.clob_host, m.up_token)
            dn = full_book(bot_cfg.clob_host, m.down_token)
        except Exception as e:
            log.warning("book fetch failed: %s", e)
            time.sleep(cfg.poll_interval_sec)
            continue

        # --- decisive census: once per distinct market, record whether a
        # fillable sub-$1.00 hedged pair exists at ask-1tick. This is the
        # number that ends the experiment; observed every cycle so we never
        # miss a market, but the store is keyed by condition_id so only one
        # row per market is kept.
        try:
            up_ask = up.get("best_ask")
            dn_ask = dn.get("best_ask")
            if up_ask is not None and dn_ask is not None:
                pair_at_touch = (up_ask + dn_ask) - 2 * cfg.ticks_below_ask * cfg.tick_size
                fillable = pair_at_touch < cfg.max_pair_cost
                store.record_hedge_census(
                    m.condition_id, m.market_slug, up_ask, dn_ask,
                    pair_at_touch, fillable, now,
                )
        except Exception as e:
            log.debug("census failed %s: %s", m.condition_id, e)

        # 1. Apply book movement -> fills, with the tape deciding what was a
        # trade and what was only a cancellation.
        tape = recent_trades(m.condition_id, seen_trades)
        first_pass = not tape_primed
        tape_primed = True
        for bk in (up, dn):
            # On the very first cycle of a window the tape query returns the
            # whole backlog of that window's trades, none of which happened
            # while our (not yet posted) orders were resting. Priming pass:
            # record them as seen, credit none of them.
            tp = {} if first_pass else tape.get(bk["token_id"], {})
            for f in engine.on_book(bk["token_id"], bk["bids"], now, tp):
                meta = quote_ids.get(f.token_id + f"{f.price:.4f}", {})
                if f.side == "UP":
                    inv.up_shares += f.size; inv.up_cost += f.size * f.price
                else:
                    inv.down_shares += f.size; inv.down_cost += f.size * f.price
                inv.fills += 1
                # A fill at/above the best ask means we CROSSED the spread
                # (the balance hedge). Tag it so kpi/settlement can tell a
                # maker fill from a settlement crossing.
                crossed = (f.price >= (up["best_ask"] if f.side == "UP"
                                            else dn["best_ask"]))
                store.log_fill(
                    quote_id=meta.get("quote_id"), market_slug=m.market_slug,
                    condition_id=m.condition_id, token_id=f.token_id, side=f.side,
                    price=f.price, size=f.size, mid_at_post=meta.get("mid"),
                    edge_vs_mid=meta.get("edge_vs_mid"), queue_waited=f.queue_waited,
                    seconds_to_fill=now - meta.get("posted_ts", now),
                    crossed=crossed, reason=f.reason,
                )
                log.info("FILL %s %.0fsh @ %.3f (queue waited %.0f) pair=%.4f bal=%.2f",
                         f.side, f.size, f.price, f.queue_waited,
                         inv.pair_cost(), inv.balance)

        # 1b. BALANCE HEDGE (settlement crossing). On 5-min BTC the
        # proven loss driver is a PARTIAL fill: one side rests and fills,
        # the other never does before close, so the market settles
        # one-sided and eats the full resolution loss. A passive maker
        # can't un-fill a resting side, so the only enforceable balance
        # rule is to CROSS the spread for the missing leg near close.
        # We do it once, only if still imbalanced, so every settled
        # market is balanced -> Phase B's verdict is on the instrument.
        # Under the rewards objective this hedge is DISABLED, and removing it
        # from that path matters more than it looks. Firing at 20s left, it can
        # only buy the missing leg AFTER the outcome is effectively decided --
        # measured fill prices 0.01 and 0.02. That is not insurance. It turns
        # an already-won directional bet into a "cheap pair" and books the luck
        # as strategy profit, while the losing version of the same coin flip
        # (our leg loses, missing side now costs 0.98, pair costs ~1.48) is
        # exactly what it cannot protect against. It made $101 of two-flip luck
        # read as edge. Inventory is now managed continuously by quote skew
        # instead -- early, while both sides still cost real money.
        if cfg.objective != "rewards" \
                and t_rem <= cfg.balance_hedge_sec and inv.fills >= 1:
            hi = max(inv.up_shares, inv.down_shares)
            if hi > 0 and inv.balance < cfg.target_balance:
                need_side = "UP" if inv.up_shares < inv.down_shares else "DOWN"
                have = inv.up_shares if need_side == "UP" else inv.down_shares
                need_sh = max(0, hi - have)
                if need_sh >= cfg.min_quote_shares and m.condition_id not in hedged_conds:
                    bk = up if need_side == "UP" else dn
                    ba = bk.get("best_ask")
                    if ba is not None and ba < 1.0:
                        # cancel resting quotes first so we don't double up
                        engine.cancel()
                        store.flush_decision()
                        hedged_conds.add(m.condition_id)

                        # How far up the book we will walk. The pair pays
                        # exactly $1.00, so paying more than (cap - what the
                        # other leg cost) makes the hedge a bigger guaranteed
                        # loss than the imbalance it was fixing.
                        other_avg = inv.avg("DOWN" if need_side == "UP" else "UP")
                        cap = (min(cfg.max_pair_cost - other_avg, 1.0)
                               if other_avg > 0 else 1.0)

                        crossed_fills = engine.cross(
                            bk["token_id"], need_side, int(need_sh),
                            bk["asks"], now, max_price=cap,
                        )
                        got = sum(f.size for f in crossed_fills)
                        qid = store.log_quote(
                            market_slug=m.market_slug, condition_id=m.condition_id,
                            token_id=bk["token_id"], side=need_side,
                            price=round(ba, 4), size=int(need_sh),
                            queue_ahead=0.0, mid=None,
                            edge_vs_mid=None, t_remaining=t_rem,
                        )
                        for f in crossed_fills:
                            if f.side == "UP":
                                inv.up_shares += f.size; inv.up_cost += f.size * f.price
                            else:
                                inv.down_shares += f.size; inv.down_cost += f.size * f.price
                            inv.fills += 1
                            store.log_fill(
                                quote_id=qid, market_slug=m.market_slug,
                                condition_id=m.condition_id, token_id=f.token_id,
                                side=f.side, price=f.price, size=f.size,
                                mid_at_post=None, edge_vs_mid=None,
                                queue_waited=0.0, seconds_to_fill=0.0,
                                crossed=True, reason=f.reason,
                            )
                        store.log_decision(
                            market_slug=m.market_slug, condition_id=m.condition_id,
                            action="CROSS_HEDGE", side=need_side,
                            price=round(ba, 4), mid=None, edge_vs_mid=None,
                            t_remaining=t_rem, balance=inv.balance,
                            pair_cost=inv.pair_cost(),
                            reason=f"imbalanced {inv.balance:.2f} < {cfg.target_balance:.2f}; "
                                    f"crossed {got:.0f}/{need_sh:.0f}sh up to {cap:.3f}",
                        )
                        log.info("CROSS_HEDGE %s %.0f/%.0fsh (cap %.3f) bal=%.2f",
                                  need_side, got, need_sh, cap, inv.balance)
                        continue

        # 2. Re-quote.
        if now - last_quote >= cfg.requote_interval_sec:
            last_quote = now

            # How far into the trading window we are, for the timing rule.
            # eventStartTime is the real window open; if it looks wrong we pass
            # None so decide_quotes skips the rule instead of gating on garbage.
            span = m.end_ts - m.start_ts
            w_frac = ((now - m.start_ts) / span) if span > 0 else None
            intents, why = decide_quotes(cfg, up, dn, inv, t_rem, w_frac)
            # ONE decision row per cycle, not one per side. Logging each side
            # separately made the run key alternate UP/DOWN every cycle, so
            # runs never built and compression stalled at ~2x. The exact
            # per-quote record still lives in the `quotes` table.
            if not intents:
                store.log_decision(
                    market_slug=m.market_slug, condition_id=m.condition_id,
                    action="SKIP_QUOTE", side=None, price=None,
                    mid=None, edge_vs_mid=None, t_remaining=t_rem,
                    balance=inv.balance, pair_cost=inv.pair_cost(), reason=why,
                )
            else:
                sides = "+".join(sorted(q.side for q in intents))
                store.log_decision(
                    market_slug=m.market_slug, condition_id=m.condition_id,
                    action="QUOTE", side=sides,
                    price=intents[0].price, mid=intents[0].mid,
                    edge_vs_mid=intents[0].edge_vs_mid, t_remaining=t_rem,
                    balance=inv.balance, pair_cost=inv.pair_cost(),
                    reason="; ".join(f"{q.side}@{q.price:.2f}" for q in intents),
                )
            # Only reprice what actually moved. Cancelling and re-posting sends
            # us to the BACK of the queue, so blanket-cancelling every 2s threw
            # away our queue position roughly as often as we polled -- measured
            # on 7 recorded windows, that churn cut the fill rate from 6.7% to
            # 3.9%. An order at an unchanged price is left exactly where it is.
            want = {qi.side: round(qi.price, 4) for qi in intents}
            keep: set[str] = set()
            drop = []
            for o in engine.open_orders():
                if want.get(o.side) == o.price:
                    keep.add(o.side)
                else:
                    drop.append(o)
            for o in drop:
                o.cancelled = True
                qid = quote_ids.get(o.token_id + f"{o.price:.4f}", {}).get("quote_id")
                if qid:
                    store.mark_cancelled([qid])

            if intents:
                for qi in intents:
                    if qi.side in keep:
                        continue          # already resting at this exact price
                    bk = up if qi.side == "UP" else dn
                    o = engine.post(qi.token_id, qi.side, qi.price, qi.size, bk["bids"], now)
                    qid = store.log_quote(
                        market_slug=m.market_slug, condition_id=m.condition_id,
                        token_id=qi.token_id, side=qi.side, price=qi.price,
                        size=qi.size, queue_ahead=o.queue_ahead, mid=qi.mid,
                        edge_vs_mid=qi.edge_vs_mid, t_remaining=t_rem,
                    )
                    quote_ids[qi.token_id + f"{qi.price:.4f}"] = {
                        "quote_id": qid, "mid": qi.mid,
                        "edge_vs_mid": qi.edge_vs_mid, "posted_ts": now,
                    }
            elif why:
                log.debug("no quotes: %s", why)

        # 2a. Reward accrual. The liquidity reward is paid on RESTING size
        # sampled once a minute, filled or not, so score-share over time IS the
        # P&L on this objective -- and the previous 60-market run recorded only
        # fills, which is why it read as a flat coin-flip while the actual
        # payoff went unmeasured. Sampled every cycle, including cycles where we
        # hold nothing: time out of the book is the cost being measured.
        if cfg.objective == "rewards":
            v = cfg.max_spread_from_mid
            C = 3.0

            def q_min(q_one: float, q_two: float) -> float:
                """The venue's per-sample score. NOT the sum of the two sides.

                    Q_min = max( min(Q_one, Q_two), max(Q_one/c, Q_two/c) )

                An earlier version summed them, which reports a balanced
                two-sided quote as 2X instead of X -- double the true score,
                and therefore double the estimated income. The min is the whole
                point of the rule: it pays for genuine two-sided liquidity and
                refuses to pay twice for one side posted twice.
                """
                return max(min(q_one, q_two), max(q_one / C, q_two / C))

            # Q_one = bids on UP + asks on DOWN;  Q_two = asks on UP + bids on DOWN.
            mkt_q1 = mkt_q2 = 0.0
            for bk, is_up in ((up, True), (dn, False)):
                bb, ba = bk.get("best_bid"), bk.get("best_ask")
                if bb is None or ba is None:
                    continue
                bmid = (bb + ba) / 2.0
                for lvls, sign in ((bk["bids"], 1.0), (bk["asks"], -1.0)):
                    is_bid = sign > 0
                    for lp, lsz in lvls.items():
                        s = (bmid - lp) * sign
                        if 0 <= s <= v and lsz >= cfg.min_quote_shares:
                            sc = ((v - s) / v) ** 2 * lsz
                            if is_bid == is_up:
                                mkt_q1 += sc
                            else:
                                mkt_q2 += sc
            # Approximation, and it cannot be made exact: the true denominator
            # is the SUM of every maker's own Q_min, and a public book does not
            # say which orders belong to the same maker. Treating the rest of
            # the book as one aggregate maker is the closest available proxy.
            mkt_score = q_min(mkt_q1, mkt_q2)

            our_q1 = our_q2 = 0.0
            sides = set()
            for o in engine.open_orders():
                bk = up if o.side == "UP" else dn
                bb, ba = bk.get("best_bid"), bk.get("best_ask")
                if bb is None or ba is None:
                    continue
                omid = (bb + ba) / 2.0
                rem = max(0.0, o.size - o.filled)
                sc = reward_score(cfg, omid - o.price, rem)
                if sc > 0:
                    # We only ever post BIDS. A bid on UP is Q_one; a bid on
                    # DOWN is economically an ask on UP, so it is Q_two.
                    if o.side == "UP":
                        our_q1 += sc
                    else:
                        our_q2 += sc
                    sides.add(o.side)
            our_score = q_min(our_q1, our_q2)
            store.log_reward_sample(
                ts=now, market_slug=m.market_slug, condition_id=m.condition_id,
                our_score=our_score, market_score=mkt_score,
                offset_c=100 * cfg.reward_offset, n_sides=len(sides),
            )

        # 2b. Publish what we're looking at, so the dashboard process can render
        # the live market without duplicating market/book polling.
        store.set_live_state({
            "market_slug": m.market_slug,
            "condition_id": m.condition_id,
            "end_ts": m.end_ts,
            "t_remaining": t_rem,
            "up": {"best_bid": up["best_bid"], "best_ask": up["best_ask"],
                   "bid_depth": sum(up["bids"].values()),
                   "top_bids": sorted(up["bids"].items(), reverse=True)[:5]},
            "down": {"best_bid": dn["best_bid"], "best_ask": dn["best_ask"],
                     "bid_depth": sum(dn["bids"].values()),
                     "top_bids": sorted(dn["bids"].items(), reverse=True)[:5]},
            "inventory": {
                "up_shares": inv.up_shares, "down_shares": inv.down_shares,
                "up_avg": inv.avg("UP"), "down_avg": inv.avg("DOWN"),
                "cost": inv.cost, "balance": inv.balance,
                "pair_cost": inv.pair_cost(), "fills": inv.fills,
            },
            "open_quotes": [
                {"side": o.side, "price": o.price, "size": o.size,
                 "filled": o.filled, "queue_ahead": o.queue_ahead}
                for o in engine.open_orders()
            ],
        })

        # Flush the collapsed decision run so the log reaches the DB even
        # while the decision is unchanged.
        if now - last_dec_flush > 5:
            last_dec_flush = now
            store.flush_decision()

        # 3. Resolutions.
        if now - last_resolve > 30:
            last_resolve = now
            try:
                resolve_finished(bot_cfg)
            except Exception as e:
                log.warning("resolve pass failed: %s", e)

        time.sleep(cfg.poll_interval_sec)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
    )
    try:
        loop()
    except KeyboardInterrupt:
        log.info("shutdown")


if __name__ == "__main__":
    main()
