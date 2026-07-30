"""Turn a matched pair back into collateral, at parity, today.

One YES plus one NO is redeemable for exactly 1.00 -- not at resolution in
2027, but now, on-chain, through Polymarket's collateral adapter. That single
fact is what separates this strategy from a carry trade.

`profit_take.py` is the other exit, and it cannot do this job. Selling a pair
means crossing the spread on both legs, so the ceiling is

    max bid_up + bid_dn   ~ 1.000   (arbitrage bound)
    pair cost             = 0.973   (measured median over 6,541 pairs)
    two taker fees        = 0.034
    ------------------------------------------------
    best case             = -0.007 / share

against a +0.020 threshold. It is not that the threshold was set too high; the
arithmetic cannot reach it. Over 18.7 hours it fired zero times while inventory
accumulated at $512/hour and froze until 2027 at ~2.1%/yr.

Merge pays 1.00 flat. No spread, no fee, gas aside. The measured pair cost of
0.9728 makes that +2.79% per cycle, and a cycle is minutes rather than years --
which is the whole reason capital can compound here at all.

Pure arithmetic. It decides; the caller applies and persists.
"""
from __future__ import annotations

# A complete set redeems for exactly one unit of collateral. Not a tunable.
PARITY = 1.00

NO: dict = {"take": False, "shares": 0.0, "cost_basis": 0.0, "proceeds": 0.0,
            "gas": 0.0, "realized_pnl": 0.0, "gain_per_share": 0.0,
            "velocity_justified": False,
            "up_cost_removed": 0.0, "dn_cost_removed": 0.0, "why": ""}


def _no(why: str) -> dict:
    return dict(NO, why=why)


def should_merge(inv, cfg, gas_cost: float | None = None,
                 projected_rent_per_day: float | None = None,
                 hold_days: float | None = None) -> dict:
    """Should the paired portion of this position be merged into collateral?

    Only `min(up_shares, down_shares)` is considered. The naked residue is left
    entirely alone -- it is a directional bet owned by skew and the exposure
    caps, and merge cannot touch it in any case: a merge consumes one share of
    EACH outcome, so an unpaired share has nothing to pair with.

    Unlike selling, there is no book to walk. Parity is not a price somebody
    has to be willing to pay, it is what the contract pays out, so depth is
    irrelevant and the whole paired position is always merge-able at once.
    That is the structural advantage over `profit_take.should_close`, which is
    capped by whatever size happens to be resting on the bid.

    `gas_cost` is the total cost of the transaction, in dollars, NOT per share.
    A merge is one transaction whatever its size, which is why the floor below
    is on total gain rather than per-share gain -- a thin per-share margin on a
    large pair count is still worth a transaction, and a fat one on ten shares
    is not.

    None means the cost is unknown, and unknown blocks. Treating it as zero
    would make every merge look profitable, which is the exact silent-failure
    shape that already cost this project once when an `except: pass` reported
    $0.00 as good news.

    Never mutates `inv`.
    """
    paired = min(inv.up_shares, inv.down_shares)
    if paired <= 0:
        return _no("no paired shares")

    if gas_cost is None:
        return _no("gas cost unknown -- refusing to price a merge at zero")
    if gas_cost < 0:
        return _no(f"nonsensical gas cost {gas_cost}")

    cost_per_share = inv.avg("UP") + inv.avg("DOWN")
    gain_per_share = PARITY - cost_per_share

    cost_basis = paired * cost_per_share
    proceeds = paired * PARITY
    realized_pnl = proceeds - cost_basis - gas_cost

    # OVER-PARITY EXCEPTION (KTD2b). A pair costing more than 1.00 loses money
    # on the merge, and holding it returns exactly the same 1.00 later -- so
    # the nominal comparison is a wash and the only real question is what the
    # freed capital earns in between.
    #
    # ORDER IS LOAD-BEARING: cap, then velocity, then gas. The cap is checked
    # first and is never yielded to, so no projected-rent figure however large
    # can license an arbitrarily bad exit price. Reversing these two would turn
    # a capital-efficiency rule into an inventory fire sale, which is exactly
    # the failure the bound exists to prevent.
    velocity_justified = False
    if gain_per_share < 0:
        loss_per_share = -gain_per_share
        cap = getattr(cfg, "merge_max_loss_per_share", 0.0)
        # Tolerance, because prices are decimals stored as binary floats: a
        # pair at 0.51 + 0.50 computes a loss of 0.010000000000000009, which a
        # bare `>` reads as over a 0.01 cap. A pair exactly at the limit is
        # inside it, and a rounding artifact must not be what decides a money
        # rule. 1e-9 is far below one tick (0.001) so it cannot admit a price
        # the venue could actually quote.
        if loss_per_share > cap + 1e-9:
            return _no(
                f"hold: {100 * loss_per_share:.2f}c/sh over parity exceeds the "
                f"{100 * cap:.2f}c cap -- velocity not evaluated")
        # Unknown rent blocks, exactly as unknown gas does. A missing figure is
        # not a favourable one, and defaulting it would make every over-parity
        # pair mergeable on an assumption nobody measured.
        if projected_rent_per_day is None or hold_days is None:
            return _no(
                f"hold: {100 * loss_per_share:.2f}c/sh over parity and the "
                f"return on freed capital is unknown -- refusing to assume it")
        freed_earnings = projected_rent_per_day * max(0.0, hold_days)
        concession = paired * loss_per_share + gas_cost
        if freed_earnings <= concession:
            return _no(
                f"hold: freeing ${cost_basis:.2f} earns ${freed_earnings:.2f} "
                f"over {hold_days:.0f}d, under the ${concession:.2f} concession")
        velocity_justified = True

    out = {
        # A velocity-justified merge has a negative realized_pnl by
        # construction; it passed a different test and must not be re-judged
        # by the profitability one.
        "take": velocity_justified or realized_pnl > 0,
        "velocity_justified": velocity_justified,
        "shares": paired,
        "cost_basis": cost_basis,
        "proceeds": proceeds,
        "gas": gas_cost,
        "realized_pnl": realized_pnl,
        "gain_per_share": gain_per_share,
        # Captured so the caller can remove each leg at its own average rather
        # than splitting a combined basis afterwards. The `closes` table
        # already learned that lesson the hard way.
        "up_cost_removed": paired * inv.avg("UP"),
        "dn_cost_removed": paired * inv.avg("DOWN"),
    }
    # Name the test that decided it. A merge booked at -0.6c/sh is correct
    # under velocity and a bug under the profitability rule, and the reader
    # cannot tell which from the net alone.
    out["why"] = (
        f"merge {paired:.0f} pairs @ cost {cost_per_share:.4f} -> "
        f"{PARITY:.2f}, {100 * gain_per_share:.2f}c/sh gross, "
        f"gas ${gas_cost:.4f}, net ${realized_pnl:.2f}"
        f"{' [velocity: freeing capital beats holding]' if velocity_justified else ''}"
        if out["take"] else
        f"hold: {paired:.0f} pairs @ cost {cost_per_share:.4f}, "
        f"{100 * gain_per_share:.2f}c/sh gross does not clear "
        f"${gas_cost:.4f} gas")
    return out


def pairing_rate(merged_shares: float, filled_shares: float) -> float | None:
    """Merged pairs as a fraction of shares filled.

    Merge economics assume fills arrive roughly two-sided, because merging
    needs a MATCHED pair -- a one-sided fill produces nothing to merge. The
    18.7h run left ~12% of flow naked with the $800 cap binding, so the
    assumption held there, but it is an assumption and this is how it gets
    measured rather than discovered late.

    None with nothing filled: no observation is not a pairing rate of zero.
    """
    if filled_shares <= 1e-9:
        return None
    return merged_shares / filled_shares
