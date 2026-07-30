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

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "run"
DB = RUN / "fleet.db"

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

    rows = []
    for s in specs:
        live = s.get("_live") or {}
        h = hist.get(s["cid"], {})
        rows.append({
            "title": s["title"], "slug": s.get("slug", ""),
            "url": f"https://polymarket.com/market/{s['slug']}" if s.get("slug") else "",
            "daily": s["daily"], "min_size": s["min_size"],
            "max_spread": s["max_spread"],
            "share": live.get("share", 0.0),
            "income": live.get("income", 0.0),
            "capital": live.get("capital", 0.0),
            "quotes": live.get("quotes", []),
            "fills": h.get("fills", 0),
            "uptime": h.get("uptime", 0.0),
            "samples": h.get("samples", 0),
            # Estimate, not a ledger entry: no dollar amount is persisted per
            # sample (reward_samples only stores our_share), so this assumes
            # today's funded daily rate held constant over the whole window --
            # same assumption the live "income" projection already makes.
            "collected_rent": h.get("avg_share", 0.0) * (s["daily"] or 0.0)
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
    unfunded = [r for r in rows if not (r["daily"] or 0) > 0]

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
            "unfunded": len(unfunded),
            "realized": rz["realized"],
            "settled": rz["settled"],
            "wins": rz["wins"],
            "losses": rz["losses"],
            "closes": rz["closes"],
            "closed_pnl": rz["closed_pnl"],
            "closed_forgone": rz["closed_forgone"],
            "locked_pair": locked,
            "at_risk": at_risk,
            "net_worst": rz["realized"] + locked - at_risk,
            # Liquidate everything right now: booked P&L, plus pairs merged
            # for $1 each (always available, no need to wait for
            # resolution), plus naked shares sold at today's best bid minus
            # what they cost. Distinct from net_worst, which prices naked
            # shares at $0 -- the resolution floor, not a sellable price.
            "liquidate_now_pnl": rz["realized"] + locked
                                 + (naked_exit_total - at_risk),
            "markout_total": mk["total"],
            "markout_spread": mk["spread"],
            "markout_n": mk["n"],
            "fleet_naked_budget": 800.0,
            "exited": len([r for r in rows if r["gate"] == "EXITED"]),
            "widened": len([r for r in rows if r["gate"] == "WIDENED"]),
            "capital": cap,
            # NOTE: `cap` counts money resting in UNFILLED offers only, so this
            # answers "what do my resting offers earn", not "what does my
            # bankroll earn". Measured 2026-07-30: 1.80%/day on $1,369 of
            # offers while $9,588 had actually left the wallet -- 0.256%/day
            # against the real denominator. U3 bounds that total; until it
            # does, read this ratio for what it is.
            "return_pct_day": (100 * inc / cap) if cap else 0.0,
            # U2. Capital that went back to work instead of sitting until 2027.
            "merged_shares": merged_total,
            "recycled_usd": sum(r["recycled_usd"] for r in rows),
            # Fleet-wide pairing rate: merged pairs over shares actually
            # filled. Deliberately NOT over `fills`, which is a count of fill
            # events -- dividing shares by events would produce a ratio with no
            # meaning that still looks plausible. None until something fills.
            "pairing_rate": ((merged_total / vr["verified_shares"])
                             if vr["verified_shares"] > 1e-9 else None),
            # U1. The Phase A decision-gate number, on the dashboard because a
            # figure that lives only in the database is a figure nobody reads.
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
 .hero{display:grid;grid-template-columns:minmax(240px,340px) 1fr;gap:1px;
       background:var(--line);border-bottom:1px solid var(--line)}
 .hero-main{background:var(--panel);padding:24px 28px}
 .hero-eyebrow{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--tx-dim);font-weight:600}
 .hero-value{font-family:var(--disp);font-size:44px;font-weight:700;line-height:1.05;margin-top:8px;
             transition:color .3s ease}
 .hero-sub{font-size:12px;color:var(--tx-dim);margin-top:6px;max-width:30ch}
 .hero-spark{width:100%;height:36px;margin-top:14px;display:block}
 .hero-rail{background:var(--panel);padding:24px 28px;display:flex;flex-direction:column;gap:14px}
 .hero-rail-hdr{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--tx-dim);font-weight:600}
 .rank-row{display:grid;grid-template-columns:15ch 1fr auto;align-items:center;gap:10px;font-size:12px}
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
  <span id="clock" class="clock"></span>
</header>
<section class="hero">
  <div class="hero-main">
    <div class="hero-eyebrow">If liquidated right now</div>
    <div class="hero-value" id="heroValue">$0.00</div>
    <div class="hero-sub">Realized, plus pairs merged for $1, plus naked shares sold at today's best bid</div>
    <svg class="hero-spark" id="heroSpark" viewBox="0 0 300 36" preserveAspectRatio="none"></svg>
  </div>
  <div class="hero-rail">
    <div class="hero-rail-hdr">Which market is carrying the fleet</div>
    <div id="rankRows"></div>
  </div>
</section>
<div class="gauge-strip">
  <div class="gauge-label">Exposure budget</div>
  <div class="gauge-track"><div class="gauge-fill" id="gaugeFill"></div><div class="gauge-cap" id="gaugeCap"></div></div>
  <div class="gauge-value" id="gaugeValue"></div>
</div>
<div class="kpi-wrapper" id="agg"></div>
<div class="exp-err" id="exp"></div>
<div class="wrap"><table id="tbl">
 <thead><tr>
  <th>Market</th>
  <th class="num">Our $/day</th>
  <th class="num">Rent collected</th>
  <th>Inventory</th>
  <th>Financials</th>
  <th style="width:300px">Where our offers sit</th>
  <th class="num">Capital</th>
  <th class="num">Our slice</th>
  <th class="num">Funds $/day</th>
  <th class="num">Qualifying</th>
  <th class="num">Fills</th>
  <th class="num">Cost per fill</th>
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
    return;
  } else {
    $('exp').style.display = 'none';
  }

  const t=s.totals;
  const activeCount = s.markets.filter(m => (m.income || 0) > 0).length;
  $('live').innerHTML = `<span class="${activeCount > 0 ? 'up' : 'down'}">● ${activeCount}/${t.markets} markets generating rent</span>`;

  // ---- hero: the one number, its trend, and who's carrying it ----
  const hv=$('heroValue');
  hv.textContent = usd(t.liquidate_now_pnl);
  hv.className = 'hero-value ' + cls(t.liquidate_now_pnl);
  $('heroSpark').innerHTML = sparkline(s.share_history);

  const top = [...s.markets].sort((a,b)=>b.income-a.income).slice(0,6);
  const maxInc = Math.max(1e-9, ...top.map(m=>m.income||0));
  $('rankRows').innerHTML = top.map(m=>`
    <div class="rank-row">
      <span class="rank-name" title="${m.title}">${m.title}</span>
      <div class="rank-track"><div class="rank-fill" style="width:${Math.max(2,100*(m.income||0)/maxInc)}%"></div></div>
      <span class="rank-val mono ${m.income>0?'up':'dim'}">${usd(m.income)}</span>
    </div>`).join('') || '<span class="dim" style="font-size:12px">No markets reporting yet</span>';

  // ---- exposure gauge ----
  const budgetPct = Math.min(100, 100*t.at_risk/t.fleet_naked_budget);
  const budgetAlert = t.at_risk >= t.fleet_naked_budget;
  const gf=$('gaugeFill');
  gf.style.width = budgetPct+'%';
  gf.style.background = budgetAlert ? 'var(--alert)' : (budgetPct>70?'var(--down)':'var(--up)');
  $('gaugeCap').style.left = '100%';
  $('gaugeValue').innerHTML = `<span class="${budgetAlert?'alert-tx bold':'dim'}">${usd(t.at_risk,0)} / ${usd(t.fleet_naked_budget,0)}</span>`+
    (budgetAlert ? ' <span class="alert-tx">· over budget, flattening only</span>' : '');

  const K=(n,v,sub,cl,isAlert)=>`<div class="k ${isAlert?'alert':''}"><div class="n">${n}</div>
    <div class="v ${cl||''}">${v}</div><div class="s">${sub||''}</div></div>`;
  const renderGroup = (title, tiles) => `<div><div class="kpi-hdr">${title}</div><div class="kpi-group">${tiles.join('')}</div></div>`;

  const t_pl = [
    K('Realized P&L',usd(t.realized), (t.settled?t.settled+' settled · '+t.wins+'W/'+t.losses+'L':'nothing settled yet')+(t.closes?' · incl. '+t.closes+' early close'+(t.closes===1?'':'s'):''), (t.settled||t.closes)?cls(t.realized):'dim'),
    K('Rent collected',usd(t.collected_rent_total), 'total historical rent earned across fleet','gold'),
    K('Early closes (sim)',usd(t.closed_pnl), t.closes?t.closes+' closed · forgave '+usd(t.closed_forgone)+' vs holding to settlement':'none yet', t.closes?cls(t.closed_pnl):'dim'),
    K('Rent projected',usd(t.income_day)+'/day', usd(t.income_hour)+'/hr forecast at current share','proj'),
    K('Return projected',t.return_pct_day.toFixed(2)+'%/day', 'rent forecast over capital committed','proj')
  ];

  const t_risk = [
    K('At risk (naked)',usd(t.at_risk,0), activeCount > 0 ?'unpaired shares, pay $1 or $0':'stale reporting', activeCount > 0 ?(t.at_risk>0?'down':'up'):'dim'),
    K('Net if all naked lose',usd(t.net_worst),'worst case on today’s book', cls(t.net_worst)),
    K('Locked (paired)',usd(t.locked_pair), activeCount > 0 ?'pairs always pay $1 - won':'stale reporting', activeCount > 0 ?(t.locked_pair>0?'up':'dim'):'dim'),
    K('Markets backed off', t.widened + ' / ' + t.exited, 'widened / exited on bad fills', (t.exited > 0 ? 'down' : (t.widened > 0 ? 'down' : 'up')), t.exited>0),
    K('Spread captured', t.markout_n >= 20 ? usd(t.markout_spread) : 'measuring…', '2c entry discount', t.markout_n >= 20 ? 'gold' : 'dim'),
    K('Drift after fill', t.markout_n >= 20 ? usd(t.markout_total) : 'measuring…', t.markout_n + ' fills priced - negative = picked off', t.markout_n >= 20 ? cls(t.markout_total) : 'dim')
  ];

  const t_cap = [
    K('Capital used',usd(t.capital,0),'money tied up in offers','dim'),
    K('Markets earning',t.scoring+' / '+t.markets, t.unfunded?t.unfunded+' unfunded (rent $0)':'0 = quoting but not scoring', t.unfunded?'down':(t.scoring===t.markets?'up':'dim')),
    K('Qualifying time',pct(t.uptime),'% of checks earning rent', thresh(t.uptime,true,0.8)),
    K('Fills',String(t.fills),'offers taken - watch for losses','dim'),
    K('Pool available',usd(t.funded_total,0)+'/day','total funded by venue','dim'),
    K('Concentration',pct(t.concentration),'share from best market', thresh(t.concentration,false,0.5))
  ];

  $('agg').innerHTML = renderGroup('Profit &amp; loss', t_pl) + renderGroup('Risk &amp; exposure', t_risk) + renderGroup('Capital &amp; operations', t_cap);

  $('rows').innerHTML=s.markets.map(m=>{
    const q=ladder(m)+capBar(m);

    const currentIncome = (m.income || 0);
    const isGenerating = currentIncome > 0;

    let statusHtml = '';
    if (m.err) {
      statusHtml = `<span class="down bold">${m.err}</span>`;
    } else if (isGenerating) {
      statusHtml = `<span class="up bold">Active</span>`;
    } else {
      let reason = m.why || 'Not scoring';
      if (m.daily === 0 || m.daily == null) {
        reason = 'Market unfunded ($0/day)';
      } else if (m.uptime < 0.1) {
        reason = 'Not on the board (0% uptime)';
      }
      statusHtml = `<span class="down" style="font-size:12px;" title="${reason}">${reason}</span>`;
    }

    const inv = posBar(m);
    const fin = finBox(m);
    const rowAlert = m.gate === 'EXITED';

    return `<tr class="${rowAlert ? 'alert' : ''}">
      <td style="max-width:260px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${m.title}">${m.url?`<a class="mkt-link" href="${m.url}" target="_blank">${m.title}</a>`:m.title}</td>
      <td class="num bold mono ${isGenerating ? 'up' : 'dim'}" style="font-size:15px">${usd(currentIncome)}</td>
      <td class="num bold mono gold" style="font-size:14px">${usd(m.collected_rent)}</td>
      <td>${inv}</td>
      <td>${fin}</td>
      <td class="dim">${q}</td>
      <td class="num mono">${usd(m.capital,0)}</td>
      <td class="num mono dim">${pct(m.share,2)}</td>
      <td class="num mono">${usd(m.daily,0)}</td>
      <td class="num mono ${thresh(m.uptime,true,0.8)}">${pct(m.uptime,0)}</td>
      <td class="num mono dim">${m.fills}</td>
      <td class="num mono ${m.markout==null?'dim':(m.markout<0?'down':'up')}">${
        m.markout==null?'-':(m.markout>=0?'+':'')+(100*m.markout).toFixed(2)+'¢'
      }<span class="dim" style="font-size:11px"> · ${m.markout_n||0}</span></td>
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