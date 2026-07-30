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
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RUN = ROOT / "run"
OFFSET = 0.020          # where we intend to quote, in price units
C = 3.0                 # venue's one-sided penalty


def q_min(a: float, b: float) -> float:
    return max(min(a, b), max(a / C, b / C))


def order_score(v: float, s: float, size: float, min_size: float) -> float:
    if s < 0 or s > v or size < min_size:
        return 0.0
    return ((v - s) / v) ** 2 * size


def evaluate(session: requests.Session, rate: float, m: dict) -> dict | None:
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
    return {
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

    out = []
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        for r in ex.map(lambda a: evaluate(s, *a), cands[:250]):
            if r:
                out.append(r)
    out.sort(key=lambda r: -r["return_pct_day"])
    picked = out[:top]

    RUN.mkdir(exist_ok=True)
    (RUN / "markets.json").write_text(json.dumps(picked, indent=1), encoding="utf-8")

    ti = sum(r["est_income"] for r in picked)
    tc = sum(r["est_capital"] for r in picked)
    print(f"scored {len(out)}, wrote top {len(picked)} -> run/markets.json\n")
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
