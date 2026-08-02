"""One dashboard for the whole fleet: aggregate on top, per-market below.

Replaces four separate pages on four ports. The aggregate strip answers "is
this working overall", the table answers "which market is carrying it" -- and
with 20 markets the second question is the one that matters, because income is
concentrated: measured, a single market can be a third of the total.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from strategy import store
from strategy.config import load as load_config
from strategy.kpi import taker_fee

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "run"
DB = RUN / "fleet.db"
CFG = load_config()
# A 20-market sweep normally takes about 50-70 seconds at the public-book
# polling interval. Give one slow sweep room before calling the fleet dead;
# otherwise a healthy fleet flashes STALE between every state-file write.
STALE_AFTER_SEC = 120.0

app = FastAPI(title="maker fleet")


def _run_started() -> float | None:
    """When this run began, as a unix timestamp, or None before any data.

    Taken from the DB rather than from this process's start time, because the
    supervisor restarts the dashboard independently of the fleet -- a clock
    anchored to module import would silently reset to zero on a dashboard
    crash and report a fresh run that never happened. The first reward sample
    is written on the first visit to the first market, so it is the earliest
    honest moment to call the run started.
    """
    if not DB.exists():
        return None
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        r = c.execute("SELECT MIN(ts) FROM reward_samples").fetchone()
        c.close()
        return r[0] if r and r[0] else None
    except Exception:
        # A brand-new DB has no reward_samples table yet. That is "not started",
        # not an error worth surfacing on the page.
        return None


def _db_heartbeat() -> float | None:
    """Most recent write timestamp from the fleet DB, if it has started."""
    if not DB.exists():
        return None
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        row = c.execute(
            "SELECT MAX(ts) FROM ("
            "SELECT ts FROM reward_samples "
            "UNION ALL SELECT ts FROM fill_evidence "
            "UNION ALL SELECT ts FROM live_state"
            ")"
        ).fetchone()
        c.close()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def _db_stats() -> dict:
    """Per-market history from the fleet DB. Empty dict if it has not run."""
    if not DB.exists():
        return {}
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        out: dict = {}
        for r in c.execute(
                "SELECT condition_id, COUNT(*) n, AVG(our_share) share, "
                "MIN(ts) t0, MAX(ts) t1, "
                "SUM(CASE WHEN our_score>0 THEN 1 ELSE 0 END) scoring "
                "FROM reward_samples GROUP BY condition_id"):
            out[r["condition_id"]] = {
                "samples": r["n"], "avg_share": r["share"] or 0.0,
                "uptime": (r["scoring"] / r["n"]) if r["n"] else 0.0,
                "hours": (r["t1"] - r["t0"]) / 3600.0,
            }
        for r in c.execute("SELECT condition_id, COUNT(*) n, SUM(size) sh, "
                           "SUM(size*price) cost FROM fills GROUP BY condition_id"):
            out.setdefault(r["condition_id"], {}).update(
                {"fills": r["n"], "shares": r["sh"] or 0, "cost": r["cost"] or 0})
        # Closes reduce the position (fills alone overstate what is still
        # held) and book their own realized money -- both need to be visible
        # per-market, not just folded into a fleet-wide total.
        for r in c.execute(
                "SELECT condition_id, COUNT(*) n, SUM(shares) sh, "
                "SUM(realized_pnl) pnl, SUM(forgone_vs_settlement) forgone "
                "FROM closes GROUP BY condition_id"):
            out.setdefault(r["condition_id"], {}).update({
                "closes": r["n"], "closed_shares": r["sh"] or 0,
                "closed_pnl": r["pnl"] or 0.0,
                "closed_forgone": r["forgone"] or 0.0,
            })
        c.close()
        return out
    except Exception:
        return {}


def _maker_rebate(db: Path | None = None) -> dict:
    """Maker Rebates earned on matched volume. NOT the liquidity-reward pot.

    Two venue programs pay a maker, and the dashboard had only ever wired one:

      * LIQUIDITY REWARDS pay for RESTING size, sampled once a minute, filled
        or not. That is `rent_reward`, and it reads $0.00 because every market
        the fleet currently holds publishes clobRewards: 0 -- the program is
        not funded on them. The zero is the truth, not a missing wire.
      * MAKER REBATES pay a share of the taker fee on volume we MADE. An
        unfilled resting order earns exactly zero here no matter how long it
        rests, which is why no amount of uptime moves this number.

    So this is a fills query, not a score-share integral. Quoting both off the
    resting-size formula is the trap: applied to a spread market it multiplies
    a spread-capture PROJECTION by uptime and reports it as a venue
    distribution, double-counting income booked P&L already holds the moment
    the fill lands.

    Crossed fills are excluded because we were the taker on them: crediting our
    own aggressive leg with a maker rebate would pay us for the side we are
    also being charged the fee on.

    `taker_fee` is imported rather than re-derived -- kpi.py already owns the
    crypto_fees_v2 curve, and a second copy is a second thing to get wrong.
    """
    out = {"earned": 0.0, "shares": 0.0, "fills": 0, "per_share_cents": None}
    path = DB if db is None else Path(db)
    if not path.exists():
        return out
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = c.execute("SELECT price, size FROM fills "
                         "WHERE crossed = 0 OR crossed IS NULL").fetchall()
        c.close()
    except Exception:
        # Same contract as every other reader here: one unreadable metric must
        # not blank the page.
        return out
    for price, size in rows:
        out["earned"] += taker_fee(price or 0.0, size or 0.0) * CFG.rebate_rate
        out["shares"] += size or 0.0
        out["fills"] += 1
    if out["shares"] > 0:
        out["per_share_cents"] = 100.0 * out["earned"] / out["shares"]
    return out


def _realized() -> dict:
    """Settled P&L from the fleet DB: payout - cost, per resolved market.

    Deliberately separate from the rent projection. Rent is what the venue is
    expected to pay for resting size; this is money the book has already
    decided. Returns zeros rather than None when nothing has resolved, so the
    tile renders a real number instead of a blank that reads as "unknown".

    Closes are folded in here because they change BOTH sides of the payout:
    a pair sold before resolution (a) no longer collects the $1 resolution
    credit for the shares that were sold, and (b) already booked its own
    realized_pnl at close time. Crediting the full fill count at resolution
    while ALSO ignoring closes would double-count money that was never paid
    (the resolution credit) while omitting money that actually was (the
    close proceeds) -- a payout that never happened, on a number the venue
    never sent.
    """
    out = {"realized": 0.0, "settled": 0, "wins": 0, "losses": 0, "cost": 0.0,
           "closes": 0, "closed_pnl": 0.0, "closed_forgone": 0.0}
    if not DB.exists():
        return out
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        res = {r["condition_id"]: r["winning_token"]
               for r in c.execute(
                   "SELECT condition_id, winning_token FROM resolutions")}
        by: dict[str, dict] = {}
        for r in c.execute(
                "SELECT condition_id, token_id, size, price FROM fills"):
            m = by.setdefault(r["condition_id"], {"cost": 0.0, "tok": {}})
            m["cost"] += (r["size"] or 0) * (r["price"] or 0)
            m["tok"][r["token_id"]] = (m["tok"].get(r["token_id"], 0.0)
                                       + (r["size"] or 0))
        closes: dict[str, dict] = {}
        for r in c.execute(
                "SELECT condition_id, COUNT(*) n, SUM(shares) sh, "
                "SUM(cost_basis) cb, SUM(realized_pnl) pnl, "
                "SUM(forgone_vs_settlement) forgone FROM closes "
                "GROUP BY condition_id"):
            closes[r["condition_id"]] = {
                "n": r["n"], "shares": r["sh"] or 0.0,
                "cost_basis": r["cb"] or 0.0, "pnl": r["pnl"] or 0.0,
                "forgone": r["forgone"] or 0.0,
            }
        c.close()
        for cond, m in by.items():
            win = res.get(cond)
            if not win:
                continue
            cl = closes.get(cond, {"n": 0, "shares": 0.0, "cost_basis": 0.0,
                                    "pnl": 0.0, "forgone": 0.0})
            # Resolution only pays for shares still held: one UP + one DOWN
            # were sold per pair closed, so the winning token's fill count
            # must drop by the same amount before the $1 credit is applied.
            held_win_shares = m["tok"].get(win, 0.0) - cl["shares"]
            # cost is every dollar ever spent on fills in this market. The
            # portion already removed by closes (cost_basis) must come back
            # out here too, or it is charged against P&L twice: once inside
            # the close's own realized_pnl, and again here against a payout
            # those shares no longer collect.
            remaining_cost = m["cost"] - cl["cost_basis"]
            pnl = held_win_shares - remaining_cost + cl["pnl"]
            out["realized"] += pnl
            out["cost"] += m["cost"]
            out["settled"] += 1
            out["wins" if pnl > 0 else "losses"] += 1
            out["closes"] += cl["n"]
            out["closed_pnl"] += cl["pnl"]
            out["closed_forgone"] += cl["forgone"]
        # Closes on markets that have NOT yet resolved still book real,
        # already-realized money -- count them too, so an operator sees the
        # close activity even before the underlying market settles.
        for cond, cl in closes.items():
            if cond in res:
                continue
            out["realized"] += cl["pnl"]
            out["closes"] += cl["n"]
            out["closed_pnl"] += cl["pnl"]
            out["closed_forgone"] += cl["forgone"]
    except Exception:
        pass
    return out


def _markout_stats() -> dict:
    """Cost-of-fill per market, straight from the markouts table.

    Read here rather than taken from live state so the figure survives a bot
    restart -- the fills happened whether or not the process that recorded
    them is still running.
    """
    out: dict = {"by_market": {}, "total": 0.0, "spread": 0.0, "n": 0}
    if not DB.exists():
        return out
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        for r in c.execute(
                "SELECT condition_id, side, fill_price, size, ref_mid, "
                "ref_mid_source, mid_h0, mid_h1, mid_h2 FROM markouts"):
            if r["ref_mid_source"] == "contaminated":
                continue
            mid = next((r[f"mid_h{i}"] for i in (2, 1, 0)
                        if r[f"mid_h{i}"] is not None), None)
            if mid is None:
                continue          # no horizon matured yet
            # DRIFT, not total. Total includes the ~2c we quote under mid, so
            # a market that never moved reads +2.15c and looks like edge.
            # Drift is the market moving against us, which is what deserves a
            # tile. Captured spread is reported separately, not blended in.
            ref = r["ref_mid"]
            if ref is None:
                continue
            drift = mid - ref
            spread = ref - (r["fill_price"] or 0.0)
            b = out["by_market"].setdefault(
                r["condition_id"], {"sum": 0.0, "n": 0})
            b["sum"] += drift
            b["n"] += 1
            out["total"] += drift * (r["size"] or 0.0)
            out["spread"] += spread * (r["size"] or 0.0)
            out["n"] += 1
        c.close()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    for cid, b in out["by_market"].items():
        b["mean_per_share"] = b["sum"] / b["n"] if b["n"] else None
    return out


def _share_history(n: int = 24) -> list[float]:
    """Fleet-wide avg our_share, one point per hour, most recent n hours.

    Not a dollar series -- no $ is persisted per sample (reward_samples only
    stores our_share), so this is the raw signal the $ estimates are built
    FROM: how much of the book the fleet held, over time. Sparkline data for
    the hero, not a ledger.
    """
    if not DB.exists():
        return []
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        rows = c.execute(
            "SELECT CAST(ts/3600 AS INTEGER) hr, AVG(our_share) s "
            "FROM reward_samples GROUP BY hr ORDER BY hr").fetchall()
        c.close()
        return [r[1] or 0.0 for r in rows[-n:]]
    except Exception:
        return []


@app.get("/api/fleet")
def fleet():
    f = RUN / "fleet_state.json"
    if not f.exists():
        return {"markets": [], "error": "fleet not running (run/fleet_state.json missing)"}
    specs = json.loads(f.read_text(encoding="utf-8"))
    hist = _db_stats()
    mk = _markout_stats()
    now = time.time()
    live_ts = max((s.get("_live", {}).get("ts", 0) or 0
                   for s in specs), default=0.0)
    db_ts = _db_heartbeat() or 0.0
    # The state file is the primary fleet heartbeat: it is written only after
    # a complete sweep. DB writes are useful diagnostics, but historical DB
    # activity must not make a dead fleet look LIVE.
    heartbeat_ts = live_ts or (f.stat().st_mtime if f.exists() else 0.0)
    state_age = now - heartbeat_ts if heartbeat_ts else None
    fleet_stale = state_age is None or state_age > STALE_AFTER_SEC

    rows = []
    for s in specs:
        live = s.get("_live") or {}
        h = hist.get(s["cid"], {})
        rows.append({
            "title": s["title"], "slug": s.get("slug", ""),
            "url": f"https://polymarket.com/market/{s['slug']}" if s.get("slug") else "",
            # THE POT THAT ACTUALLY PAYS THIS MARKET. `spec["daily"]` is the
            # reward pot alone and reads 0 for every spread market, which made
            # the whole table report $0.00/day on markets that were filling.
            # The fleet publishes the effective pot in live state; the spec
            # figure is the fallback for a market not yet visited.
            "daily": live.get("pot", s["daily"]),
            "reward_daily": s["daily"],
            "source": live.get("source", s.get("source",
                                               "rewards" if s["daily"] > 0
                                               else "spread")),
            "min_size": s["min_size"],
            "max_spread": s["max_spread"],
            "share": live.get("share", 0.0),
            "income": live.get("income", 0.0),
            # `capital` is resting offers only. `committed` includes offers
            # plus inventory cost, so the table uses the same denominator as
            # the wallet gauge instead of silently understating exposure.
            "capital": live.get("capital", 0.0),
            "committed": (live.get("capital", 0.0)
                          + (live.get("naked_cost", 0.0) or 0.0)
                          + (live.get("pair_paid", 0.0) or 0.0)),
            "quotes": live.get("quotes", []),
            "fills": h.get("fills", 0),
            "uptime": h.get("uptime", 0.0),
            "samples": h.get("samples", 0),
            # Estimate, not a ledger entry: no dollar amount is persisted per
            # sample (reward_samples only stores our_share), so this assumes
            # today's funded daily rate held constant over the whole window --
            # same assumption the live "income" projection already makes.
            "collected_rent": h.get("avg_share", 0.0)
                              * (live.get("pot", s["daily"]) or 0.0)
                              * (h.get("hours", 0.0) / 24.0),
            "age": (now - live["ts"]) if live.get("ts") else None,
            "err": live.get("err") or "",
            "why": live.get("why") or "",
            # price-ladder fields, all on the UP axis
            "up_bid": live.get("up_bid"), "up_ask": live.get("up_ask"),
            "mid_up": live.get("mid_up"), "our_up": live.get("our_up"),
            "our_dn_as_up": live.get("our_dn_as_up"),
            "dn_bid_as_up": live.get("dn_bid_as_up"),
            "pair_cost": live.get("pair_cost"),
            # position: paired shares are safe (always pay $1), naked shares
            # are the only thing that can lose (pay $1 or $0)
            "paired": live.get("paired", 0.0),
            "naked_side": live.get("naked_side", ""),
            "naked_sh": live.get("naked_sh", 0.0),
            "naked_cost": live.get("naked_cost", 0.0),
            "pair_paid": live.get("pair_paid", 0.0),
            # What the naked leg fetches if sold at the current best bid right
            # now, instead of waiting to be paid $1 or $0 at resolution. UP
            # sells against up_bid directly; DOWN sells against dn_bid, which
            # is carried on the UP axis as dn_bid_as_up = 1 - dn_bid, so it is
            # un-folded back here. None (no live bid) means "can't exit right
            # now" -- valued at 0, same as the worst-case resolution number,
            # not blended into a guess.
            "naked_exit_value": (
                live.get("naked_sh", 0.0) * live["up_bid"]
                if live.get("naked_side") == "UP" and live.get("up_bid") is not None
                else live.get("naked_sh", 0.0) * (1.0 - live["dn_bid_as_up"])
                if live.get("naked_side") == "DOWN" and live.get("dn_bid_as_up") is not None
                else 0.0
            ),
            # cost of being filled -- the half of EV the rent line ignores
            "gate": live.get("gate", "NORMAL"),
            "markout": mk["by_market"].get(s["cid"], {}).get("mean_per_share"),
            "markout_n": mk["by_market"].get(s["cid"], {}).get("n", 0),
            # Profit-take closes: shares already sold early, and why -- an
            # operator watching positions shrink with no explanation is the
            # exact gap this closes.
            "closes": h.get("closes", 0),
            "closed_pnl": h.get("closed_pnl", 0.0),
            "closed_forgone": h.get("closed_forgone", 0.0),
            "close_why": live.get("close_why") or "",
            # U2. Merge is the exit that actually fires -- the sell path's
            # ceiling is -0.007/share against a +0.020 threshold, which is why
            # `closes` sat at zero for 18.7 hours. Reported separately so a
            # reader can see which mechanism released the capital.
            "merge_why": live.get("merge_why") or "",
            "merged_shares": live.get("merged_shares", 0.0),
            "recycled_usd": live.get("recycled_usd", 0.0),
            "pairing_rate": live.get("pairing_rate"),
        })
    rows.sort(key=lambda r: -r["income"])

    scoring = [r for r in rows if r["income"] > 0]
    cap = sum(r["capital"] for r in rows)
    inc = sum(r["income"] for r in rows)
    total_collected_rent = sum(r["collected_rent"] for r in rows)
    rz = _realized()

    # The projection integrated over the time it was actually held, rather
    # than whatever it happens to read this second.
    try:
        accrual = store.income_accrual()
    except Exception:
        accrual = {"accrued": 0.0, "twa_day": None, "hours": 0.0, "n": 0}

    rebate = _maker_rebate()
    merged_total = sum(r["merged_shares"] for r in rows)
    try:
        vr = store.verified_ratio()
    except Exception:
        # A dashboard that cannot read one metric must still render the rest.
        vr = {"verified_fills": 0, "verified_shares": 0.0,
              "unverified_fills": 0, "unverified_shares": 0.0,
              "unverified_sweep_shares": 0.0, "ratio": None}

    locked = sum((r["paired"] or 0) * 1.0 - (r["pair_paid"] or 0) for r in rows)
    at_risk = sum(r["naked_cost"] or 0 for r in rows)
    # Naked value at the current bid, not the $1/$0 resolution outcome --
    # what selling out actually raises if it happened this second.
    naked_exit_total = sum(r["naked_exit_value"] for r in rows)
    # Unfunded now means "no pot from EITHER source". A spread market has no
    # reward rate by definition, and counting those as unfunded reported the
    # entire working universe as dead capital.
    unfunded = [r for r in rows if not (r["daily"] or 0) > 0]
    spread_rows = [r for r in rows if r["source"] == "spread"]
    committed_total = cap + at_risk + sum(r["pair_paid"] or 0 for r in rows)
    available_cash = max(0.0, CFG.bankroll_usd - committed_total)
    committed_overage = max(0.0, committed_total - CFG.max_committed_usd)

    return {
        "now": now,
        "run_started": _run_started(),
        "markets": rows,
        "share_history": _share_history(),
        "totals": {
            "markets": len(rows),
            "scoring": len(scoring),
            "income_day": inc,
            "income_hour": inc / 24.0,
            "collected_rent_total": total_collected_rent,
            # RENT SPLIT BY WHETHER IT IS OWED TO US OR MERELY MODELLED.
            #
            # Reward rent is money the venue distributes for resting size. It
            # is earned but not yet in the wallet, so it is a genuine P&L term
            # the headline is missing.
            #
            # Spread "rent" is not a distribution at all -- it is a projection
            # of income that arrives BY BEING FILLED, and those same dollars
            # are already counted in booked P&L and pair P&L the moment a fill
            # happens. Adding it would book the same income twice, which is
            # exactly the double-count that makes a paper strategy look
            # profitable when it is not.
            # MODELLED INCOME ACCRUED, integrated over time. Replaces the old
            # `collected_rent_total`, which multiplied today's pot by the whole
            # run's hours and so rewrote history every time a pot moved.
            "income_accrued": accrual["accrued"],
            "income_twa_day": accrual["twa_day"],
            "income_hours": accrual["hours"],
            "income_samples": accrual["n"],
            "rent_reward": sum(r["collected_rent"] for r in rows
                               if r["source"] == "rewards"),
            # THE OTHER PROGRAM. `rent_reward` above is liquidity rewards, paid
            # for resting size, and it is $0.00 whenever the fleet holds only
            # clobRewards: 0 markets. This is Maker Rebates, paid as a share of
            # the taker fee on volume we MADE -- disjoint from the pot, so the
            # two add without double-counting, and additive to booked P&L
            # because a rebate is money the venue sends on top of the fill.
            "maker_rebate": rebate["earned"],
            "maker_rebate_shares": rebate["shares"],
            "maker_rebate_fills": rebate["fills"],
            "maker_rebate_cps": rebate["per_share_cents"],
            "rent_modelled_spread": sum(r["collected_rent"] for r in rows
                                        if r["source"] == "spread"),
            "unfunded": len(unfunded),
            "realized": rz["realized"],
            "settled": rz["settled"],
            "wins": rz["wins"],
            "losses": rz["losses"],
            "closes": rz["closes"],
            "closed_pnl": rz["closed_pnl"],
            "closed_forgone": rz["closed_forgone"],
            "locked_pair": locked,
            # The pieces `locked_pair` is made of, published separately so the
            # page can show the arithmetic instead of one net figure labelled
            # as though it were a holding. A reader seeing -$13.59 under
            # "matched shares valued at $1" cannot tell that the shares are
            # worth $571 and cost $584.59 -- which is the actual news.
            "pair_value": sum((r["paired"] or 0) * 1.0 for r in rows),
            "pair_paid": sum(r["pair_paid"] or 0 for r in rows),
            "naked_exit": naked_exit_total,
            "at_risk": at_risk,
            "net_worst": rz["realized"] + locked - at_risk,
            # Liquidate & cancel everything: booked P&L + pairs merged ($1 each)
            # + naked shares sold at current bid - cost of naked shares.
            "liquidate_now_pnl": rz["realized"] + locked
                                 + (naked_exit_total - at_risk),
            # Cash currently locked in active limit orders (released 100% on cancel)
            "locked_bids_cash": cap,
            "markout_total": mk["total"],
            "markout_spread": mk["spread"],
            "markout_n": mk["n"],
            # THE MEASURED ANSWER, as opposed to the modelled one. Spread
            # capture is a projection until a fill proves it: `markout_spread`
            # is the edge actually captured on filled shares (mid minus what
            # we paid) and `markout_total` is the market then moving against
            # us. Their sum is what being filled was worth in dollars, and it
            # is the number that decides whether this strategy makes money.
            "fill_edge": mk["spread"] + mk["total"],
            "income_spread": sum(r["income"] for r in spread_rows),
            "income_reward": inc - sum(r["income"] for r in spread_rows),
            "markets_spread": len(spread_rows),
            "fleet_naked_budget": CFG.max_fleet_naked_usd,
            "wallet": CFG.bankroll_usd,
            "committed_total": committed_total,
            "available_cash": available_cash,
            "committed_overage": committed_overage,
            "max_committed": CFG.max_committed_usd,
            "state_age": state_age,
            "db_age": (now - db_ts) if db_ts else None,
            "heartbeat_ts": heartbeat_ts or None,
            "fleet_stale": fleet_stale,
            "exited": len([r for r in rows if r["gate"] == "EXITED"]),
            "widened": len([r for r in rows if r["gate"] == "WIDENED"]),
            "capital": cap,
            # Honest wallet return: offers-only was the old misleading
            # denominator. Open inventory is committed capital too.
            "return_pct_day": (100 * inc / committed_total)
                               if committed_total else 0.0,
            "merged_shares": merged_total,
            "recycled_usd": sum(r["recycled_usd"] for r in rows),
            "pairing_rate": ((merged_total / vr["verified_shares"])
                             if vr["verified_shares"] > 1e-9 else None),
            "verified": vr,
            "funded_total": sum(r["daily"] for r in rows),
            "fills": sum(r["fills"] for r in rows),
            "uptime": (sum(r["uptime"] for r in rows) / len(rows)) if rows else 0,
            "concentration": (max((r["income"] for r in rows), default=0) / inc)
                             if inc else 0,
        },
    }


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Maker Fleet</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
 :root{
   --bg:#0a0d12; --panel:#12161d; --panel-2:#171c24; --line:#232a35; --line-soft:#1a2029;
   --tx:#e7ebf3; --tx-dim:#8792a6; --tx-faint:#535e70;
   --up:#33c9b5; --up-soft:#12302c;
   --down:#f0684d; --down-soft:#3a201a;
   --gold:#e8b84b; --gold-soft:#3a2f18;
   --proj:#7b9bf7; --proj-soft:#1c2540;
   --alert:#ff5c5c;
   --r-lg:12px; --r-md:8px; --r-sm:5px;
   --disp:'Space Grotesk',system-ui,sans-serif;
   --mono:'IBM Plex Mono',ui-monospace,Menlo,Consolas,monospace;
   --body:'IBM Plex Sans',system-ui,-apple-system,"Segoe UI",sans-serif;
 }
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.5 var(--body);
      -webkit-font-smoothing:antialiased}
 a{color:inherit}
 a:focus-visible,button:focus-visible{outline:2px solid var(--proj);outline-offset:2px}
 .up{color:var(--up)}.down{color:var(--down)}.gold{color:var(--gold)}
 .proj{color:var(--proj)}.alert-tx{color:var(--alert)}.dim{color:var(--tx-dim)}
 .bold{font-weight:600}.mono{font-family:var(--mono)}
 .num{text-align:right;font-variant-numeric:tabular-nums}

 /* ---------- masthead ---------- */
 .mast{display:flex;align-items:center;gap:14px;padding:14px 24px;
       background:var(--panel);border-bottom:1px solid var(--line)}
 .mast-id{font-family:var(--disp);font-weight:700;font-size:16px;letter-spacing:.01em}
 .mast-id b{color:var(--gold)}
 .tag{border:1px solid var(--down);color:var(--down);border-radius:99px;
      padding:3px 10px;font-size:11px;font-weight:600;letter-spacing:.06em}
 .legend{display:flex;gap:14px;font-size:11px;color:var(--tx-dim);letter-spacing:.02em}
 .legend span{display:inline-flex;align-items:center;gap:5px}
 .legend i{width:7px;height:7px;border-radius:50%;display:inline-block}
 .live{font-size:12px;font-weight:600}
 .clock{font-family:var(--mono);font-size:12px;color:var(--tx-dim)}

 /* ---------- hero ---------- */
 /* The equation and the strip live in hero-main, so main takes the width and
    the rail is capped. It was the other way round, which squeezed a one-line
    sum into four wrapped lines while six rank bars -- five of them $0.00 --
    stretched across two thirds of the screen. */
 .hero{display:grid;grid-template-columns:1fr minmax(300px,380px);gap:1px;
       background:var(--line);border-bottom:1px solid var(--line)}
 .hero-main{background:var(--panel);padding:24px 28px}
 .hero-eyebrow{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--tx-dim);font-weight:600}
 .hero-duo{display:flex;flex-wrap:wrap;gap:36px;align-items:flex-start}
 .hero-value{font-family:var(--disp);font-size:44px;font-weight:700;line-height:1.05;margin-top:8px;
             transition:color .3s ease}
 /* The sum reads as one statement or it reads as noise -- 32ch broke it
    across four lines. Wraps only when the viewport genuinely cannot hold it. */
 .hero-sub{font-size:13px;color:var(--tx-dim);margin-top:8px;max-width:none;
   line-height:1.7}
 .hero-spark{width:100%;height:36px;margin-top:14px;display:block}
 /* The five facts that decide whether this run means anything, on one line.
    They were spread across three KPI groups, so answering "is it working?"
    meant assembling them by eye every time. */
 .hero-strip{display:flex;flex-wrap:wrap;gap:22px;margin-top:16px;
   padding-top:14px;border-top:1px solid var(--line-soft)}
 .hs{min-width:96px}
 .hs .hs-n{font-size:10px;letter-spacing:.08em;text-transform:uppercase;
   color:var(--tx-dim);font-weight:600}
 .hs .hs-v{font-family:var(--mono);font-size:17px;font-weight:600;margin-top:3px}
 .hs .hs-s{font-size:11px;color:var(--tx-dim);margin-top:1px}
 .hero-rail{background:var(--panel);padding:24px 28px;display:flex;flex-direction:column;gap:14px}
 .hero-rail-hdr{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--tx-dim);font-weight:600}
 .rank-row{display:grid;grid-template-columns:1fr 84px auto;align-items:center;gap:10px;font-size:12px}
 .rank-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--tx-dim)}
 .rank-track{height:8px;background:var(--line-soft);border-radius:4px;overflow:hidden}
 .rank-fill{height:100%;background:var(--up);border-radius:4px;transition:width .4s ease}
 .rank-val{font-family:var(--mono);font-weight:600;text-align:right;min-width:6ch}

 /* ---------- gauge strip ---------- */
 .gauge-strip{background:var(--panel);border-bottom:1px solid var(--line);padding:14px 28px;
              display:flex;align-items:center;gap:16px}
 .gauge-label{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--tx-dim);
              font-weight:600;min-width:15ch}
 .gauge-track{flex:1;height:14px;background:var(--line-soft);border-radius:7px;position:relative;overflow:hidden}
 .gauge-fill{height:100%;border-radius:7px;background:var(--up);transition:width .4s ease,background .4s ease}
 .gauge-cap{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--tx-faint)}
 .gauge-value{font-family:var(--mono);font-size:12px;color:var(--tx-dim);min-width:16ch;text-align:right}

 /* ---------- kpi groups ---------- */
 .kpi-wrapper{display:flex;flex-direction:column;gap:18px;padding:20px 24px;
              background:var(--bg);border-bottom:1px solid var(--line)}
 .kpi-hdr{color:var(--tx-faint);font-size:11px;padding:0 0 8px 2px;
          letter-spacing:.1em;font-weight:600;text-transform:uppercase}
 .kpi-group{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));
            gap:1px;background:var(--line);border:1px solid var(--line);border-radius:var(--r-md);overflow:hidden}
 .k{background:var(--panel);padding:14px 16px}
 .k.alert{box-shadow:inset 3px 0 0 var(--alert);background:rgba(255,92,92,.06)}
 .k .n{color:var(--tx-dim);font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;font-weight:600}
 .k .v{font-family:var(--mono);font-size:21px;font-weight:600;margin-top:5px}
 .k .s{color:var(--tx-faint);font-size:11.5px;margin-top:4px;line-height:1.35}

 .exp-err{padding:12px 24px;background:rgba(255,92,92,.1);color:var(--alert);
          border-bottom:1px solid var(--line);display:none;font-weight:500;font-size:13px}

 /* ---------- table ---------- */
 .wrap{overflow-x:auto}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th{text-align:left;color:var(--tx-faint);font-weight:600;font-size:10.5px;
    letter-spacing:.07em;padding:12px;border-bottom:1px solid var(--line);text-transform:uppercase;
    white-space:nowrap}
 td{padding:12px;border-bottom:1px solid var(--line-soft);vertical-align:middle}
 tr:hover td{background:var(--panel-2)}
 tr.alert td{background:rgba(255,92,92,.05)}
 .mkt-link{color:var(--tx);text-decoration:none;font-weight:500}
 .mkt-link:hover{color:var(--gold);text-decoration:underline}
</style></head><body>
<header class="mast">
  <div class="mast-id"><b>◆</b> Maker Fleet</div>
  <span class="tag">Paper · simulated fills</span>
  <span class="legend">
    <span><i style="background:var(--up)"></i>gain</span>
    <span><i style="background:var(--down)"></i>loss / risk</span>
    <span><i style="background:var(--gold)"></i>income</span>
    <span><i style="background:var(--proj)"></i>projected</span>
  </span>
  <span style="flex:1"></span>
  <span id="live" class="live"></span>
  <span id="health" class="live"></span>
  <span id="clock" class="clock"></span>
</header>
<section class="hero">
  <div class="hero-main">
    <div class="hero-duo">
      <div>
        <div class="hero-eyebrow">Liquidation P&L</div>
        <div class="hero-value" id="heroValue">$0.00</div>
      </div>
      <!-- The modelled side, deliberately the same size and deliberately a
           different colour. It is income the strategy claims to have earned,
           and it is NOT summed into the hard number to its left: for a
           spread-funded market that income arrives by being filled, so it is
           already inside `booked` and `pairs held`. Two figures, one hard and
           one modelled, is the honest presentation -- adding them would book
           the same dollars twice. -->
      <div>
        <div class="hero-eyebrow">Unrealized P&L <span class="dim">(Floating)</span></div>
        <div class="hero-value proj" id="heroIncome">$0.00</div>
      </div>
    </div>
    <div class="hero-sub" id="heroBridge">&nbsp;</div>
    <svg class="hero-spark" id="heroSpark" viewBox="0 0 300 36" preserveAspectRatio="none"></svg>
    <div class="hero-strip" id="heroStrip"></div>
  </div>
  <div class="hero-rail">
    <div class="hero-rail-hdr">Which market is carrying the fleet</div>
    <div id="rankRows"></div>
  </div>
</section>
<div class="gauge-strip">
  <div class="gauge-label">Wallet committed</div>
  <div class="gauge-track"><div class="gauge-fill" id="gaugeFill"></div><div class="gauge-cap" id="gaugeCap"></div></div>
  <div class="gauge-value" id="gaugeValue"></div>
</div>
<div class="kpi-wrapper" id="agg"></div>
<div class="exp-err" id="exp"></div>
<div class="wrap"><table id="tbl">
 <thead><tr>
  <th>Market</th>
  <th class="num">Projected / day</th>
  <th class="num">Committed</th>
  <th>Position / risk</th>
  <th class="num">Score share</th>
  <th class="num">Uptime</th>
  <th class="num">Fills</th>
  <th>Status</th>
 </tr></thead><tbody id="rows"></tbody></table></div>
<script>
const $=x=>document.getElementById(x);
const usd=(v,d=2)=>v==null?'-':'$'+Number(v).toFixed(d);
const pct=(v,d=1)=>v==null?'-':(100*v).toFixed(d)+'%';
const cls=v=>v==null||v===0?'dim':(v>0?'up':'down');
const thresh=(v,goodAbove,cut)=>v==null?'dim':((v>=cut)===goodAbove?'up':'down');
const hms=s=>{s=Math.max(0,Math.floor(s));
  const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;
  const p=n=>String(n).padStart(2,'0');
  return h?`${h}h ${p(m)}m ${p(x)}s`:`${m}m ${p(x)}s`;};

function ladder(m){
  const mid=m.mid_up, bid=m.up_bid, ask=m.up_ask;
  if(mid==null||bid==null||ask==null) return '<span class="dim">No two-sided book</span>';
  const v=(m.max_spread||4.5)/100;
  const half=Math.max(v*1.35, (ask-bid)*0.75, 0.01);
  const lo=mid-half, hi=mid+half, W=hi-lo;
  const x=p=>Math.max(0,Math.min(100,100*(p-lo)/W));
  const tag=(p,cls,lbl,top)=>p==null?'':
    `<span style="position:absolute;left:${x(p)}%;top:${top}px;transform:translateX(-50%)">
       <span class="${cls}" style="font-family:var(--mono);font-weight:700;font-size:10.5px;white-space:nowrap;text-shadow:0 1px 2px rgba(0,0,0,.9)">${lbl}</span></span>`;
  const mark=(p,color,top,h)=>p==null?'':
    `<span style="position:absolute;left:${x(p)}%;top:${top}px;width:2px;height:${h}px;
       background:${color};transform:translateX(-50%)"></span>`;
  const wl=x(mid-v), wr=x(mid+v);
  return `<div style="position:relative;height:38px;width:100%;max-width:280px">
    <div style="position:absolute;left:${wl}%;width:${wr-wl}%;top:12px;height:12px;
         background:var(--up-soft);border-left:1px solid #24463f;border-right:1px solid #24463f"></div>
    <div style="position:absolute;left:0;right:0;top:17px;height:1px;background:var(--line)"></div>
    ${mark(mid,'#4a5568',10,16)}
    ${mark(bid,'var(--tx-faint)',13,10)}${mark(ask,'var(--tx-faint)',13,10)}
    ${mark(m.our_up,'var(--proj)',8,20)}
    ${mark(m.our_dn_as_up,'var(--down)',8,20)}
    ${tag(m.our_up,'proj',(m.our_up!=null?m.our_up.toFixed(3):''),27)}
    ${tag(m.our_dn_as_up,'down',(m.our_dn_as_up!=null?m.our_dn_as_up.toFixed(3):''),27)}
    ${tag(mid,'dim','mid',-2)}
  </div>`;
}

function capBar(m){
  let up=0, dn=0;
  for(const o of (m.quotes||[])){
    const notional=(o.price||0)*(o.size||0);
    if(o.side==='UP') up+=notional; else dn+=notional;
  }
  const total=up+dn;
  if(total<=0) return '<div class="dim" style="font-size:11px;margin-top:6px">No capital resting</div>';
  const upPct=100*up/total, dnPct=100*dn/total;
  return `<div style="display:flex;height:16px;width:100%;max-width:280px;margin-top:6px;
       background:var(--line-soft);border-radius:4px;overflow:hidden;font-size:11px;font-weight:600;letter-spacing:0.02em">
    <div style="width:${dnPct}%;background:var(--down-soft);display:flex;align-items:center;padding-left:6px;color:var(--down);white-space:nowrap">${usd(dn,0)} NO</div>
    <div style="width:${upPct}%;background:var(--proj-soft);display:flex;align-items:center;justify-content:flex-end;padding-right:6px;color:var(--proj);white-space:nowrap">${usd(up,0)} YES</div>
  </div>`;
}

function posBar(m) {
  if (!(m.paired>0) && !(m.naked_sh>0)) {
    return m.closes>0 ? `<span class="dim">Closed ${m.closes}x early</span>` : '<span class="dim">-</span>';
  }
  const p = m.paired || 0;
  const n = m.naked_sh || 0;
  const tot = p + n;
  const pPct = 100 * p / tot;
  const nPct = 100 * n / tot;

  return `<div style="width:160px">
    <div style="display:flex;justify-content:space-between;font-size:12px;font-weight:600;margin-bottom:6px">
      <span class="proj">${p?p.toFixed(0)+' pairs':''}</span>
      <span class="down">${n?n.toFixed(0)+' '+m.naked_side:''}</span>
    </div>
    <div style="display:flex;height:8px;background:var(--line-soft);border-radius:4px;overflow:hidden">
      ${p ? `<div style="width:${pPct}%;background:var(--proj)"></div>` : ''}
      ${n ? `<div style="width:${nPct}%;background:var(--down)"></div>` : ''}
    </div>
  </div>`;
}

function finBox(m) {
  if (!(m.paired>0) && !(m.naked_sh>0) && !(m.closes>0)) return '<span class="dim">-</span>';
  let h = `<div style="display:grid;grid-template-columns:auto 1fr;gap:6px 16px;align-items:center;font-family:var(--mono);font-size:12px">`;

  if (m.paired > 0) {
    const locked = m.paired * 1.0 - m.pair_paid;
    h += `<span class="dim" style="font-family:var(--body)">Locked</span>
          <span class="${locked>=0?'up':'down'} bold">${locked>=0?'+':''}${usd(locked)}</span>`;
  }
  if (m.naked_sh > 0) {
    h += `<span class="dim" style="font-family:var(--body)">Risk</span>
          <span class="down bold">-${usd(m.naked_cost)}</span>`;
  }
  if (m.closes > 0) {
    h += `<span class="dim" style="font-family:var(--body)">Closed</span>
          <span class="${m.closed_pnl>=0?'up':'down'} bold">${m.closed_pnl>=0?'+':''}${usd(m.closed_pnl)}</span>`;
  }
  h += `</div>`;

  if (m.close_why) {
    h += `<div class="dim" style="font-size:11px;margin-top:8px;line-height:1.3;">${m.close_why}</div>`;
  }
  return h;
}

function sparkline(points){
  if(!points || points.length<2) return '';
  const w=300,ht=36,pad=2;
  const lo=Math.min(...points), hi=Math.max(...points), span=(hi-lo)||1;
  const step=(w-2*pad)/(points.length-1);
  const xy=(v,i)=>[pad+i*step, ht-pad-((v-lo)/span)*(ht-2*pad)];
  const d=points.map((v,i)=>{const [x,y]=xy(v,i); return `${i===0?'M':'L'}${x.toFixed(1)},${y.toFixed(1)}`;}).join(' ');
  const [lx,ly]=xy(points[points.length-1],points.length-1);
  return `<path d="${d}" fill="none" stroke="var(--proj)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
          <circle cx="${lx}" cy="${ly}" r="2.5" fill="var(--proj)"/>`;
}

async function tick(){
  let s; try{ s=await (await fetch('/api/fleet',{cache:'no-store'})).json(); }
  catch(e){ return; }
  $('clock').textContent = s.run_started
    ? 'T+ ' + hms(s.now - s.run_started)
    : 'Not started';

  if(s.error){
    $('exp').textContent = s.error;
    $('exp').style.display = 'block';
    $('health').innerHTML = '<span class="alert-tx">● OFFLINE</span>';
    return;
  } else {
    $('exp').style.display = 'none';
  }

  const t=s.totals;
  const activeCount = s.markets.filter(m => (m.income || 0) > 0).length;
  const healthy = !t.fleet_stale;
  $('live').innerHTML = `<span class="${activeCount > 0 ? 'up' : 'down'}">● ${activeCount}/${t.markets} scoring</span>`;
  $('health').innerHTML = healthy
    ? '<span class="up">● LIVE</span>'
    : `<span class="alert-tx">● STALE · ${hms(t.state_age)}</span>`;
  if(!healthy){
    $('exp').textContent = `Fleet heartbeat is stale (${hms(t.state_age)} old). Displayed figures are historical, not live.`;
    $('exp').style.display = 'block';
  }

  // ---- hero: the one number, its trend, and who's carrying it ----
  const hv=$('heroValue');
  hv.textContent = usd(t.liquidate_now_pnl);
  hv.className = 'hero-value ' + cls(t.liquidate_now_pnl);
  // FLOATING P&L IS A POSITION VALUE, NOT A MODEL. Unrealized P&L means what
  // the open book is worth against what it cost -- inventory float plus
  // unhedged float -- and that is exactly the middle of the liquidation
  // equation below. The modelled accrual that used to sit here is a
  // projection integrated over time; giving it a brokerage name for a live
  // position would put a forecast where a mark-to-market belongs. It keeps
  // its place in the income-rate tile, labelled as the benchmark it is.
  const floating = (t.locked_pair || 0) + (t.naked_exit || 0) - (t.at_risk || 0);
  const hi = $('heroIncome');
  hi.textContent = usd(floating);
  hi.className = 'hero-value ' + cls(floating);
  // THE ARITHMETIC, SPELLED OUT. The hero used to carry a prose description
  // of a formula while four tiles showed pieces of it under names that did
  // not match -- so $40 realized sitting above an $18.89 headline read as a
  // contradiction. Every term below is signed and they sum to the headline.
  const term=(label,v)=>`<span class="${cls(v)}">${v>=0?'+':'−'}${usd(Math.abs(v))}</span> ${label}`;
  // TWO PROGRAMS PAY A MAKER, AND BOTH BELONG IN THIS TERM.
  //
  // Reward rent is money the venue owes for RESTING size. Maker rebates are a
  // share of the taker fee on volume we MADE. They are disjoint products, so
  // they add; and neither is inside `booked`, because a rebate arrives on top
  // of the fill rather than through its price.
  //
  // Spread "rent" is still NOT added: it projects income that arrives BY being
  // filled, and a fill is already in `booked` and `pairs held`. That is the
  // one line here that would double-count, which is why the split exists.
  const rent = (t.rent_reward || 0) + (t.maker_rebate || 0);
  // Naked cost and resale are one term now -- "Unhedged Float", the mark on
  // the unpaired leg -- because a reader tracking a brokerage statement wants
  // realized, inventory float and unhedged float, not the venue mechanics
  // underneath each.
  $('heroBridge').innerHTML =
    term('Realized', t.realized) + ' &nbsp;|&nbsp; ' +
    // Shown unconditionally now: a rebate line reading +$0.00 states that
    // nothing held pays a rebate, which is itself the fact worth knowing on a
    // fleet whose entire universe publishes clobRewards: 0. Hiding it left the
    // reader unable to tell "no rebate" from "rebates not counted".
    term('Earned Rebates', rent) + ' &nbsp;|&nbsp; ' +
    term('Paired Unrealized', t.locked_pair) + ' &nbsp;|&nbsp; ' +
    term('Unhedged Unrealized', t.naked_exit - t.at_risk) +
    ` &nbsp;=&nbsp; <b>${usd(t.liquidate_now_pnl + rent)}</b> Total Liquidation P&L`;
  $('heroSpark').innerHTML = sparkline(s.share_history);

  // THE STORY, IN FIVE FACTS. Ordered as the questions actually get asked:
  // has it run long enough, is it trading, is being filled profitable, is the
  // model believable, and has anything settled to prove it.
  const edgePerFill = t.markout_n ? t.fill_edge / t.markout_n : null;
  const HS=(n,v,sub,cl)=>`<div class="hs"><div class="hs-n">${n}</div>
    <div class="hs-v ${cl||''}">${v}</div><div class="hs-s">${sub||''}</div></div>`;
  $('heroStrip').innerHTML =
    // Run age lives in the masthead clock beside the LIVE dot; repeating it
    // here spent a strip slot on a number already on screen.
    HS('Fills', String(t.fills),
       (t.verified && t.verified.ratio !== null)
         ? pct(t.verified.ratio)+' tape-verified' : 'no tape yet',
       t.fills ? 'up' : 'dim') +
    // THE VERDICT METRIC. Everything else is a projection; this is what being
    // filled actually paid, per fill, measured from the markouts table.
    HS('Edge / fill', edgePerFill === null ? '—' : usd(edgePerFill),
       t.markout_n ? t.markout_n+' matured'+(t.markout_n<20?' · need 20':'') : 'awaiting horizon',
       edgePerFill === null ? 'dim' : cls(edgePerFill)) +
    // The projection standing next to the measurement, deliberately adjacent:
    // if the model is right these converge, and if it is optimistic the gap
    // is the story.
    // TIME-WEIGHTED, not instantaneous. The live figure moved $302 -> $41
    // inside an hour as markets were funded and defunded; whichever moment
    // you looked at became "the" number. This one credits each level for the
    // time it was actually held, so a rate held ten minutes counts a sixth as
    // much as one held an hour. The spot reading stays in the sub-line,
    // because the gap between them is itself information.
    HS('Avg income rate',
       t.income_twa_day === null ? '—' : usd(t.income_twa_day)+'/d',
       t.income_twa_day === null
         ? 'need 2 samples'
         : 'avg over '+t.income_hours.toFixed(1)+'h · now '+usd(t.income_day),
       'proj') +
    HS('Settled', String(t.settled),
       t.settled ? usd(t.realized)+' booked' : 'no ground truth yet',
       t.settled ? 'up' : 'dim');

  const top = [...s.markets].sort((a,b)=>b.income-a.income).slice(0,6);
  const maxInc = Math.max(1e-9, ...top.map(m=>m.income||0));
  $('rankRows').innerHTML = top.map(m=>`
    <div class="rank-row">
      <span class="rank-name" title="${m.title}">${m.title}</span>
      <div class="rank-track"><div class="rank-fill" style="width:${Math.max(2,100*(m.income||0)/maxInc)}%"></div></div>
      <span class="rank-val mono ${m.income>0?'up':'dim'}">${usd(m.income)}</span>
    </div>`).join('') || '<span class="dim" style="font-size:12px">No markets reporting yet</span>';

  // ---- exposure gauge ----
  const budgetPct = t.wallet > 0 ? Math.min(100, 100*t.committed_total/t.wallet) : 0;
  const capPct = t.wallet > 0 ? Math.min(100, 100*t.max_committed/t.wallet) : 100;
  const budgetAlert = t.committed_total >= t.max_committed;
  const gf=$('gaugeFill');
  gf.style.width = budgetPct+'%';
  gf.style.background = budgetAlert ? 'var(--alert)' : (budgetPct>70?'var(--down)':'var(--up)');
  $('gaugeCap').style.left = capPct+'%';
  $('gaugeValue').innerHTML = `<span class="${budgetAlert?'alert-tx bold':'dim'}">${usd(t.committed_total,0)} / ${usd(t.wallet,0)}</span>`+
    (budgetAlert ? ` <span class="alert-tx">· ${usd(t.committed_overage,0)} over cap</span>` : ` · ${usd(t.available_cash,0)} available`);

  const K=(n,v,sub,cl,isAlert)=>`<div class="k ${isAlert?'alert':''}"><div class="n">${n}</div>
    <div class="v ${cl||''}">${v}</div><div class="s">${sub||''}</div></div>`;
  const renderGroup = (title, tiles) => `<div><div class="kpi-hdr">${title}</div><div class="kpi-group">${tiles.join('')}</div></div>`;

  const t_pl = [
    K('Projected Daily Return',usd(t.income_day)+'/day',
      (t.markets_spread ? usd(t.income_spread)+' spread · '+usd(t.income_reward)+' rewards'
                        : 'modelled at current score share'),'proj'),
    // The breakdown is named for what each half MEANS, not for the mechanic
    // that produced it: one is edge the bot took on purpose and repeats, the
    // other is the market happening to move afterwards and averages toward
    // zero. "captured / drift" required already knowing that distinction.
    K('Total Trade Alpha',usd(t.fill_edge),
      t.markout_n ? usd(t.markout_spread)+' Spread Capture + '+usd(t.markout_total)+' Capital Gain · '+t.markout_n+' fills'
                  : 'no matured fill yet',
      t.markout_n ? cls(t.fill_edge) : 'dim'),
    K('Realized P&L',usd(t.realized),
      (t.closes?t.closes+' closed trade'+(t.closes===1?'':'s'):'no closed trades')
        +' · '+(t.settled?t.settled+' settled':'$0.00 settled'),
      (t.settled||t.closes)?cls(t.realized):'dim'),
    // Deliberately fenced off. This is a MODEL of income earned so far, it is
    // not in the headline and never was, and reading it as money is what made
    // the P&L look like it did not add up.
    // Income accrued is a hero figure now; carrying it here too made the same
    // modelled number appear twice on one screen.
    // A RANGE, NOT A POINT. The model projected $159.80 of spread income
    // while the bot measurably captured $17.76 -- roughly 9x apart -- and
    // showing only the model made the run look far better than it was, while
    // showing only the measurement would ignore what the strategy is aiming
    // at. Floor is what actually happened, ceiling is what the model claims,
    // and the width between them is the honesty of `spread_capture_frac`.
    //
    // Both ends are ALREADY inside Closed P&L and Fill Edge -- this is a
    // benchmark to judge those by, never an amount to add to them.
    K('Expected vs Realized Yield',
      (t.markout_n
        ? usd(t.markout_spread)+' – '+usd(t.rent_modelled_spread + t.rent_reward)
        : usd(t.rent_modelled_spread + t.rent_reward)),
      (t.markout_n
        ? 'Baseline: Pure Edge | Ceiling: Model Target · already booked on execution'
        : 'Ceiling: Model Target · no matured fills yet'),
      'proj'),
    // The income the venue owes but has not paid: reward emissions on resting
    // size plus maker rebates on matched volume. Stays a separate line and
    // stays in the headline equation. The subtext names WHICH program is
    // paying, because a fleet holding only clobRewards: 0 markets earns the
    // rebate and nothing else -- and a single blended figure left the reader
    // unable to tell that apart from "the pot is funded".
    K('Dividend / Rebate Income',usd(rent),
      rent > 0
        ? [t.rent_reward > 0 ? usd(t.rent_reward)+' emissions' : null,
           t.maker_rebate > 0
             ? usd(t.maker_rebate)+' rebates on '+(t.maker_rebate_shares||0).toFixed(0)+' filled sh'
             : null].filter(Boolean).join(' · ')+' · unpaid, in the headline'
        : 'no emissions funded · no maker fills yet',
      rent > 0 ? 'gold' : 'dim'),
    K('Capital return',t.return_pct_day.toFixed(2)+'%/day','projected income / wallet committed','proj')
  ];

  const t_risk = [
    K('Capital Deployed',usd(t.committed_total,0),`of ${usd(t.wallet,0)} simulated wallet`,t.committed_total>=t.max_committed?'alert-tx':'dim',t.committed_total>=t.max_committed),
    // MARKET VALUE, not cost. Stated as value, the drawdown below is exactly
    // this figure subtracted from liquidation P&L -- the two tiles reconcile
    // by inspection. Stated as cost it does not: liquidation already nets
    // cost against resale, so subtracting cost a second time double-counts it
    // (-$197.89 against a true -$221.61 on the numbers of 2026-08-02).
    K('Unhedged Exposure',usd(t.naked_exit,0),'unhedged positions market value','down'),
    K('Worst-Case P&L',usd(t.net_worst),'P&L if value goes to $0 on all current unhedged bets',cls(t.net_worst)),
    // NOT the size of the holding -- the profit or loss on it. The old label
    // said "matched shares valued at $1" over a number that is the difference
    // between that value and what we paid, so a -$13.59 loss read as though
    // the inventory itself were negative. Both figures are now shown.
    K('Open Positions Unrealized P&L',usd(t.locked_pair),
      usd(t.pair_value,0)+' at $1 · paid '+usd(t.pair_paid,0),cls(t.locked_pair)),
    K('Matured Position Horizon',String(t.markout_n),'fills with a matured mark',t.markout_n>=20?'up':'dim'),
    K('Markets exited',String(t.exited),'after adverse-selection evidence',t.exited?'down':'up',t.exited>0)
  ];

  const t_cap = [
    // Unallocated cash only -- deliberately NOT buying power, which would
    // also count collateral released by cancelling open orders.
    K('Net Available Cash',usd(t.available_cash,0),'unallocated of '+usd(t.wallet,0)+' wallet','up'),
    K('Open Orders Collateral',usd(t.capital,0),'cash held against resting bids','gold'),
    K('Active Traded Bets',t.scoring+' / '+t.markets,
      t.markets_spread ? t.markets_spread+' priced on spread capture' : 'positive projected income',
      t.scoring?'up':'dim'),
    K('Scoring uptime',pct(t.uptime),'checks with non-zero score',thresh(t.uptime,true,0.8)),
    K('Fills',String(t.fills),'tape-confirmed + crossed','dim'),
    K('Max Allowed Unrealized Loss',usd(t.fleet_naked_budget,0),'hard unhedged-loss ceiling','dim'),
    K('Data health',healthy?'LIVE':'STALE',healthy?'heartbeat < 120s':'do not trust live figures',healthy?'up':'alert-tx',!healthy)
  ];

  $('agg').innerHTML = renderGroup('Profit &amp; loss', t_pl) + renderGroup('Risk &amp; exposure', t_risk) + renderGroup('Capital &amp; operations', t_cap);

  $('rows').innerHTML=s.markets.map(m=>{
    const currentIncome = (m.income || 0);
    const isGenerating = currentIncome > 0;
    const risk = m.naked_sh || 0;
    const position = risk > 0
      ? `<span class="down bold">${risk.toFixed(0)} ${m.naked_side || 'naked'}</span><br><span class="dim">risk ${usd(m.naked_cost,0)}</span>`
      : (m.paired > 0 ? `<span class="up bold">${m.paired.toFixed(0)} paired</span>` : '<span class="dim">flat</span>');
    let statusHtml = m.err
      ? `<span class="down bold">${m.err}</span>`
      : (m.gate === 'EXITED' ? '<span class="down bold">EXITED</span>'
      : (isGenerating
          ? `<span class="up bold">${m.source === 'spread' ? 'EARNING SPREAD' : 'SCORING'}</span>`
          : `<span class="dim">${m.why || 'not earning'}</span>`));
    return `<tr class="${m.gate === 'EXITED' ? 'alert' : ''}">
      <td style="max-width:300px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${m.title}">${m.url?`<a class="mkt-link" href="${m.url}" target="_blank">${m.title}</a>`:m.title}</td>
      <td class="num bold mono ${isGenerating ? 'up' : 'dim'}" style="font-size:15px">${usd(currentIncome)}</td>
      <td class="num mono" title="Offers ${usd(m.capital,0)}">${usd(m.committed,0)}</td>
      <td>${position}</td>
      <td class="num mono">${pct(m.share,1)}</td>
      <td class="num mono ${thresh(m.uptime,true,0.8)}">${pct(m.uptime,0)}</td>
      <td class="num mono dim">${m.fills}</td>
      <td>${statusHtml}</td>
    </tr>`;
  }).join('');
}
tick(); setInterval(tick,4000);
</script>
</body></html>
"""
@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE