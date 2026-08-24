"""Where to rest bids. The decision layer of the Hunter sim.

Mirrors powerwinner's measured behaviour: quote BOTH outcomes, stay ~92%
balanced, never let the pair cost reach $1.00 (it pays exactly $1.00, so a pair
bought at >= 1.00 is a guaranteed loss), and keep quotes inside the rebate
window (>= 50 shares, within 4.5c of mid).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from strategy import gate, risk
from strategy.config import MakerConfig


@dataclass
class QuoteIntent:
    side: str            # 'UP' | 'DOWN'
    token_id: str
    price: float
    size: int
    mid: float
    edge_vs_mid: float   # mid - price, our theoretical capture per share
    reason: str = ""
    crossed: bool = False  # True = we crossed the spread to BUY (balance hedge)


@dataclass
class Inventory:
    up_shares: float = 0.0
    down_shares: float = 0.0
    up_cost: float = 0.0
    down_cost: float = 0.0
    fills: int = 0
    # Wall time of the most recent fill (rebuilt from the fills ledger by
    # `stats.inventory_from_db`; None before the first fill). The pairs-only
    # rule (U35) dates its 15-minute action window off this -- a naked leg
    # older than the window is left alone rather than force-exited.
    last_fill_ts: float | None = None

    @property
    def cost(self) -> float:
        return self.up_cost + self.down_cost

    @property
    def balance(self) -> float:
        """min/max of the two legs. 1.0 = perfectly hedged."""
        hi = max(self.up_shares, self.down_shares)
        return (min(self.up_shares, self.down_shares) / hi) if hi > 0 else 1.0

    def avg(self, side: str) -> float:
        sh = self.up_shares if side == "UP" else self.down_shares
        c = self.up_cost if side == "UP" else self.down_cost
        return (c / sh) if sh > 0 else 0.0

    def pair_cost(self) -> float:
        """avg(UP) + avg(DOWN). Under 1.00 means the hedged part is locked in."""
        if self.up_shares <= 0 or self.down_shares <= 0:
            return 0.0
        return self.avg("UP") + self.avg("DOWN")


def _in_band(cfg: MakerConfig, price: float) -> bool:
    """Is this a price worth resting at? 54% of powerwinner's volume is here."""
    if not cfg.enforce_price_band:
        return True
    return cfg.price_band_low <= price <= cfg.price_band_high


def mid_price(best_bid: Optional[float], best_ask: Optional[float]) -> Optional[float]:
    if best_bid is None or best_ask is None:
        return None
    return (best_bid + best_ask) / 2.0


def reward_score(cfg: MakerConfig, spread_from_mid: float, size: float) -> float:
    """Polymarket liquidity-reward score for one resting order.

        S(v, s) = ((v - s) / v)^2 * size,    v = rewardsMaxSpread (4.5c)

    Quadratic, so it collapses fast: at 2c of 4.5c an order keeps only 30% of
    the score it would earn at the touch. Orders outside v score nothing.
    """
    v = cfg.max_spread_from_mid
    if spread_from_mid < 0 or spread_from_mid > v or size < cfg.min_quote_shares:
        return 0.0
    return ((v - spread_from_mid) / v) ** 2 * size


def _decide_quotes_rewards(
    cfg: MakerConfig,
    up_book: dict,
    down_book: dict,
    inv: Inventory,
    t_remaining: float,
) -> tuple[list[QuoteIntent], str]:
    """Quote for the reward score, not for the fill.

    Rests at `mid - reward_offset` on BOTH tokens. Two properties the ask-based
    quoting never had:

      * It scores. The reward is paid on resting size sampled once a minute,
        whether or not the order is ever hit, so time in the book is the
        product. The old objective's gates left us out of the book 69% of the
        time, which is 69% of the pool forfeited.
      * The pair is cheap by construction. mid_up + mid_down ~ 1.00, so bidding
        `offset` under mid on each side makes the pair cost ~1.00 - 2*offset.
        Quoting off the ASK did the opposite: ~half a spread ABOVE mid on each
        side, i.e. 1.00 + spread, which is why no sub-$1.00 pair ever appeared.

    Still deliberately NOT gated on the QUOTING WINDOW. That rule exists to
    protect fill quality by refusing to open late, and here a fill is a side
    effect: the score is paid on resting size every minute the order is there,
    so a cycle spent sitting out earns nothing and buys nothing.

    The PRICE BAND and the PAIR-COST cap used to be excused on the same
    reasoning, and that reasoning was wrong -- not because sitting out is
    cheap, but because neither rule protects fill quality alone. Outside the
    band the spread has collapsed on an outcome the market already considers
    settled, so the rent is small AND the naked leg a fill creates is decided
    against us. A pair over $1.00 is not a worse fill, it is a booked loss on
    an instrument that pays exactly $1.00. Both now run in `risk.hard_block`,
    on this path. They were unreachable rather than declined: both sit in the
    legacy branch of `decide_quotes`, below the line where this function
    returns, and the telemetry reads as absent rules would -- fills averaged
    0.8152 against a nominal 0.30-0.70 band, and wta-kalinsk-kessler bought 14
    pairs at $1.0200 against a $0.995 cap.
    """
    if t_remaining < cfg.min_t_remaining_sec:
        return [], f"t_remaining {t_remaining:.0f}s < {cfg.min_t_remaining_sec:.0f}s"

    # A market that kept picking us off AFTER we widened is not mispriced, it
    # is toxic. Giving up the rent is the point: rent is worth ~$50/day across
    # the fleet, and the exposure a bad market builds is worth multiples of it.
    if getattr(cfg, "gate_state", gate.NORMAL) == gate.EXITED:
        return [], "market exited: fills still lost money after widening"

    # PER-MARKET FILL CAP. This check has existed in `decide_quotes` since the
    # beginning and has never once run: the rewards objective returns from THIS
    # function, several lines before the caller reaches it. Three markets sat
    # at 26 fills against a nominal limit of 25.
    #
    # It belongs here rather than in the caller because "rewards" is the
    # objective the fleet actually runs -- a cap enforced only on the path
    # nobody takes is not a cap.
    if inv.fills >= cfg.max_fills_per_market:
        return [], f"hit {cfg.max_fills_per_market} fills for this market"

    # ZERO ALLOCATION MEANS QUOTE NOTHING. `reallocate` has always documented
    # that an unfunded market "gets 0 and stops quoting", and it never did:
    # `size = max(quote_shares, min_quote_shares)` below silently promoted a 0
    # back to the venue minimum, so a market the allocator had deliberately
    # defunded carried on posting 50-share orders.
    #
    # Harmless while every market was fundable. Not harmless once U4 defunds
    # markets that cannot clear the payout floor -- measured on the first
    # smoke run, 17 markets kept quoting while 4 were funded, putting $2,108
    # of offers against a $2,000 committed cap before a share was bought.
    if cfg.quote_shares <= 0:
        return [], "unfunded by the allocator -- quoting nothing"

    out: list[QuoteIntent] = []
    blocked: list[str] = []
    for side, book in (("UP", up_book), ("DOWN", down_book)):
        bb, ba = book.get("best_bid"), book.get("best_ask")
        mid = mid_price(bb, ba)
        if mid is None:
            blocked.append(f"{side}: no two-sided book")
            continue

        # INVENTORY SKEW. Push the heavy side away from mid and pull the light
        # side toward it, proportional to how lopsided we are. The light side
        # then fills first, which flattens the position using resting orders
        # only -- no crossing, no taker fee, and it happens from the first
        # share of imbalance rather than at 20s to close, when the only hedge
        # available is a 1c ticket on an outcome that has already happened.
        mine = inv.up_shares if side == "UP" else inv.down_shares
        theirs = inv.down_shares if side == "UP" else inv.up_shares
        imbalance = mine - theirs

        # EMERGENCY STOP-LOSS -- the one place this strategy takes liquidity.
        #
        # Skew and the hard cap between them assume the light side eventually
        # fills: skew pulls it toward mid, the cap stops the heavy side growing,
        # and the position flattens for free. That assumption holds in a market
        # that is merely lopsided. It fails in exactly the market that costs
        # money -- one moving hard against us -- because our light-side BID
        # sits under a mid that keeps walking away from it. Nobody comes down
        # to hit it, the heavy leg loses on every tick, and the cap has already
        # spent its authority: it froze us AT maximum exposure rather than
        # reducing it. Skew is a spring, and a spring holds a position; it does
        # not exit one.
        #
        # Two conditions, both required. Size alone is not an emergency: a
        # large, hedging-in-progress position in a flat market is doing no harm
        # and crossing it would pay the fee for nothing.
        #   1. The deficit on this side, VALUED AT THE HEAVY LEG'S AVERAGE
        #      COST, is within `emergency_hedge_frac` of the dollar cap --
        #      fired INSIDE the cap, while there is still a hedge left to buy.
        #      Dollars, not shares, for the same reason the cap itself is in
        #      dollars: 400 shares short is $80 of exposure at 0.20 and $340 at
        #      0.85, and a share-denominated trigger would sit inside the cap
        #      at one of those prices and outside it at the other.
        #   2. The heavy leg's mid has fallen below what we paid for it. That
        #      is the position losing money right now, measured, rather than
        #      inferred from how big it is.
        #
        # We deliberately do NOT cap the price at max_pair_cost here. A pair
        # bought over $1.00 is a bounded, known loss of a few cents per share;
        # an unhedged leg in a market running away from us is unbounded up to
        # the full $1.00. Refusing the expensive hedge to protect the pair-cost
        # rule would be choosing the larger loss to keep the smaller number
        # tidy.
        deficit = -imbalance
        heavy = "DOWN" if side == "UP" else "UP"
        # What the missing hedge is worth, priced at what the leg it would
        # cover actually cost us. A zero budget disables the rule, same escape
        # hatch as every other cap here -- without the guard a 0 budget would
        # put the trigger at $0 and cross on the first share of imbalance.
        deficit_usd = deficit * inv.avg(heavy) if deficit > 0 else 0.0
        if (cfg.enable_emergency_hedge and ba is not None and ba < 1.0
                and cfg.max_naked_usd > 0
                and deficit_usd >= cfg.max_naked_usd * cfg.emergency_hedge_frac):
            heavy_book = down_book if side == "UP" else up_book
            heavy_mid = mid_price(heavy_book.get("best_bid"),
                                  heavy_book.get("best_ask"))
            heavy_avg = inv.avg(heavy)
            if heavy_mid is not None and heavy_avg > 0 and heavy_mid < heavy_avg:
                # Take the whole deficit. A partial cross leaves us still
                # unhedged in the market we just decided is dangerous, and the
                # fill engine walks real ask depth -- if the size is not there,
                # the shortfall is a fact about the book, not a target we chose.
                # `price` is the touch; deeper levels get their real prices when
                # the caller crosses. `edge_vs_mid` is negative here on purpose:
                # we are paying above mid and the log should say so.
                out.append(QuoteIntent(
                    side=side, token_id=book.get("token_id"),
                    price=round(ba, 4), size=int(deficit), mid=mid,
                    edge_vs_mid=mid - ba, crossed=True,
                    reason=(f"EMERGENCY hedge: {deficit:.0f}sh short of "
                            f"{heavy} = ${deficit_usd:.0f} vs "
                            f"${cfg.max_naked_usd:.0f} cap, "
                            f"{heavy} mid {heavy_mid:.3f} under avg "
                            f"{heavy_avg:.3f} -- crossing at {ba:.3f}"),
                ))
                continue

        # A WIDENED market quotes further from mid on BOTH sides: fewer fills,
        # and the ones we still get are on better terms. It stays inside the
        # 4.5c reward window, so the rent keeps coming while we back off.
        base = gate.offset_for(getattr(cfg, "gate_state", gate.NORMAL),
                               cfg.reward_offset, cfg.widen_offset)
        # Provisional resting price, computed BEFORE the block so the block can
        # reason about it. The real price below is this one plus skew and the
        # price-risk widening, then clamped against the tick, the ask and the
        # book's best bid. Those move it by at most max_skew + 1.5c ~ 3c, which
        # cannot carry a price from inside the 0.30-0.70 band to outside it or
        # back, and computing the clamps for a quote we are about to refuse
        # would be work done for nothing.
        #
        # It is also the input to `band_risk_factor`, and it has to be: the
        # widening it returns is one of the terms that produces the final
        # price, so reading the final price here would be circular. Basing both
        # the band VERDICT and the risk RESPONSE on the same number is the
        # property that matters -- the two never disagree about where the quote
        # sits.
        provisional = round(mid - base, 4)

        # THE HARD BLOCK (strategy/risk.py). Five arms -- the hedge token's
        # health, this token's health, the dollar cap, the price band and the
        # pair-cost cap -- kept out of line here on purpose: this function
        # already carries six caps inline, and five more spelled out in place
        # would make the binding constraint unreadable. It replaces a share-denominated cap that measured the
        # wrong thing: 360 shares was $72 of risk at 0.20 and $293 at 0.8152,
        # so it never fired on lol-maz-mg1's $190.26 position.
        #
        # The light side is exempt inside `hard_block` -- it is the only
        # resting order that flattens us, so blocking it would freeze the
        # market at maximum exposure. That costs reward score when the heavy
        # side drops out (one-sided books score at 1/c, c=3.0) and bounds the
        # exposure that actually loses money.
        why = risk.hard_block(cfg, inv, side, provisional, book,
                              down_book if side == "UP" else up_book)
        if why:
            blocked.append(f"{side}: {why}")
            continue

        # FLEET-WIDE cap. The per-market rule above cannot see a book where
        # every market is individually fine and the total is not: 16 compliant
        # markets summed to $1,630 of exposure, carrying a +/-$456 swing
        # against a $62 edge. Being on the heavy side of ANY market adds to
        # that total, so over budget we stop adding everywhere at once. The
        # light side stays allowed on purpose -- it is the only order that
        # reduces the number we are over budget on.
        if (imbalance > 0
                and cfg.max_fleet_naked_usd > 0
                and cfg.fleet_naked_usd >= cfg.max_fleet_naked_usd):
            blocked.append(
                f"{side}: fleet ${cfg.fleet_naked_usd:.0f} unhedged >= "
                f"${cfg.max_fleet_naked_usd:.0f} budget -- not adding")
            continue

        # TOTAL COMMITTED CAPITAL. The cap above bounds only the unhedged leg,
        # on the reasoning that a matched pair always pays $1 and therefore
        # cannot lose. True, and beside the point: a pair still ties up money
        # that cannot be committed elsewhere. With only the naked cap running,
        # $9,588 left the wallet against a nominal $1,200 budget.
        #
        # Same asymmetry as the naked cap, for the same reason: the side that
        # would REDUCE the position keeps quoting even at the limit. Blocking
        # both sides here would freeze the fleet at maximum commitment with no
        # way down, since merge needs a matched pair and the light side is what
        # produces one. Being over the cap must never remove the only route
        # back under it.
        #
        # Named separately in the blocked list from the naked cap: an operator
        # reading "not adding" has to be able to tell which limit bound, or the
        # dashboard shows a dead market with no explanation.
        if (imbalance >= 0
                and cfg.max_committed_usd > 0
                and cfg.committed_usd >= cfg.max_committed_usd):
            blocked.append(
                f"{side}: fleet ${cfg.committed_usd:.0f} committed >= "
                f"${cfg.max_committed_usd:.0f} cap -- not adding")
            continue

        # THE FLEET CIRCUIT BREAKER (U6). Every rule above this line bounds a
        # QUANTITY -- dollars naked here, dollars naked fleet-wide, dollars
        # committed, and below, shares per order. Not one of them reads whether
        # the fills being bought are any good, so a fleet quoting compliant
        # sizes into uniformly toxic flow passes all of them. Measured
        # 2026-08-02 the POOLED markout read -4.75c/share while every market
        # individually sat at WIDENED, because none of them ever matured a
        # sample of its own.
        #
        # Placed AFTER the pair-cost arm of `hard_block` and BEFORE the size
        # ladder for the same reason the caps are ordered as they are: the
        # rules that describe THIS market's book report first, because they are
        # the more specific answer, and a market refused on its own terms
        # should not be blamed on the fleet. Before the ladder because the
        # ladder's job is to SIZE an order we have decided to place, and there
        # is no size to compute for one that is not going out.
        #
        # `imbalance > 0` is the whole of the exemption (R4/R10). The light
        # side, the merge and the emergency stop-loss all reduce exposure, and
        # a brake that also stopped them would freeze the fleet at maximum
        # exposure with no route down -- exactly the failure the old share cap
        # produced, where it held us AT the limit rather than under it. A FLAT
        # market has no heavy side and keeps quoting both.
        #
        # Reversible and un-persisted, unlike the EXITED check at the top of
        # this function: the posture is re-derived from the pool every sweep,
        # so this stops binding the moment the fleet's fills stop losing money.
        if imbalance > 0 and getattr(cfg, "fleet_posture",
                                     gate.NORMAL) == gate.HALTED:
            blocked.append(
                f"{side}: fleet HALTED on pooled markout -- no new naked "
                f"exposure until the fleet's fills stop losing money")
            continue

        # THE SPRING, wound by dollars (strategy/risk.py). It used to be wound
        # by imbalance against a fixed share ramp, which answered a 100-share naked
        # leg identically at 0.85 ($85 of downside) and at 0.15 ($15) -- so on
        # lol-maz-mg1 it was still ramping, 233 shares into a 240-share ramp,
        # while $190 was already at stake. Utilization of the naked side is the
        # same number the cap and the taper read, so all three now respond to
        # the same event.
        skew = risk.skew_offset(cfg, inv, side)

        # PRICE-DEPENDENT RISK (R6, strategy/risk.py). Everything above is
        # priced in dollars ALREADY HELD; nothing yet reads where in the 0..1
        # range the next fill would land. Two risks live there and they point
        # opposite ways -- variance peaks at the coin flip, magnitude rises all
        # the way to 1.00 -- so `band_risk_factor` answers the first with size
        # and the second with offset. Applied to BOTH sides, unlike the
        # exposure taper: this is a property of the PRICE, and on a binary
        # market both legs sit at prices summing to ~1.00, so treating them
        # symmetrically tilts nothing. The exposure taper is one-sided because
        # only one side reduces exposure; nothing here reduces anything.
        band = risk.band_risk_factor(cfg, provisional)
        desired = base + skew + band.extra_offset

        # Stay inside the reward window at the far end and off the touch at the
        # near end: a quote outside 4.5c scores nothing, which defeats the skew.
        offset = max(cfg.min_reward_offset,
                     min(cfg.max_spread_from_mid, desired))

        # KTD3. THE WINDOW IS A HARD BUDGET AND RISK AVERSION MUST NOT VANISH
        # INTO IT. Under WIDENED `base` is already 0.035, so 0.015 of skew plus
        # up to 1.5c of price-risk widening asks for more than the 4.5c window
        # allows -- and the clamp above would silently discard the excess in
        # exactly the state we entered BECAUSE fills were losing money. The
        # truncated fraction becomes a proportional size cut instead: if only
        # 90% of the requested distance fits, only 90% of the size rests.
        # Aversion always has somewhere to go. Only the FAR clamp counts; being
        # pushed out to `min_reward_offset` at the near end means resting
        # further from mid than asked for, which is not a truncation.
        truncated = 1.0
        if desired > offset > 0:
            truncated = offset / desired

        price = round(mid - offset, 4)
        # Land on a real venue price level (min tick 0.001), never above mid.
        price = round(round(price / cfg.price_tick) * cfg.price_tick, 4)
        if price <= 0.0 or price >= 1.0:
            blocked.append(f"{side}: price {price:.3f} off-scale")
            continue

        # Never cross -- with one exception, handled above. A bid at or above
        # the ask is a taker order, and taker fee here is 0.07*p*(1-p) --
        # 1.75c/share at p=0.50, larger than any edge in this book. That
        # arithmetic is about EARNING; it stops applying when the alternative
        # is not a smaller profit but an unbounded loss, which is the only case
        # the emergency stop-loss above lets through.
        if ba is not None and price >= ba:
            price = round(ba - cfg.price_tick, 4)

        # NEVER outbid the book by more than a tick. On a wide book, mid-minus-
        # offset can sit far above the best bid: measured on a market quoting
        # 0.26/0.42, the reward window (3.5c around a 0.34 mid) lies entirely
        # inside the spread, so landing in it means bidding 0.32 against a 0.26
        # best bid -- six cents better than anyone else, first in line to be
        # hit, and marked down six cents the moment we are. Those books are
        # empty inside the window precisely BECAUSE quoting there is unsafe;
        # the apparent 15%/day on such a market is payment for that risk, not
        # free money. Capping at best_bid + one tick keeps us competitive
        # without ever being the most exposed order in the book.
        if bb is not None:
            cap_price = round(bb + cfg.price_tick, 4)
            if price > cap_price:
                price = cap_price

        s = mid - price
        # The epsilon is representation error, not tolerance. The clamp above
        # sets the offset TO `max_spread_from_mid` in the cases that now reach
        # it, and `0.525 - 0.48` evaluates to 0.04500000000000004 in binary
        # floating point -- so without it a quote the clamp deliberately placed
        # ON the window edge is thrown out for being outside the window. That
        # is unreachable while the clamp never binds and routine once the U4
        # risk terms push against it, which is the whole of KTD3.
        if s > cfg.max_spread_from_mid + 1e-9:
            blocked.append(f"{side}: {100*s:.1f}c from mid > "
                           f"{100*cfg.max_spread_from_mid:.1f}c reward window")
            continue

        # Dropping the heavy side entirely used to live here. Skew replaces it:
        # pulling out of one side costs two thirds of the reward score (a
        # one-sided book scores at 1/c, c=3.0) and still leaves the position
        # lopsided, whereas skewing keeps us two-sided AND flattens. The only
        # hard stop left is the per-market cost cap.
        if inv.cost >= cfg.max_cost_per_market and mine >= theirs:
            blocked.append(f"{side}: cost cap ${cfg.max_cost_per_market:.0f}")
            continue

        # THE SIZE LADDER (strategy/risk.py). The dollar cap above is a step:
        # $119.99 of a $120 budget rested the full 120 shares and $120.00
        # rested none, so the largest order of a market's life arrived with a
        # cent of headroom left. `size_for` walks the size down as the budget
        # fills -- 16% of full size at 60% utilization -- and reaches zero AT
        # the budget. It reads the FINAL price, not the provisional one, because
        # its remaining-dollars arm is priced in the money an order actually
        # costs.
        ladder = risk.size_for(cfg, inv, side, price)
        # The two price-driven cuts, applied to what the ladder allowed. Both
        # are fractions of a size the exposure rules already approved, so they
        # can only ever make an order smaller -- neither can license one the
        # dollar budget refused.
        size = int(ladder * band.size_mult * truncated)
        if size < cfg.min_quote_shares:
            # Below rewardsMinSize (50) an order earns no score while still
            # buying inventory, so the honest answer is no order at all.
            size = 0
        if size <= 0:
            # No intent at all, never a zero-size order. Each rule names
            # ITSELF: at 80% of the budget the dollar cap has NOT fired, and an
            # operator reading the wrong reason goes looking for the wrong
            # limit. THREE terms cut this size -- the exposure ladder, the
            # price-risk multiplier and the KTD3 reward-window truncation --
            # so all three get their own arm. Reporting the truncation as
            # "price risk" named a rule that was not binding, and it also
            # classified wrong: `store.reason_code` matches "reward window"
            # but not "reward minimum", so those refusals were counted OTHER
            # instead of SPREAD.
            if ladder <= 0:
                util = risk.risk_utilization(cfg, inv, side)
                blocked.append(
                    f"{side}: size tapered to 0 at {100*util:.0f}% of the "
                    f"${cfg.max_naked_usd:.0f} naked budget")
            elif (truncated < 1.0
                    and int(ladder * band.size_mult) >= cfg.min_quote_shares):
                # The ladder and the price-risk cut together still cleared the
                # minimum; only the truncation took it under. That is the
                # 4.5c window refusing to carry the aversion the risk terms
                # asked for, which is a spread problem, not a price one.
                blocked.append(
                    f"{side}: reward window truncated the quote to "
                    f"{100*truncated:.0f}% of the distance asked for, cutting "
                    f"{ladder}sh under the {cfg.min_quote_shares}sh minimum")
            else:
                blocked.append(
                    f"{side}: price risk at {price:.3f} cut {ladder}sh to "
                    f"under the {cfg.min_quote_shares}sh reward minimum")
            continue
        out.append(QuoteIntent(
            side=side, token_id=book.get("token_id"), price=price, size=size,
            mid=mid, edge_vs_mid=mid - price,
            reason=(f"reward quote {100*s:.1f}c under mid {mid:.3f}, "
                    f"score {reward_score(cfg, s, size):.0f}"),
        ))

    if not out:
        return [], "; ".join(blocked) or "no side quotable"
    return out, ""


def _decide_quotes_spread(
    cfg: MakerConfig,
    up_book: dict,
    down_book: dict,
    inv: Inventory,
    t_remaining: float,
) -> tuple[list[QuoteIntent], str]:
    """Spread-capture quoting for SHORT-DATED markets (Plan 03, harden-to-reality).

    A market paying no rewards but trading real volume earns its income as
    bid/ask spread, not emissions. We rest just INSIDE the touch -- one tick
    above the best bid -- so a resting bid captures the spread on the next
    marketable sell. This is the inverse of the rewards path, which rests
    mid-minus-offset to earn the rebate window: here the edge is the spread
    itself, so we sit at the top of the book.

    Refuses a side when:
      * the spread is too thin (< `spread_capture_default_spread`) -- no edge;
      * the pair would cost >= `max_pair_cost` (still a $1.00 instrument);
      * the size ladder yields nothing (same dollar caps as the rewards path).
    """
    out: list[QuoteIntent] = []
    blocked: list[str] = []

    # A spread-capture book needs real width to be worth resting inside.
    min_spread = cfg.spread_capture_default_spread
    for side, book in (("UP", up_book), ("DOWN", down_book)):
        bb, ba = book.get("best_bid"), book.get("best_ask")
        if bb is None or ba is None:
            blocked.append(f"{side}: no book")
            continue
        spread = ba - bb
        if spread < min_spread:
            blocked.append(f"{side}: spread {spread:.3f} < {min_spread:.3f} min")
            continue
        # Rest one tick inside the touch: a maker bid at best_bid + tick.
        price = round(bb + cfg.tick_size, 4)
        if price >= ba:
            # Touch collapsed to one tick; nothing to capture.
            blocked.append(f"{side}: touch too thin to rest inside")
            continue
        mid = (bb + ba) / 2.0

        # Pair-cost cap still binds: the instrument pays exactly $1.00.
        other = "DOWN" if side == "UP" else "UP"
        other_book = down_book if side == "UP" else up_book
        other_avg = inv.avg(other)
        other_bb = (other_book.get("best_bid") or 0.0)
        # A held hedge averages into the pair; an open side uses the touch.
        hedge_cost = other_avg if other_avg > 0 else other_bb
        if hedge_cost > 0 and (price + hedge_cost) >= cfg.max_pair_cost:
            blocked.append(
                f"{side}: spread quote {price:.3f} + {other} {hedge_cost:.3f} "
                f"= {price + hedge_cost:.3f} >= ${cfg.max_pair_cost:.3f} cap")
            continue

        ladder = risk.size_for(cfg, inv, side, price)
        if ladder <= 0:
            blocked.append(f"{side}: size ladder returned 0")
            continue
        size = int(ladder)
        if size < cfg.min_quote_shares:
            blocked.append(f"{side}: size {size} < min {cfg.min_quote_shares}")
            continue

        out.append(QuoteIntent(
            side=side, token_id=book.get("token_id"), price=price, size=size,
            mid=mid, edge_vs_mid=mid - price,
            reason=(f"spread capture {side} inside touch {price:.3f} "
                    f"(spread {spread:.3f})"),
        ))

    if not out:
        return [], "; ".join(blocked) or "no side quotable"
    return out, ""


def decide_quotes(
    cfg: MakerConfig,
    up_book: dict,
    down_book: dict,
    inv: Inventory,
    t_remaining: float,
    window_frac: Optional[float] = None,
) -> tuple[list[QuoteIntent], str]:
    """Return the bids we want resting right now, plus a reason if we want none.

    `*_book` is {'best_bid','best_ask'} for that outcome's token.
    `window_frac` is how far into the trading window we are, 0..1. None means
    the caller could not work it out, in which case the timing rule is SKIPPED
    rather than guessed -- a missing clock must not silently gate every quote.
    """
    if cfg.objective == "rewards":
        return _decide_quotes_rewards(cfg, up_book, down_book, inv, t_remaining)
    if cfg.objective == "spread":
        return _decide_quotes_spread(cfg, up_book, down_book, inv, t_remaining)

    if t_remaining < cfg.min_t_remaining_sec:
        return [], f"t_remaining {t_remaining:.0f}s < {cfg.min_t_remaining_sec:.0f}s"

    # powerwinner posts 57% of his entries in the FIRST 40% of the window: a
    # passive order needs time to be reached, and quoting late means resting
    # into the convergence, when a fill is most likely to be the wrong side of
    # a move.
    if cfg.enforce_quote_window and window_frac is not None \
            and window_frac > cfg.quote_window_frac:
        return [], (f"{100*window_frac:.0f}% into window > "
                    f"{100*cfg.quote_window_frac:.0f}% quoting window")
    if inv.fills >= cfg.max_fills_per_market:
        return [], f"hit {cfg.max_fills_per_market} fills for this market"

    # At the cost cap we may still buy the LIGHTER side. Measured over 44
    # settled markets: perfectly hedged markets averaged +$30.70 while badly
    # unbalanced ones averaged -$50.95 (hedged +$409 total vs unbalanced -$848).
    # The old rule stopped ALL quoting at the cap, which froze whatever
    # imbalance we happened to hold -- the single biggest loss driver. Buying
    # the light side REDUCES exposure, so the cap must not block it.
    balancing_only = inv.cost >= cfg.max_cost_per_market
    if balancing_only and inv.balance >= cfg.target_balance:
        return [], f"cost cap ${cfg.max_cost_per_market:.0f} reached and balanced"

    # Fresh market: only open a position if BOTH sides can be filled at a pair
    # that pays under $1.00 at ask-1tick. A lone directional leg is an unhedged
    # bet we never asked for -- sit out wide markets instead of taking it.
    if inv.fills == 0:
        p_up = p_dn = None
        blocked: list[str] = []
        for side, book in (("UP", up_book), ("DOWN", down_book)):
            bb, ba = book.get("best_bid"), book.get("best_ask")
            mid = mid_price(bb, ba)
            if mid is None or ba is None:
                blocked.append(f"{side}: no two-sided book")
                continue
            p = round(ba - cfg.ticks_below_ask * cfg.tick_size, 4)
            if p <= 0.0 or p >= 1.0:
                blocked.append(f"{side}: price {p:.2f} off-scale")
                continue
            # Name the ACTUAL blocking filter. An earlier version reported every
            # one-sided market as a price-band rejection, which sent the skip
            # log chasing the wrong rule -- turning the band off changed nothing,
            # because it was almost never the binding constraint.
            if abs(mid - p) > cfg.max_spread_from_mid:
                blocked.append(f"{side}: {100*abs(mid-p):.1f}c from mid > "
                               f"{100*cfg.max_spread_from_mid:.1f}c rebate window")
                continue
            if not _in_band(cfg, p):
                blocked.append(f"{side}: {p:.2f} outside band "
                               f"{cfg.price_band_low:.2f}-{cfg.price_band_high:.2f}")
                continue
            if side == "UP":
                p_up = p
            else:
                p_dn = p
        if p_up is not None and p_dn is not None:
            if (p_up + p_dn) >= cfg.max_pair_cost:
                return [], "no fillable sub-$1.00 pair at touch -- sitting out"
        else:
            return [], ("only one side quotable at touch -- no pair to hedge; "
                        + "; ".join(blocked))

    out: list[QuoteIntent] = []
    for side, book, tok in (
        ("UP", up_book, up_book.get("token_id")),
        ("DOWN", down_book, down_book.get("token_id")),
    ):
        bb, ba = book.get("best_bid"), book.get("best_ask")
        mid = mid_price(bb, ba)
        if mid is None or ba is None:
            continue

        # Rest one tick inside the ask -- passive, never crossing.
        price = round(ba - cfg.ticks_below_ask * cfg.tick_size, 4)
        if price <= 0.0 or price >= 1.0:
            continue

        # Rebate window: must be within 4.5c of mid, else no rebate and the
        # whole point of being a maker is gone.
        if abs(mid - price) > cfg.max_spread_from_mid:
            continue

        # Price band. Outside 0.30-0.70 the spread narrows toward one tick on
        # an outcome the market already considers settled, so there is little
        # to capture -- while the loss if it goes the other way is still the
        # full $1.00. powerwinner has zero trades at 0.98+.
        if not _in_band(cfg, price):
            continue

        # Uniform pair-cost cap. The earlier hedge exemption let the balancing
        # side bypass this cap, which produced pairs > $1.00 -- a guaranteed
        # loss on a payout that is exactly $1.00. The cap now applies to EVERY
        # side, hedge or not: we never build a pair that costs more than it pays.
        # Combined with the fresh-market guard above, the bot simply sits out
        # markets where no fillable sub-$1.00 pair exists.
        other = "DOWN" if side == "UP" else "UP"
        other_avg = inv.avg(other)
        if other_avg > 0 and (price + other_avg) >= cfg.max_pair_cost:
            store.log_decision(
                market_slug=m.market_slug, condition_id=m.condition_id,
                action="SKIP_PAIR_COST", side=side, price=price, mid=mid,
                edge_vs_mid=mid - price, t_remaining=None,
                balance=inv.balance, pair_cost=inv.pair_cost(),
                reason=(f"{side} quote {price:.3f} + {other} avg "
                        f"{other_avg:.3f} = {price+other_avg:.3f} >= "
                        f"${cfg.max_pair_cost:.3f} cap -- sits out"),
                reason_code="PAIR_COST")
            continue

        # Inventory control: if we're already heavy on this side, only quote
        # the lighter one until balance recovers.
        mine = inv.up_shares if side == "UP" else inv.down_shares
        theirs = inv.down_shares if side == "UP" else inv.up_shares
        if (inv.up_shares > 0 or inv.down_shares > 0):
            if mine > theirs and inv.balance < cfg.target_balance:
                continue
        # Past the cost cap we ONLY add to the light side, never the heavy one.
        if balancing_only and mine >= theirs:
            continue

        out.append(QuoteIntent(
            side=side, token_id=tok, price=price, size=cfg.quote_shares,
            mid=mid, edge_vs_mid=mid - price,
            reason=f"rest {cfg.ticks_below_ask} tick under ask {ba:.2f}",
        ))

    if not out:
        return [], "no side passed the quote filters"
    return out, ""
