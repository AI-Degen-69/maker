"""Turn a locked pair back into working capital.

A matched pair pays exactly $1.00 -- in 2027. Until then the money that
bought it is immobilised, and immobilised money earns no daily rent. That is
the real cost of a fill, and nothing in the strategy addressed it: every
position simply rode to settlement.

If the pair can be sold today for more than it cost, the trade is finished
early -- the profit is booked and the capital goes back to work. Selling
means crossing the spread on BOTH legs, so the move has to be big enough to
cover two taker fees before it is worth anything at all.

Pure arithmetic. It decides; the caller applies.
"""
from __future__ import annotations

NO: dict = {"take": False, "shares": 0.0, "cost_basis": 0.0, "proceeds": 0.0,
            "fee": 0.0, "realized_pnl": 0.0, "forgone_vs_settlement": 0.0,
            "up_avg_price": 0.0, "dn_avg_price": 0.0, "why": ""}


def _no(why: str) -> dict:
    return dict(NO, why=why)


def _walk(bids: dict, qty: float) -> tuple[float, float]:
    """Proceeds AND the size-weighted average price from selling `qty` shares
    into `bids`, walking the ladder from the best price down. Returns
    proceeds/avg for whatever portion of `qty` the ladder can actually absorb
    (caller is responsible for capping `qty` at the ladder's total depth
    beforehand -- this just prices it)."""
    if qty <= 0 or not bids:
        return 0.0, 0.0
    remaining = qty
    proceeds = 0.0
    for price in sorted(bids.keys(), reverse=True):
        if remaining <= 0:
            break
        size = bids[price]
        take = min(remaining, size)
        proceeds += take * price
        remaining -= take
    filled = qty - remaining
    avg_price = (proceeds / filled) if filled > 0 else 0.0
    return proceeds, avg_price


def should_close(inv, up_bids, dn_bids, cfg, capital_scarce: bool = False) -> dict:
    """Should the paired portion of this position be sold now?

    `up_bids` / `dn_bids` are the venue's full bid LADDERS for each side --
    `{price: size}`, the same shape `full_book()` in strategy/main.py returns
    as `book["bids"]`. We would be the seller, and a seller hits bids, never
    asks.

    Only `min(up_shares, down_shares)` is considered. The naked residue is
    left entirely alone: it is a directional bet, owned by skew and the
    exposure caps, and closing it here would be a different decision wearing
    this one's clothes.

    DEPTH, not top-of-book. On the thin books this strategy targets, the
    resting bid is often a fraction of the paired position -- "closing" 600
    pairs against a 50-share bid is fiction. The sellable quantity is capped
    at the smaller of the two legs' available depth (a pair needs one UP
    share sold AND one DOWN share sold), and never exceeds `paired`.

    We WALK the ladder rather than pricing everything at the top: it is the
    honest number, since selling size below the top level really does average
    down to a worse price, and pricing a large close at the top tick would
    book proceeds nobody could actually collect. Because deeper levels are
    worse, the achieved average price falls as the size sold grows, so the
    profitability test below is run on the ACHIEVED average price for the
    size actually being closed, not on the top-of-book price.

    A partial close -- sell what the book can absorb, leave the rest -- is
    the intended behaviour, not a degraded case. `shares` in the returned
    dict is the quantity actually closable, and `cost_basis` / `proceeds` /
    `fee` / `realized_pnl` are all computed on that quantity so the caller's
    arithmetic (which mutates inventory by exactly `shares`) stays consistent.

    `capital_scarce` is the allocator's report that the budget is the binding
    constraint on a market still returning well above the marginal floor (see
    `allocate.capital_scarcity`). When it is set, the required net drops from
    `profit_take_net_threshold` to `scarcity_close_threshold` -- a slightly
    NEGATIVE number, so a stagnant pair is sold at a small loss to free the
    dollars behind it.

    That is not a contradiction of the threshold above, it is the threshold
    finally pricing the alternative. The normal rule compares closing against
    holding and nothing else; under scarcity the true comparison is closing
    against the return the freed capital earns in the starved market, which is
    measured in per-DAY percent while the concession here is a one-off half a
    cent per share.

    Deliberately bounded: the relaxed threshold is a fraction of a fee, not an
    instruction to dump inventory. A pair far under water still fails it.

    Never mutates `inv`.
    """
    paired = min(inv.up_shares, inv.down_shares)
    if paired <= 0:
        return _no("no paired shares")
    if not up_bids or not dn_bids:
        return _no("no two-sided book")

    depth_up = sum(up_bids.values())
    depth_dn = sum(dn_bids.values())
    sellable = min(paired, depth_up, depth_dn)
    if sellable <= 0:
        return _no("book cannot absorb any pairs (zero depth on one leg)")

    cost_per_share = inv.avg("UP") + inv.avg("DOWN")
    # Two legs, each paying the taker fee. Fixed per share -- independent of
    # how deep into the ladder we had to walk to fill it.
    fee_per_share = 2.0 * cfg.profit_take_fee_per_share

    up_proceeds, up_avg_price = _walk(up_bids, sellable)
    dn_proceeds, dn_avg_price = _walk(dn_bids, sellable)
    proceeds = up_proceeds + dn_proceeds
    cost_basis = sellable * cost_per_share
    fee = sellable * fee_per_share
    realized_pnl = proceeds - cost_basis - fee
    net_per_share = realized_pnl / sellable
    exit_per_share = proceeds / sellable

    # What holding to settlement would have netted on these shares, minus what
    # closing actually netted. Recorded because the close is justified by
    # capital velocity (money freed ~1.5 years early to earn daily rent
    # elsewhere), NOT by nominal value -- two bids almost always sum to under
    # $1.00, so a close nearly always forgoes some settlement value. A reader
    # who cannot see this number cannot check that the trade-off was actually
    # worth it; without it, the feature reads as pure gain instead of a
    # deliberate trade of a few cents for early capital.
    forgone_vs_settlement = (1.00 - cost_per_share) * sellable - realized_pnl

    threshold = (cfg.scarcity_close_threshold if capital_scarce
                 else cfg.profit_take_net_threshold)

    out = {
        "take": net_per_share >= threshold,
        "shares": sellable,
        "cost_basis": cost_basis,
        "proceeds": proceeds,
        "fee": fee,
        "realized_pnl": realized_pnl,
        "forgone_vs_settlement": forgone_vs_settlement,
        # The achieved size-weighted average price actually realized on each
        # leg, NOT top-of-book. Exposed so the caller can log a `closes` row
        # whose price columns agree with `proceeds` -- logging the top-of-
        # book tick here instead would silently contradict `proceeds` on any
        # partial close that walked into a second, worse level.
        "up_avg_price": up_avg_price,
        "dn_avg_price": dn_avg_price,
    }
    partial = sellable < paired
    # Name the threshold that actually decided it. A close booked at -0.3c/sh
    # is correct under scarcity and a bug under the normal rule, and the reader
    # cannot tell which they are looking at from the net alone.
    scarce_tag = " [capital scarce]" if capital_scarce else ""
    out["why"] = (
        f"close {sellable:.0f}{'/' + format(paired, '.0f') + ' (book-limited)' if partial else ''} "
        f"pairs @ avg {exit_per_share:.4f} vs cost {cost_per_share:.4f}, "
        f"net {100 * net_per_share:.2f}c/sh{scarce_tag}"
        if out["take"] else
        f"hold: net {100 * net_per_share:.2f}c/sh under "
        f"{100 * threshold:.2f}c{scarce_tag}"
        f"{' (book-limited to ' + format(sellable, '.0f') + '/' + format(paired, '.0f') + ')' if partial else ''}")
    return out
