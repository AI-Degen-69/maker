"""Polymarket liquidity-reward scoring, as the venue defines it.

Extracted so the multi-market fleet and the ranking script compute scores the
same way. Two details here are easy to get wrong and both were, once:

  * The per-order score is quadratic in distance from the midpoint, so backing
    off is expensive fast -- at 2c of a 4.5c window an order keeps 31% of the
    score it would earn at the touch.
  * A maker's sample score is NOT the sum of their two sides. It is Q_min,
    which takes the MINIMUM of the two sides (with a reduced rate for
    one-sided). Summing reports a balanced quote at double its true score, and
    therefore doubles the estimated income.
"""
from __future__ import annotations

C = 3.0          # venue's one-sided scaling penalty, currently 3.0 everywhere


def order_score(max_spread: float, spread_from_mid: float, size: float,
                min_size: float) -> float:
    """S(v, s) = ((v - s)/v)^2 * size, and 0 outside the window."""
    if spread_from_mid < 0 or spread_from_mid > max_spread or size < min_size:
        return 0.0
    return ((max_spread - spread_from_mid) / max_spread) ** 2 * size


def q_min(q_one: float, q_two: float) -> float:
    """Q_min = max( min(Q_one, Q_two), max(Q_one/c, Q_two/c) )."""
    return max(min(q_one, q_two), max(q_one / C, q_two / C))


def score_per_share(max_spread: float, offset: float) -> float:
    """Score one share of a balanced two-sided quote earns at `offset`.

    Answers "what WOULD we score here", which is a different question from
    `our_scores` -- that one reads our resting orders, and a market the
    allocator has defunded has none. Sizing off a measurement that only exists
    while we are already funded is what latched the fleet on 2026-07-30:
    defund once, measure zero forever, never fund again.

    Balanced two-sided collapses Q_min to the single-side score -- both sides
    are equal, so min(Q_one, Q_two) is that value and it is never below either
    side over c. Size then cancels, leaving a per-share constant, ((v-s)/v)^2,
    that converts a competing SCORE into the share count needed to match it.
    At the shipped 2.0c offset in a 4.5c window it is 0.3086, so 120 shares
    score 37.04 -- exactly what the venue reported for our 120-share quotes.
    """
    return order_score(max_spread, offset, 1.0, 0.0)


def book_scores(up: dict, dn: dict, max_spread: float, min_size: float
                ) -> tuple[float, float]:
    """(Q_one, Q_two) for everything resting in both books.

    Q_one = bids on UP + asks on DOWN;  Q_two = asks on UP + bids on DOWN.
    A bid on DOWN is economically an ask on UP, which is why the two books
    fold into one pair of side-scores rather than being scored separately.
    """
    q1 = q2 = 0.0
    for book, is_up in ((up, True), (dn, False)):
        bb, ba = book.get("best_bid"), book.get("best_ask")
        if bb is None or ba is None:
            continue
        mid = (bb + ba) / 2.0
        for levels, sign, is_bid in ((book["bids"], 1.0, True),
                                     (book["asks"], -1.0, False)):
            for price, size in levels.items():
                s = (mid - price) * sign
                sc = order_score(max_spread, s, size, min_size)
                if sc <= 0:
                    continue
                if is_bid == is_up:
                    q1 += sc
                else:
                    q2 += sc
    return q1, q2


def our_scores(orders, up: dict, dn: dict, max_spread: float, min_size: float
               ) -> tuple[float, float]:
    """(Q_one, Q_two) for our own resting orders. We only ever post bids."""
    q1 = q2 = 0.0
    for o in orders:
        book = up if o.side == "UP" else dn
        bb, ba = book.get("best_bid"), book.get("best_ask")
        if bb is None or ba is None:
            continue
        mid = (bb + ba) / 2.0
        remaining = max(0.0, o.size - o.filled)
        sc = order_score(max_spread, mid - o.price, remaining, min_size)
        if sc <= 0:
            continue
        if o.side == "UP":
            q1 += sc
        else:
            q2 += sc
    return q1, q2


def share_of_pool(our_q1: float, our_q2: float,
                  book_q1: float, book_q2: float) -> tuple[float, float, float]:
    """(our_score, others_score, share).

    The book measured here does NOT contain our orders -- in simulation we post
    nothing real. Live, our size would rest in that book and count toward the
    total, so the pool splits over ours PLUS theirs. Dividing by theirs alone
    overstates the share, worst in exactly the thin markets worth picking.
    """
    ours = q_min(our_q1, our_q2)
    theirs = q_min(book_q1, book_q2)
    total = ours + theirs
    return ours, theirs, (ours / total if total > 0 else 0.0)
