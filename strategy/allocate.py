"""Capital allocation by marginal return.

`quote_shares` was a flat 120 on every market, so every market got the same
~$115 whether it returned 27.58%/day or 0.28%/day -- a 98x spread, measured
live on 2026-07-29.

Why the spread is that wide is worth stating plainly, because it is the
opposite of the intuition: income is `pot x share`, and share is
`ours / (ours + theirs)`. Pot size barely matters; competition does. Big pots
are big precisely because the venue is paying up for liquidity nobody wants to
provide cheaply, so makers crowd in. Measured, the $100 and $74 pots sat near
the BOTTOM of the return table while a $50 pot paid 27x better on identical
capital.

Nothing here does I/O. It is a pure function of measured state, so it can be
tested against the real numbers the fleet reported.
"""
from __future__ import annotations


def competitor_depth(capital: float, share: float) -> float:
    """Invert `share = C/(C+T)` to recover T -- everyone else's resting depth,
    expressed in the same units as our own committed capital.

    A zero share means the competition is effectively unbounded (we scored
    nothing against them), so this returns infinity and the caller's marginal
    return collapses to zero. That is the correct reading, not an error.
    """
    if share <= 0.0:
        return float("inf")
    if share >= 1.0:
        return 0.0
    return capital * (1.0 - share) / share


def income(capital: float, daily: float, T: float) -> float:
    """Rent per day at this commitment. Asymptotes to `daily` -- a market can
    never pay more than its pot, however much we push in."""
    if capital <= 0:
        return 0.0
    return daily * capital / (capital + T)


def marginal(capital: float, daily: float, T: float) -> float:
    """d(income)/d(capital): what the next dollar earns per day.

    This is the whole reason the allocator does not simply empty the budget
    into the best market -- our own size dilutes our share, so the return on
    each extra dollar falls as we commit more.

    T == 0 means we are the entire book and take the whole pot at any size.
    The derivative is then a step, not a curve: the first dollar is worth the
    whole `daily`, every dollar after it is worth nothing. Evaluating the
    formula naively at capital == 0 gives 0/0 -- which crashed the live fleet
    at 13:40 on 2026-07-29, killing the bot mid-sweep and costing 3.5 hours of
    collection. Both branches are now answered explicitly.
    """
    if T <= 0:
        return daily if capital <= 0 else 0.0
    return daily * T / ((capital + T) ** 2)


def spread_capture_daily(volume_24h_usd: float, spread: float,
                         capture_frac: float = 0.25,
                         avg_price: float = 0.5) -> float:
    """Pot-equivalent, in $/day, for a market that pays no rewards at all.

    Everything else in this module is written against `daily` -- the dollars a
    market hands out per day, split by score share. A market with
    `clobRewards: 0` has no such pot, so every function here values it at zero
    and the fleet never funds it. That is backwards for the markets U6 wants:
    bitcoin-up-or-down-* pays nothing for resting and trades ~$92k in 24 hours.
    The income is there; it arrives as spread rather than as emissions.

    So a synthetic pot is computed and handed to the same machinery:

        shares/day = volume_24h / avg_price
        pot        = shares/day x spread x capture_frac

    It is a pot in the same sense the reward pot is -- what the market pays out
    in total, of which we take our depth's share -- so `income()` and
    `marginal()` need no special case: our share still dilutes as we push size
    in, and the water-fill compares a spread market against a reward market on
    one axis.

    `capture_frac` is the honest part of the estimate, and it is a HYPOTHESIS
    rather than a measurement. A maker resting inside the touch earns at most
    half the quoted spread per round trip, and earns less in practice because
    the flow that reaches a resting order is selected against it -- which is
    the entire subject of the markout gate. 0.25 is half of that theoretical
    half-spread. The first real markout sample on one of these markets should
    replace it.

    `avg_price` is 0.5 because a binary pair costs $1 and volume is quoted in
    dollars: near the middle of the book each share changes hands for about
    half a dollar.
    """
    if volume_24h_usd <= 0 or spread <= 0 or avg_price <= 0 or capture_frac <= 0:
        return 0.0
    return (volume_24h_usd / avg_price) * spread * capture_frac


def shares_for(dollars: float, min_size: int) -> int:
    """Turn an allocation in dollars into a per-side quote size.

    A pair costs about $1 by construction -- one UP at p plus one DOWN at
    roughly (1-p) -- so N dollars of committed capital buys about N shares on
    each side. No division by price is needed, and doing one would be wrong.

    Returns 0 below the market's minimum: quoting under min_size scores
    exactly zero at the venue, so a sub-minimum allocation is strictly worse
    than not quoting at all -- it ties up capital and earns nothing.
    """
    n = int(dollars)
    return n if n >= min_size else 0


def allocate(markets: list[dict], budget: float, floor: float,
             step: float = 5.0, max_frac: float = 1.0) -> dict[str, float]:
    """Water-fill: hand each increment to whichever market currently has the
    highest marginal return, until the budget is exhausted or no market clears
    the floor.

    Leftover budget is deliberately NOT forced out. Capital earning under the
    floor is worse than idle capital, which at least stays available for
    something better -- and every extra market carries its own fill risk.

    `markets` items need `cid`, `daily`, `capital`, `share`; `capital` and
    `share` are the CURRENT observation, used only to infer the competition.
    Returns dollars per market.

    `max_frac` bounds any ONE market's share of the budget, and it exists
    because this function was read as a diversifier and is not one. Marginal
    return is daily*T/(capital+T)^2, which is nearly FLAT in capital whenever
    competitor depth T dominates our own size -- so the argmax never changes
    hands and one market absorbs every increment. Measured 2026-08-02: a single
    market took the entire $900 budget, `shares_for` turned it into a 900-share
    order, and it filled in one print for $792 -- 79% of a $1,000 wallet,
    against a nominal $400 per-market cost cap.

    A full market is SKIPPED, not a reason to stop: the budget should keep
    flowing to the next-best market, which is the entire point of the cap.
    Defaults to 1.0 so existing callers keep the old behaviour and only the
    fleet runner, which has a configured fraction, opts in.
    """
    T = {m["cid"]: competitor_depth(m["capital"], m["share"]) for m in markets}
    daily = {m["cid"]: m["daily"] for m in markets}
    out = {m["cid"]: 0.0 for m in markets}
    cap = budget * max_frac if max_frac < 1.0 else float("inf")

    spent = 0.0
    while spent < budget:
        best, best_marginal = None, floor
        for cid in out:
            # Full markets leave the auction entirely. Comparing one and then
            # refusing to award it would stall the loop on a market that can
            # never take another dollar.
            if out[cid] + step > cap:
                continue
            mg = marginal(out[cid], daily[cid], T[cid])
            if mg > best_marginal:
                best, best_marginal = cid, mg
        if best is None:
            break          # nothing worth funding, or everything is full
        out[best] += step
        spent += step
    return out


def allocate_fundable(markets: list[dict], budget: float, floor: float,
                      payout_floor: float, step: float = 5.0,
                      max_frac: float = 1.0) -> dict[str, float]:
    """Water-fill, then drop whatever the funded size cannot actually earn on,
    and redistribute what that frees. Every input cid appears in the result;
    the ones that survived nothing are 0.0.

    Two ways an allocation is dead on arrival, and plain `allocate` sees
    neither, because both depend on the market's own venue terms rather than
    on the marginal return:

      * Under `min_dollars`. `shares_for` refuses to quote below a market's
        minimum -- an order that small scores zero -- so the dollars are
        committed on paper and buy nothing at all.
      * Under `payout_floor`. Polymarket pays nothing below $1 per
        distribution, so a market projecting under it is not a small earner,
        it earns exactly zero.

    The payout floor is a REWARD rule, and it applies only to markets earning
    reward income. A market whose `source` is "spread" is paid by the taker who
    lifts its offer, in the amount of the spread, on the trade -- there is no
    distribution and therefore no minimum distribution. Applying the floor
    there would defund exactly the liquid short-dated markets U6 added it for:
    the venue never pays them a dollar a day because the venue never pays them
    anything. Markets carry `source` themselves (default "rewards", so existing
    callers are unaffected) rather than the caller passing two floors, because
    the two kinds sit in one water-fill and are judged in one pass.

    Judged AT the allocation, not at a fixed probe size, because income is
    monotone in size: Taylor Swift pays $1.00/day at its 100-share minimum and
    $5.50/day at the 600 shares the budget can afford. A probe at the minimum
    would defund a market paying 3.6x the floor.

    An under-minimum allocation is not automatically a bad market, so it is
    offered a promotion to the full lot before being dropped. The water-fill
    is a marginal-return argument and marginal return cannot see an
    indivisible lot: the two markets we face no competition in have T = 0, and
    marginal() is then a step -- the first dollar is worth the whole pot, every
    dollar after it is worth nothing -- so the fill hands them $5 and moves on.
    $5 cannot be quoted. Dropping them costs the two best returns on the board
    ($5/day each on a $100 minimum, 5%/day), so the lot gets judged on the
    AVERAGE return over the whole lot, which is what an indivisible commitment
    actually earns.

    One change per pass, promotions before drops, and the worst market first
    among drops. Dropping frees budget for the survivors and can lift a market
    that was only just under, so clearing every offender at once would defund
    markets the redistribution would have saved.
    """
    live = list(markets)
    fixed: dict[str, float] = {}          # markets held at their minimum lot
    out = {m["cid"]: 0.0 for m in markets}
    # The concentration cap is a property of the BUDGET, so it is computed once
    # against the full budget rather than against the shrinking `free` -- if it
    # tracked `free`, each promotion would tighten the cap on everyone left and
    # the limit would depend on the order markets happened to be promoted in.
    cap = budget * max_frac if max_frac < 1.0 else float("inf")
    while True:
        free = max(budget - sum(fixed.values()), 0.0)
        # Re-expressed as a fraction of `free`, because that is the budget
        # `allocate` actually sees. Clamping its OUTPUT instead would cap the
        # right markets and then throw the freed dollars away -- the water-fill
        # has to know a market is full while it still has increments to hand
        # out, or the surplus never reaches the next-best market.
        frac = 1.0 if cap == float("inf") or free <= 0 else min(cap / free, 1.0)
        dollars = allocate(live, free, floor, step, max_frac=frac) if live else {}

        promote, drop = None, None
        for m in live:
            d = dollars.get(m["cid"], 0.0)
            if d <= 0:
                continue        # under the marginal floor; allocate said so
            T = competitor_depth(m["capital"], m["share"])
            lot = m.get("min_dollars", 0.0)
            # Zero for spread markets: the floor is a minimum DISTRIBUTION, and
            # spread income is not distributed.
            pf = payout_floor if m.get("source", "rewards") == "rewards" else 0.0
            if d < lot:
                inc = income(lot, m["daily"], T)
                # `lot <= cap` closes the obvious way back into the bug this
                # cap exists to stop: the promotion path funds an indivisible
                # minimum OUTSIDE the water-fill, so without it a market whose
                # venue minimum exceeds the concentration limit would be handed
                # that minimum in full. A market we cannot fund without
                # breaching the limit is one we do not fund.
                if lot <= free and lot <= cap and inc >= pf and inc / lot >= floor:
                    if promote is None or inc / lot > promote[1]:
                        promote = (m["cid"], inc / lot, lot)
                elif drop is None or inc < drop[1]:
                    drop = (m["cid"], inc)
                continue
            inc = income(d, m["daily"], T)
            if inc < pf and (drop is None or inc < drop[1]):
                drop = (m["cid"], inc)

        if promote is not None:
            cid, _, lot = promote
            fixed[cid] = lot
            live = [m for m in live if m["cid"] != cid]
            continue
        if drop is not None:
            live = [m for m in live if m["cid"] != drop[0]]
            continue

        out.update(dollars)
        out.update(fixed)
        return out


def capital_scarcity(markets: list[dict], allocation: dict[str, float],
                     budget: float, floor: float,
                     multiple: float = 2.0) -> bool:
    """Is the budget the binding constraint on a market still worth funding?

    Two distinct reasons `allocate` stops, and only one of them is scarcity:

      * Nothing cleared the floor. The budget is intact and idle by choice --
        there is nowhere good to put it. Not scarce.
      * The budget ran out while a market was still returning well above the
        floor. That market is underfunded, and the next dollar it does not get
        is a real loss. Scarce.

    `multiple` is what separates "above the floor" from "well above it". At 1.0
    every fully spent budget would read as scarce, since the water-fill only
    exhausts the budget while something is still over the floor -- the relaxed
    profit-take threshold would become the normal case rather than the
    exception it is meant to be.

    Measured on the same numbers that motivated this module: returns spanning
    27.58%/day to 0.28%/day across markets means a dollar sitting in a stagnant
    pair is not neutral, it is the 27%/day market going unfunded.

    Pure, like everything else here -- the caller decides what to do about it.
    """
    if budget <= 0 or not markets:
        return False
    if sum(allocation.values()) < budget - 1e-9:
        return False        # budget survived: the floor stopped us, not scarcity

    threshold = floor * multiple
    for m in markets:
        T = competitor_depth(m["capital"], m["share"])
        if marginal(allocation.get(m["cid"], 0.0), m["daily"], T) >= threshold:
            return True
    return False
