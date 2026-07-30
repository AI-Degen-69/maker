"""Maker KPIs.

A taker asks "was I right?". A maker asks "did I get filled, at what price
relative to fair, and did I get picked off?". These are the metrics that tell
you whether to keep going, and they have no equivalent in the taker dashboard.

The headline decomposition splits P&L into the two things that actually pay:
  SPREAD CAPTURE   shares x (mid_at_post - fill_price)  -- the maker's edge
  DIRECTIONAL      what the resolution did to us        -- the maker's cost
  REBATE (est.)    volume-based estimate, never blended into realized PnL
"""
from __future__ import annotations

import statistics
import time
from typing import Optional

from strategy import store
from strategy.config import load as load_cfg

cfg = load_cfg()


def taker_fee(price: float, shares: float) -> float:
    """crypto_fees_v2, from research/market_spec.md:

        taker_fee = shares * 0.07 * p * (1 - p)     # USDC
        maker_fee = 0                               # takerOnly: true

    Only crossed (balance-hedge) fills pay this. Maker fills pay nothing, which
    is the entire reason this repo exists.
    """
    p = max(0.0, min(1.0, price))
    return shares * cfg.fee_rate * p * (1.0 - p)


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with store.db() as c:
        cur = c.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _reward_report() -> dict:
    """What the liquidity-reward objective is actually earning.

    This is the payoff line for the current strategy and it has nothing to do
    with fills. Polymarket pays makers for RESTING size, sampled once a minute,
    filled or not, so the product is share-of-score over time. The previous
    objective measured fills only, which is why 60 markets of running produced
    no reading on the thing that pays.

    `est_usd_per_window` multiplies our share by the observed maker pool. The
    pool figure is measured, not guaranteed -- see cfg.est_reward_pool_usd.
    """
    rows = _rows("SELECT ts, our_score, market_score, our_share, n_sides "
                 "FROM reward_samples ORDER BY ts")
    if not rows:
        return {"samples": 0}

    shares = [r["our_share"] for r in rows if r["our_share"] is not None]
    in_book = [r for r in rows if (r["our_score"] or 0) > 0]
    two_sided = [r for r in in_book if r["n_sides"] == 2]
    avg_share = statistics.mean(shares) if shares else 0.0
    # Uptime is the headline: a cycle spent out of the book earns exactly zero,
    # and the old gates were out of the book 69% of the time.
    uptime = len(in_book) / len(rows)
    span_hours = (rows[-1]["ts"] - rows[0]["ts"]) / 3600.0

    # THE CORRECTION. An earlier version multiplied our resting-score share by
    # a pool and reported ~$46/hr. That number was fiction, for two reasons
    # confirmed against the venue:
    #
    #   1. btc-updown-5m has rewards.rates = null on /clob/markets. The
    #      liquidity-rewards program -- the one that pays for RESTING size --
    #      is not funded on this market. min_size/max_spread are set, but with
    #      no rate attached they fund nothing.
    #   2. What this market does have is the Maker Rebates Program, and that
    #      pays on MATCHED volume only: "you earn based on the share of
    #      liquidity you provided that actually got taken",
    #          rebate = 0.20 * (shares * 0.07 * p * (1-p))
    #      An unfilled resting order earns exactly zero.
    #
    # So the honest income here is per FILLED maker share, and it is tiny:
    # 0.35c/share at p=0.50, against 24.5c/share of measured adverse
    # selection -- the cost is 70x the income. Reported as a real, earned
    # number instead of an estimate, so the dashboard cannot flatter itself.
    fills = _rows("SELECT price, size, crossed FROM fills")
    rebate_earned = sum(
        cfg.rebate_rate * taker_fee(f["price"] or 0.0, f["size"] or 0.0)
        for f in fills if not f.get("crossed"))
    filled_sh = sum(f["size"] or 0 for f in fills if not f.get("crossed"))

    # On a PINNED market we know the funded daily rate exactly, from the venue
    # (rewards.rates[].rewards_daily_rate). No pool guessing: income is our
    # score share of a published number. `pays_for_resting` is True only when
    # that rate is actually positive.
    pays_resting = cfg.market_daily_rate > 0
    est_daily = avg_share * cfg.market_daily_rate if pays_resting else 0.0

    est_window = avg_share * cfg.est_reward_pool_usd
    return {
        # Paid on fills, not on waiting. This is the real one.
        "rebate_earned": rebate_earned,
        "rebate_per_share_cents": (100 * rebate_earned / filled_sh) if filled_sh else None,
        "pays_for_resting": pays_resting,
        "market_daily_rate": cfg.market_daily_rate,
        "est_resting_usd_per_day": est_daily,
        "resting_pay_note": ("funded: $%.0f/day split by score share" % cfg.market_daily_rate)
                            if pays_resting else
                            "rewards.rates=null on this market: resting earns $0",
        "samples": len(rows),
        "uptime": uptime,
        "two_sided_rate": (len(two_sided) / len(in_book)) if in_book else 0.0,
        "avg_share": avg_share,
        "median_share": statistics.median(shares) if shares else None,
        "avg_our_score": statistics.mean([r["our_score"] for r in rows]),
        "avg_market_score": statistics.mean(
            [r["market_score"] for r in rows if r["market_score"]] or [0]),
        "offset_cents": 100 * cfg.reward_offset,
        "est_usd_per_window": est_window,
        "est_usd_per_hour": est_window * 12,       # 5-minute windows
        "hours_running": span_hours,
        "pool_assumption_usd": cfg.est_reward_pool_usd,
    }


def report() -> dict:
    quotes = _rows("SELECT * FROM quotes")
    fills = _rows("SELECT * FROM fills")
    _res_rows = _rows("SELECT * FROM resolutions")
    res = {r["condition_id"]: r["winning_token"] for r in _res_rows}
    res_ts = {r["condition_id"]: r["resolved_ts"] for r in _res_rows}

    posted_sh = sum(q["size"] or 0 for q in quotes)
    filled_sh = sum(f["size"] or 0 for f in fills)
    crossed_sh = sum(f["size"] or 0 for f in fills if f.get("crossed"))
    cost = sum((f["size"] or 0) * (f["price"] or 0) for f in fills)

    # --- the maker's edge: how far below mid did we buy? ------------------
    cap = [(f["size"] or 0) * (f["edge_vs_mid"] or 0) for f in fills if f.get("edge_vs_mid") is not None]
    spread_capture = sum(cap)
    edges = [f["edge_vs_mid"] for f in fills if f.get("edge_vs_mid") is not None]

    # --- fill quality ------------------------------------------------------
    waits = [f["seconds_to_fill"] for f in fills if f.get("seconds_to_fill") is not None]
    queues = [f["queue_waited"] for f in fills if f.get("queue_waited") is not None]

    # --- per-market: inventory balance, pair cost, realized outcome --------
    by_mkt: dict[str, dict] = {}
    for f in fills:
        m = by_mkt.setdefault(f["condition_id"], {
            "slug": f["market_slug"], "up_sh": 0.0, "dn_sh": 0.0,
            "up_cost": 0.0, "dn_cost": 0.0, "fills": 0, "tokens": {},
            "fee": 0.0, "crossed_sh": 0.0,
        })
        # The balance hedge CROSSES the spread, which makes it a taker order,
        # and takers pay crypto_fees_v2: shares * 0.07 * p * (1-p). This is the
        # one place the "makers pay no fee" premise does not hold, and the fee
        # peaks at p=0.50 (1.75c/share) -- right where this strategy trades, and
        # several times the ~1c edge on a hedged pair. Charging it here is what
        # stops the hedge from looking free.
        if f.get("crossed"):
            m["fee"] += taker_fee(f["price"] or 0.0, f["size"] or 0.0)
            m["crossed_sh"] += f["size"] or 0.0
        if f["side"] == "UP":
            m["up_sh"] += f["size"]; m["up_cost"] += f["size"] * f["price"]
        else:
            m["dn_sh"] += f["size"]; m["dn_cost"] += f["size"] * f["price"]
        m["fills"] += 1
        m["tokens"][f["side"]] = f["token_id"]

    settled, pnls, balances, pairs = [], [], [], []
    for cond, m in by_mkt.items():
        hi = max(m["up_sh"], m["dn_sh"])
        if hi > 0:
            balances.append(min(m["up_sh"], m["dn_sh"]) / hi)
        if m["up_sh"] > 0 and m["dn_sh"] > 0:
            pairs.append(m["up_cost"] / m["up_sh"] + m["dn_cost"] / m["dn_sh"])
        win = res.get(cond)
        if not win:
            continue
        payout = 0.0
        for side in ("UP", "DOWN"):
            tok = m["tokens"].get(side)
            sh = m["up_sh"] if side == "UP" else m["dn_sh"]
            if tok and tok == win:
                payout += sh
        c = m["up_cost"] + m["dn_cost"]
        # Maker fills pay no fee; crossed balance-hedge fills do. m["fee"] is 0
        # for any market we never had to hedge.
        pnl = payout - c - m["fee"]
        pnls.append(pnl)
        settled.append({"slug": m["slug"], "cost": c, "payout": payout,
                        "pnl": pnl, "fills": m["fills"], "fee": m["fee"],
                        "crossed_sh": m["crossed_sh"],
                        "ts": res_ts.get(cond) or 0,
                        "up_sh": m["up_sh"], "dn_sh": m["dn_sh"],
                        "balance": (min(m["up_sh"], m["dn_sh"]) / hi) if hi else 1.0})

    realized = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    # --- rebate estimate ---------------------------------------------------
    # Rebate pool = rebate_rate x taker fees on matched volume. We only see our
    # own side, so this is an ESTIMATE and is reported separately, never added
    # into realized PnL.
    # Crossed fills are excluded: a rebate is paid to MAKERS. Crediting our own
    # taker orders with a maker rebate would pay us for the leg we are also
    # being charged a fee on.
    rebate_est = sum(
        taker_fee(f["price"] or 0, f["size"] or 0) * cfg.rebate_rate
        for f in fills if not f.get("crossed")
    )

    ts = [f["ts"] for f in fills if f.get("ts")]
    days = ((max(ts) - min(ts)) / 86400) if len(ts) > 1 else 0.0

    # --- fill rate vs QUEUE DEPTH -----------------------------------------
    # The single most useful maker diagnostic: it says whether we are failing
    # to fill because the strategy is wrong or simply because we keep joining
    # behind 400 shares. Bucketed off quotes.queue_ahead, which is written at
    # post time, so every bucket traces to real rows.
    q_buckets = [(0, 1), (1, 50), (50, 150), (150, 400), (400, 1e12)]
    fill_by_queue = []
    for lo, hi in q_buckets:
        b = [q for q in quotes if lo <= (q.get("queue_ahead") or 0) < hi]
        posted_b = sum(q["size"] or 0 for q in b)
        filled_b = sum(q["filled"] or 0 for q in b)
        if b:
            fill_by_queue.append({
                "label": (f"{lo:.0f}-{hi:.0f}" if hi < 1e11 else f"{lo:.0f}+"),
                "quotes": len(b), "posted": posted_b, "filled": filled_b,
                "fill_rate": (filled_b / posted_b) if posted_b else None,
            })

    # --- PARTIAL FILL exposure --------------------------------------------
    # The documented loss driver. A quote that filled some but not all of its
    # size leaves an unpaired position that settles one-sided.
    partial = [q for q in quotes
               if 0 < (q["filled"] or 0) < (q["size"] or 0) - 1e-9]
    fully = [q for q in quotes if (q["filled"] or 0) >= (q["size"] or 0) - 1e-9
             and (q["filled"] or 0) > 0]
    unfilled_of_partials = sum((q["size"] or 0) - (q["filled"] or 0)
                               for q in partial)

    # --- QUOTE UPTIME -------------------------------------------------------
    # Fraction of observed market-time we actually had a quote resting. A
    # maker that is not on the book cannot be filled, so a low fill rate means
    # something very different at 20% uptime than at 95%.
    dec_rows = _rows("SELECT action, count, t_remaining FROM decisions")
    dec_total = sum((d["count"] or 1) for d in dec_rows)
    dec_quoting = sum((d["count"] or 1) for d in dec_rows
                      if d["action"] == "QUOTE")
    quote_uptime = (dec_quoting / dec_total) if dec_total else None
    skip_reasons = {}
    for d in _rows("SELECT action, reason, count FROM decisions "
                   "WHERE action <> 'QUOTE'"):
        skip_reasons[d["reason"] or d["action"]] = (
            skip_reasons.get(d["reason"] or d["action"], 0) + (d["count"] or 1))
    top_skips = sorted(skip_reasons.items(), key=lambda kv: -kv[1])[:6]

    # --- fill PROVENANCE ----------------------------------------------------
    # 'sweep' fills are credited off a level emptying, which a mass cancel
    # produces just as well as a trade. Measured on recorded books, the
    # book-only model attributed 100% of fills to that branch and reported a
    # 50% fill rate where the tape-confirmed rate was 3%. Never present one
    # number without saying which kind it is.
    prov: dict[str, float] = {}
    for f in fills:
        prov[f.get("reason") or "queue"] = (
            prov.get(f.get("reason") or "queue", 0.0) + (f["size"] or 0))

    # --- how big a sample do we actually need? -----------------------------
    # Question: is mean P&L per market reliably above zero? For a mean, the CI
    # half-width is z*sigma/sqrt(n). It excludes zero once
    #     n > (z * sigma / |mean|)^2
    # sigma and mean are estimated from what we've settled so far, so these
    # targets MOVE as the data comes in -- that's expected, not a bug. A noisy
    # strategy (big sigma relative to its edge) needs far more markets.
    sample = {"n": len(pnls), "mean": None, "stdev": None, "targets": {}}
    if len(pnls) >= 2:
        mu = statistics.mean(pnls)
        sd = statistics.stdev(pnls)
        sample["mean"] = mu
        sample["stdev"] = sd
        for label, z in (("90%", 1.645), ("95%", 1.960), ("99%", 2.576)):
            if abs(mu) < 1e-9 or sd <= 0:
                need = None
            else:
                need = int((z * sd / abs(mu)) ** 2) + 1
            sample["targets"][label] = {
                "need": need,
                "remaining": (max(0, need - len(pnls)) if need else None),
                "reached": (need is not None and len(pnls) >= need),
            }
        # rough ETA from observed market pace
        if days > 0.01 and len(by_mkt) > 0:
            per_day = len(by_mkt) / days
            sample["markets_per_day"] = per_day
            for label in sample["targets"]:
                rem = sample["targets"][label]["remaining"]
                sample["targets"][label]["eta_hours"] = (
                    round(rem / per_day * 24, 1) if (rem and per_day > 0) else 0
                )

    # --- decisive experiment readout ------------------------------------
    # Phase A (census): fraction of observed markets where a fillable sub-$1.00
    # pair exists. Below hedge_fillable_min_rate the instrument can't be made
    # profitably -> DEAD. Phase B (verdict): after the census passes, settle
    # experiment_verdict_markets and read P&L sign + confidence.
    census = {"markets_observed": 0, "fillable": 0, "fillable_rate": None,
              "median_pair_at_touch": None}
    try:
        crows = _rows("SELECT pair_cost_at_touch, fillable_sub_one FROM hedge_census")
        if crows:
            census["markets_observed"] = len(crows)
            census["fillable"] = sum(1 for r in crows if r["fillable_sub_one"])
            census["fillable_rate"] = (
                census["fillable"] / census["markets_observed"]
                if census["markets_observed"] else None)
            pairs = sorted(r["pair_cost_at_touch"] for r in crows
                           if r["pair_cost_at_touch"] is not None)
            if pairs:
                census["median_pair_at_touch"] = statistics.median(pairs)
    except Exception:
        pass

    # Phase status for the dashboard banner.
    if pnls:
        mu = statistics.mean(pnls) if len(pnls) >= 2 else pnls[0]
        sd = statistics.stdev(pnls) if len(pnls) >= 2 else 0.0
    else:
        mu = sd = 0.0
    phase = "A_CENSUS"
    if census["markets_observed"] >= cfg.experiment_census_markets \
            and (census["fillable_rate"] or 0) >= cfg.hedge_fillable_min_rate:
        phase = "B_VERDICT"
    if len(pnls) >= cfg.experiment_verdict_markets:
        phase = "DONE"

    return {
        # pace (compare against powerwinner: 437 mkts/day, 8351 fills/day)
        "markets_quoted": len({q["condition_id"] for q in quotes}),
        "rewards": _reward_report(),
        "markets_filled": len(by_mkt),
        "markets_settled": len(settled),
        "fills": len(fills),
        "quotes": len(quotes),
        "days": days,
        "fills_per_day": (len(fills) / days) if days > 0.01 else None,
        "notional_per_day": (cost / days) if days > 0.01 else None,

        # THE maker metric. Crossed shares are excluded from the numerator:
        # they were TAKEN, not waited for, so counting them would inflate the
        # one number that says whether resting orders actually get filled.
        "fill_rate": ((filled_sh - crossed_sh) / posted_sh) if posted_sh else None,
        "posted_shares": posted_sh,
        "filled_shares": filled_sh,
        "crossed_shares": crossed_sh,
        "taker_fees_paid": sum(m["fee"] for m in by_mkt.values()),
        "median_seconds_to_fill": statistics.median(waits) if waits else None,
        "median_queue_ahead": statistics.median(queues) if queues else None,

        # the edge
        "spread_capture": spread_capture,
        "spread_capture_per_share": (spread_capture / filled_sh) if filled_sh else None,
        "avg_edge_cents": (statistics.mean(edges) * 100) if edges else None,
        "cost": cost,

        # maker diagnostics -- each traces to rows in quotes/fills/decisions
        "fill_by_queue": fill_by_queue,
        "fill_provenance": prov,
        "partial_quotes": len(partial),
        "full_quotes": len(fully),
        "partial_fill_shares_missing": unfilled_of_partials,
        "quote_uptime": quote_uptime,
        "top_skip_reasons": [{"reason": r, "cycles": n} for r, n in top_skips],
        "pair_cost_distribution": sorted(pairs),

        # inventory discipline
        "median_balance": statistics.median(balances) if balances else None,
        "median_pair_cost": statistics.median(pairs) if pairs else None,
        "pairs_under_1": (100 * sum(1 for p in pairs if p < 1.0) / len(pairs)) if pairs else None,

        # outcome
        "realized_pnl": realized,
        "wins": len(wins), "losses": len(losses),
        "win_rate": (len(wins) / len(pnls)) if pnls else None,
        "avg_win": statistics.mean(wins) if wins else 0.0,
        "avg_loss": statistics.mean(losses) if losses else 0.0,
        "roi_on_cost": (realized / cost) if cost else None,

        # adverse selection: spread we captured vs what direction cost us
        "adverse_selection": realized - spread_capture,
        "rebate_est": rebate_est,
        "total_with_rebate": realized + rebate_est,

        "equity": cfg.bankroll_usd + realized,
        "bankroll": cfg.bankroll_usd,
        "sample": sample,
        "census": census,
        "balance_hedges": _rows(
            "SELECT count(*) AS n FROM decisions WHERE action='CROSS_HEDGE'")[0]["n"],
        "experiment": {
            "phase": phase,
            "census_markets": cfg.experiment_census_markets,
            "verdict_markets": cfg.experiment_verdict_markets,
            "min_fillable_rate": cfg.hedge_fillable_min_rate,
            "mean_per_market": mu if pnls else None,
            "stdev_per_market": sd if pnls else None,
        },
        "settlements": sorted(settled, key=lambda x: -x.get("ts", 0))[:60] or settled[:60],
    }


def recent_decisions(limit: int = 60) -> list[dict]:
    return _rows("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))


def recent_fills(limit: int = 40) -> list[dict]:
    return _rows("SELECT * FROM fills ORDER BY id DESC LIMIT ?", (limit,))


def recent_quotes(limit: int = 40) -> list[dict]:
    return _rows("SELECT * FROM quotes ORDER BY id DESC LIMIT ?", (limit,))
