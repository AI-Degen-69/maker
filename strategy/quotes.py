"""Where to rest bids. The decision layer of the maker sim.

Mirrors powerwinner's measured behaviour: quote BOTH outcomes, stay ~92%
balanced, never let the pair cost reach $1.00 (it pays exactly $1.00, so a pair
bought at >= 1.00 is a guaranteed loss), and keep quotes inside the rebate
window (>= 50 shares, within 4.5c of mid).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from strategy import gate
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

    Deliberately NOT gated on the price band, the quoting window, or the pair
    cost. Those rules exist to protect fill quality; here a fill is a side
    effect, and every cycle spent sitting out earns nothing.
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
        #   1. The deficit on this side is within `emergency_hedge_frac` of the
        #      hard cap -- fired INSIDE the cap, while there is still a hedge
        #      left to buy.
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
        if (cfg.enable_emergency_hedge and ba is not None and ba < 1.0
                and deficit >= cfg.max_naked_shares * cfg.emergency_hedge_frac):
            heavy = "DOWN" if side == "UP" else "UP"
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
                            f"{heavy} vs {cfg.max_naked_shares:.0f} cap, "
                            f"{heavy} mid {heavy_mid:.3f} under avg "
                            f"{heavy_avg:.3f} -- crossing at {ba:.3f}"),
                ))
                continue

        # HARD CAP. The skew below is a spring and it bottoms out at
        # skew_full_shares; past max_naked_shares it has no authority left, so
        # stop adding to the heavy side entirely. The light side keeps quoting
        # -- it is the only order that flattens us -- so this costs reward
        # score (one-sided books score at 1/c, c=3.0) but bounds the exposure
        # that actually loses money. Without it, measured live, one market ran
        # to 681 shares unhedged and fleet exposure hit $1333 against $47/day
        # of rent.
        if imbalance >= cfg.max_naked_shares:
            blocked.append(f"{side}: {imbalance:.0f}sh unhedged >= "
                           f"{cfg.max_naked_shares:.0f} cap -- not adding")
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
        skew = cfg.max_skew * max(-1.0, min(1.0, imbalance / cfg.skew_full_shares))
        # A WIDENED market quotes further from mid on BOTH sides: fewer fills,
        # and the ones we still get are on better terms. It stays inside the
        # 4.5c reward window, so the rent keeps coming while we back off.
        base = gate.offset_for(getattr(cfg, "gate_state", gate.NORMAL),
                               cfg.reward_offset, cfg.widen_offset)
        offset = base + skew
        # Stay inside the reward window at the far end and off the touch at the
        # near end: a quote outside 4.5c scores nothing, which defeats the skew.
        offset = max(cfg.min_reward_offset, min(cfg.max_spread_from_mid, offset))

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
        if s > cfg.max_spread_from_mid:
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

        size = max(cfg.quote_shares, cfg.min_quote_shares)
        out.append(QuoteIntent(
            side=side, token_id=book.get("token_id"), price=price, size=size,
            mid=mid, edge_vs_mid=mid - price,
            reason=(f"reward quote {100*s:.1f}c under mid {mid:.3f}, "
                    f"score {reward_score(cfg, s, size):.0f}"),
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
