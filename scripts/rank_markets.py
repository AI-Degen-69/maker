"""Rank funded reward markets by RETURN, and write the winners to run/markets.json.

Headline daily rate is the wrong sort key. A $300/day market with thousands of
shares already resting inside the reward window pays less than a $50/day market
with a thin book, because the pool splits by score share:

    income = daily_rate * ours / (ours + theirs)

So the metric is income per dollar of capital committed, computed against the
live book. Measured across 211 funded markets, the spread between best and
worst on that metric is roughly 7x at identical risk.

    python -m scripts.rank_markets            # top 20
    python -m scripts.rank_markets --top 40
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategy.config import load as _load_cfg   # noqa: E402

RUN = ROOT / "run"
OFFSET = 0.020          # where we intend to quote, in price units
C = 3.0                 # venue's one-sided penalty

# Polymarket: "The minimum reward payout is $1; amounts below this will not be
# paid." A market projecting under a dollar a day does not pay a fraction of a
# dollar, it pays nothing -- so a sub-floor market is not a small position, it
# is capital committed for zero income. Measured 2026-07-30, 16 of 20 fleet
# markets were in exactly that state.
MIN_PAYOUT = 1.0
FLOOR_MULTIPLE = 1.5    # headroom: projections are noisy and rivals arrive

# TRADABILITY AND HORIZON (U6). Sourced from config so the ranker and the
# fleet cannot drift, exactly as the payout floor is.
_CFG = _load_cfg()
MIN_VOLUME_24H = _CFG.select_min_volume_24h_usd
MAX_DAYS_TO_RESOLVE = _CFG.select_max_days_to_resolve

GAMMA = "https://gamma-api.polymarket.com/markets"


def q_min(a: float, b: float) -> float:
    return max(min(a, b), max(a / C, b / C))


def days_to_resolve(end_iso: Optional[str],
                    now_iso: Optional[str] = None) -> Optional[float]:
    """Days from now until the venue's stated end date, or None if unstated.

    `now_iso` exists so the horizon arithmetic is testable without freezing
    the clock. Negative means the end date has already passed.
    """
    if not end_iso:
        return None
    try:
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    now = (datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
           if now_iso else datetime.now(timezone.utc))
    return (end - now).total_seconds() / 86400.0


def tradable(volume_24h: Optional[float],
             days: Optional[float]) -> tuple[bool, str]:
    """Can this market produce the two observations the run needs?

    A fill needs someone to trade at our price; a settled P&L needs the market
    to resolve inside the run. Reward yield answers neither question, and
    ranking on it alone chose 20 markets that between them printed 48 trades in
    11.6 hours and resolved no sooner than September 2026.

    Unknown is refused on both axes rather than assumed favourable. The
    universe that produced zero fills and zero resolutions was long-dated and
    thin, so a missing field is far more likely to be another one of those than
    a liquid market with a gap in its metadata.
    """
    if volume_24h is None:
        return False, "volume unknown"
    if volume_24h < MIN_VOLUME_24H:
        return False, f"24h volume ${volume_24h:,.0f} < ${MIN_VOLUME_24H:,.0f}"
    if days is None:
        return False, "horizon unknown"
    if days < 0:
        return False, "horizon passed"
    if days > MAX_DAYS_TO_RESOLVE:
        return False, f"horizon {days:.1f}d > {MAX_DAYS_TO_RESOLVE:.0f}d"
    return True, ""


def gamma_volume(session: requests.Session,
                 cids: list[str]) -> dict[str, float]:
    """24h traded volume per condition_id, from gamma.

    The CLOB's market payload carries no volume at all, which is why the
    ranker never had this filter: the number it needed was on a different
    host. Queried in chunks because the endpoint takes repeated
    `condition_ids` parameters and the candidate list is a few hundred long.
    """
    out: dict[str, float] = {}
    for i in range(0, len(cids), 20):
        chunk = cids[i:i + 20]
        try:
            rows = session.get(GAMMA, params={"condition_ids": chunk,
                                              "limit": len(chunk)},
                               timeout=20).json()
        except Exception:
            continue
        if isinstance(rows, dict):
            rows = rows.get("data") or []
        for r in rows:
            cid = r.get("conditionId")
            if cid:
                out[cid] = float(r.get("volume24hr") or 0.0)
    return out


def order_score(v: float, s: float, size: float, min_size: float) -> float:
    if s < 0 or s > v or size < min_size:
        return 0.0
    return ((v - s) / v) ** 2 * size


def evaluate(session: requests.Session, rate: float, m: dict,
             volume_24h: Optional[float] = None) -> dict | None:
    """Income and capital for one market, from its live book."""
    rw = m.get("rewards") or {}
    v = (rw.get("max_spread") or 3.5) / 100.0
    min_size = rw.get("min_size") or 50
    toks = [t.get("token_id") for t in (m.get("tokens") or [])]
    if len(toks) != 2 or OFFSET >= v:
        return None

    q1 = q2 = 0.0
    capital_per_share = 0.0
    mids: dict[int, float] = {}
    best_bids: dict[int, float] = {}
    for j, tok in enumerate(toks):
        try:
            b = session.get("https://clob.polymarket.com/book",
                            params={"token_id": tok}, timeout=12).json()
        except Exception:
            return None
        bids = [(float(x["price"]), float(x["size"])) for x in (b.get("bids") or [])]
        asks = [(float(x["price"]), float(x["size"])) for x in (b.get("asks") or [])]
        if not bids or not asks:
            return None
        mid = (max(bids)[0] + min(asks)[0]) / 2.0
        # Outside [0.05, 0.95] the book is one-sided in practice and the
        # position is mostly a bet on a near-settled outcome.
        if not 0.05 < mid < 0.95:
            return None
        mids[j] = mid
        best_bids[j] = max(bids)[0]
        capital_per_share += mid
        for levels, sign, is_bid in ((bids, 1.0, True), (asks, -1.0, False)):
            for p, s in levels:
                d = (mid - p) * sign
                if 0 <= d <= v and s >= min_size:
                    sc = ((v - d) / v) ** 2 * s
                    if is_bid == (j == 0):
                        q1 += sc
                    else:
                        q2 += sc

    theirs = q_min(q1, q2)
    n = max(min_size, 120)

    # Score the price we would ACTUALLY quote, not the price we would like to.
    # The bot never bids more than one tick above the best bid (see
    # quotes._decide_quotes_rewards): on a wide book, mid-minus-offset sits
    # deep inside the spread, and a market whose reward window is empty is
    # usually empty because quoting there means being the most exposed order in
    # the book. Ranking on the uncapped price overstated those markets badly --
    # one showed 15%/day for a quote six cents above the next best bid.
    tick = m.get("minimum_tick_size") or 0.01
    sides = []
    for j in range(2):
        mid = mids[j]
        want = mid - OFFSET
        price = min(want, best_bids[j] + tick)
        s = mid - price
        sides.append(order_score(v, s, n, min_size))
    ours = q_min(sides[0], sides[1])
    if ours <= 0:
        return None            # cannot score here without overbidding the book
    income = rate * ours / (ours + theirs)
    capital = n * capital_per_share

    # U6. A market must be able to produce the observations before its yield
    # is worth comparing. Both reasons are recorded rather than dropped, so
    # the report can show that a universe was refused for being untradeable
    # rather than for being unprofitable -- the distinction the last six runs
    # could not make.
    days = days_to_resolve(m.get("end_date_iso"))
    can_trade, why = tradable(volume_24h, days)
    pays = income >= MIN_PAYOUT * FLOOR_MULTIPLE
    if not why and not pays:
        why = f"income ${income:.2f}/day under payout floor"

    return {
        # Below the payout floor this market pays exactly zero, however good
        # its return_pct_day looks. Recorded rather than filtered here so the
        # report can show what was rejected and why.
        "eligible": pays and can_trade,
        "reject_reason": why,
        "volume_24h": round(volume_24h, 2) if volume_24h is not None else None,
        "days_to_resolve": round(days, 2) if days is not None else None,
        "cid": m["condition_id"],
        "title": m.get("question", "")[:90],
        "slug": m.get("market_slug", ""),
        "daily": rate,
        "min_size": min_size,
        "max_spread": rw.get("max_spread") or 3.5,
        "tick": m.get("minimum_tick_size") or 0.01,
        "shares": n,
        "est_income": round(income, 3),
        "est_capital": round(capital, 2),
        "return_pct_day": round(100 * income / capital, 3) if capital else 0,
        "their_score": round(theirs, 1),
    }


def main() -> None:
    top = 20
    if "--top" in sys.argv:
        top = int(sys.argv[sys.argv.index("--top") + 1])

    s = requests.Session()
    data = s.get("https://clob.polymarket.com/sampling-markets", timeout=30).json()
    cands = []
    for m in data.get("data") or []:
        if not m.get("accepting_orders") or m.get("closed"):
            continue
        rate = sum(x.get("rewards_daily_rate", 0) or 0
                   for x in ((m.get("rewards") or {}).get("rates") or []))
        if rate > 0:
            cands.append((rate, m))
    cands.sort(key=lambda x: -x[0])
    print(f"funded live markets: {len(cands)}  (scoring top 250 by rate)")

    # Volume lives on gamma, the book lives on the CLOB. Fetched up front for
    # the whole candidate list so the per-market workers stay one round trip
    # each, as they were before the filter existed.
    short = [(rate, m) for rate, m in cands[:250]
             if (days_to_resolve(m.get("end_date_iso")) or -1) >= 0]
    vols = gamma_volume(s, [m["condition_id"] for _, m in short])
    print(f"volume read for {len(vols)}/{len(short)} unexpired candidates")

    out = []
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        for r in ex.map(lambda a: evaluate(s, a[0], a[1],
                                           vols.get(a[1]["condition_id"])),
                        short):
            if r:
                out.append(r)
    # Eligibility BEFORE ranking. Sorting on return_pct_day alone put the
    # top-ranked market at $0.25/day actual against $18.96 projected, because a
    # spectacular percentage return on an income of eleven cents is still
    # eleven cents -- and under the payout floor it is zero.
    eligible = [r for r in out if r["eligible"]]
    rejected = len(out) - len(eligible)
    eligible.sort(key=lambda r: -r["return_pct_day"])
    picked = eligible[:top]

    RUN.mkdir(exist_ok=True)
    (RUN / "markets.json").write_text(json.dumps(picked, indent=1), encoding="utf-8")

    ti = sum(r["est_income"] for r in picked)
    tc = sum(r["est_capital"] for r in picked)
    # Rejections grouped by cause. A run that returns nothing must say whether
    # the venue had no tradeable market today or the filters are set wrong --
    # a bare count cannot, and a silent empty universe is how the fleet ended
    # up quoting markets that never traded.
    causes: dict[str, int] = {}
    for r in out:
        if not r["eligible"]:
            causes[r["reject_reason"].split(" $")[0].split(" ")[0]] = (
                causes.get(r["reject_reason"].split(" $")[0].split(" ")[0], 0) + 1)
    print(f"scored {len(out)}, rejected {rejected} "
          f"({', '.join(f'{k}={v}' for k, v in sorted(causes.items())) or 'none'}), "
          f"wrote top {len(picked)} -> run/markets.json")
    print(f"gates: 24h volume >= ${MIN_VOLUME_24H:,.0f}, "
          f"resolves within {MAX_DAYS_TO_RESOLVE:.0f}d, "
          f"income >= ${MIN_PAYOUT * FLOOR_MULTIPLE:.2f}/day\n")
    print(f"{'market':<46}{'$/day':>7}{'capital':>9}{'ret%/d':>8}")
    for r in picked:
        # Windows consoles default to a legacy codepage; market titles carry
        # curly quotes and accents that crash a plain print AFTER the file is
        # already written, which looks like a failed run when it succeeded.
        title = r["title"][:46].encode("ascii", "replace").decode("ascii")
        print(f"{title:<46}{r['est_income']:>7.2f}"
              f"{r['est_capital']:>9.0f}{r['return_pct_day']:>8.2f}")
    if tc:
        print(f"\nTOTAL capital ${tc:,.0f}  income ${ti:,.2f}/day  = {100*ti/tc:.2f}%/day")


if __name__ == "__main__":
    main()
