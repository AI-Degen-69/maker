"""Where to rest bids. The decision layer of the Hunter sim.

Mirrors powerwinner's measured behaviour: quote BOTH outcomes, stay ~92%
balanced, never let the pair cost reach $1.00 (it pays exactly $1.00, so a pair
bought at >= 1.00 is a guaranteed loss), and keep quotes inside the rebate
window (>= 50 shares, within 4.5c of mid).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from engine import config, gate, risk
from engine.config import MakerConfig


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


def quote_resting_price(
    cfg: MakerConfig, inv: Inventory, side: str, book: dict
) -> tuple[float | None, float | None, risk.BandRisk | None, float]:
    """Calculate the resting price, provisional price, band risk, and truncation factor for a side."""
    bb, ba = book.get("best_bid"), book.get("best_ask")
    mid = mid_price(bb, ba)
    if mid is None:
        return None, None, None, 1.0
    base = gate.offset_for(getattr(cfg, "gate_state", gate.NORMAL),
                           cfg.reward_offset, cfg.widen_offset)
    provisional = round(mid - base, 4)
    skew = risk.skew_offset(cfg, inv, side)
    band = risk.band_risk_factor(cfg, provisional)
    desired = base + skew + band.extra_offset
    offset = max(cfg.min_reward_offset, min(cfg.max_spread_from_mid, desired))
    truncated = 1.0
    if desired > offset > 0:
        truncated = offset / desired
    price = round(mid - offset, 4)
    price = round(round(price / cfg.price_tick) * cfg.price_tick, 4)
    if ba is not None and price >= ba:
        price = round(ba - cfg.price_tick, 4)
    if bb is not None:
        cap_price = round(bb + cfg.price_tick, 4)
        if price > cap_price:
            price = cap_price
    return price, provisional, band, truncated


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

    p_up_calc, _, _, _ = quote_resting_price(cfg, inv, "UP", up_book)
    p_dn_calc, _, _, _ = quote_resting_price(cfg, inv, "DOWN", down_book)
    calc_pair_price = None
    if p_up_calc is not None and p_dn_calc is not None and p_up_calc > 0 and p_dn_calc > 0:
        calc_pair_price = round(p_up_calc + p_dn_calc, 4)

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
        deficit = -imbalance
        heavy = "DOWN" if side == "UP" else "UP"
        deficit_usd = deficit * inv.avg(heavy) if deficit > 0 else 0.0
        if (cfg.enable_emergency_hedge and ba is not None and ba < 1.0
                and cfg.max_naked_usd > 0
                and deficit_usd >= cfg.max_naked_usd * cfg.emergency_hedge_frac):
            heavy_book = down_book if side == "UP" else up_book
            heavy_mid = mid_price(heavy_book.get("best_bid"),
                                  heavy_book.get("best_ask"))
            heavy_avg = inv.avg(heavy)
            if heavy_mid is not None and heavy_avg > 0 and heavy_mid < heavy_avg:
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

        price, provisional, band, truncated = quote_resting_price(cfg, inv, side, book)
        if price is None or provisional is None or band is None:
            blocked.append(f"{side}: price calculation failed")
            continue

        # THE HARD BLOCK (strategy/risk.py).
        why = risk.hard_block(cfg, inv, side, provisional, book,
                              down_book if side == "UP" else up_book)
        if why:
            blocked.append(f"{side}: {why}")
            continue

        # FLEET-WIDE cap.
        if (imbalance > 0
                and cfg.max_fleet_naked_usd > 0
                and cfg.fleet_naked_usd >= cfg.max_fleet_naked_usd):
            blocked.append(
                f"{side}: fleet ${cfg.fleet_naked_usd:.0f} unhedged >= "
                f"${cfg.max_fleet_naked_usd:.0f} budget -- not adding")
            continue

        # TOTAL COMMITTED CAPITAL.
        if (imbalance >= 0
                and cfg.max_committed_usd > 0
                and cfg.committed_usd >= cfg.max_committed_usd):
            blocked.append(
                f"{side}: fleet ${cfg.committed_usd:.0f} committed >= "
                f"${cfg.max_committed_usd:.0f} cap -- not adding")
            continue

        # THE FLEET CIRCUIT BREAKER (U6).
        if imbalance > 0 and getattr(cfg, "fleet_posture",
                                     gate.NORMAL) == gate.HALTED:
            blocked.append(
                f"{side}: fleet HALTED on pooled markout -- no new naked "
                f"exposure until the fleet's fills stop losing money")
            continue

        if price <= 0.0 or price >= 1.0:
            blocked.append(f"{side}: price {price:.3f} off-scale")
            continue

        s = mid - price
        if s > cfg.max_spread_from_mid + 1e-9:
            blocked.append(f"{side}: {100*s:.1f}c from mid > "
                           f"{100*cfg.max_spread_from_mid:.1f}c reward window")
            continue

        if inv.cost >= cfg.max_cost_per_market and mine >= theirs:
            blocked.append(f"{side}: cost cap ${cfg.max_cost_per_market:.0f}")
            continue

        # THE SIZE LADDER (strategy/risk.py). Sized in couple shares.
        ladder = risk.size_for(cfg, inv, side, price, pair_price=calc_pair_price)
        size = int(ladder * band.size_mult * truncated)
        waived = ""
        if size < cfg.min_quote_shares:
            if (cfg.waive_attenuation_below_floor
                    and getattr(cfg, "size_mode", "shares") == "dollars"
                    and ladder >= cfg.min_quote_shares
                    and truncated >= 1.0):
                size = cfg.min_quote_shares
                waived = (f", price-risk taper x{band.size_mult:.2f} waived: "
                          f"{int(ladder * band.size_mult)}sh is under the "
                          f"{cfg.min_quote_shares}sh venue floor")
            else:
                size = 0
        if size <= 0:
            if ladder <= 0:
                couple_alloc = (config.couple_allocation_usd(cfg)
                                if getattr(cfg, "size_mode", "shares") == "dollars"
                                else 0.0)
                eff_pair = calc_pair_price if (calc_pair_price is not None and calc_pair_price > 0) else (2.0 * price if price > 0 else 1.0)
                affordable = int(couple_alloc / eff_pair) if (couple_alloc and eff_pair > 0) else None
                if affordable is not None and affordable < cfg.min_quote_shares:
                    needed = cfg.min_quote_shares * eff_pair
                    blocked.append(
                        f"{side}: ${couple_alloc:.2f} couple allocation at pair price "
                        f"{eff_pair:.3f} buys {affordable}sh, below the venue "
                        f"minimum of {cfg.min_quote_shares}sh (needs "
                        f"${needed:.2f}, shortfall ${needed - couple_alloc:.2f})")
                else:
                    util = risk.risk_utilization(cfg, inv, side)
                    blocked.append(
                        f"{side}: size tapered to 0 at {100*util:.0f}% of the "
                        f"${cfg.max_naked_usd:.0f} naked budget")
            elif (truncated < 1.0
                    and int(ladder * band.size_mult) >= cfg.min_quote_shares):
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
                    f"score {reward_score(cfg, s, size):.0f}{waived}"),
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
        intents, why = _decide_quotes_rewards(cfg, up_book, down_book, inv,
                                              t_remaining)
        return _require_two_sided(cfg, inv, intents, why)

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


def _require_two_sided(cfg, inv, intents, why):
    """A flat book quotes a couple or nothing at all.

    One resting leg against no inventory is a naked position by construction:
    if it fills, the hedge does not exist yet and the only way to acquire it is
    to cross, paying away the spread the quote was resting to earn. The pair is
    the product; half a pair is a directional bet nobody decided to take.

    Unbalanced inventory is the opposite case: the lone intent is the LIGHT
    side, it reduces exposure, and refusing it would hold the position at its
    widest. That is only true when the lone intent really is the light side.
    A lone intent on the HEAVY side deepens the imbalance instead of closing
    it -- a directional bet on the side we already over-hold -- and is refused
    exactly like the flat case. (The phantom-inventory bug made markets look
    unbalanced when they were flat; that is fixed at the registry, and this
    guard is the second line that refuses a heavy-side lone leg regardless.)
    """
    if not getattr(cfg, "require_two_sided_when_flat", False):
        return intents, why
    heavy = risk.naked_side(inv)
    if heavy is not None:
        if len(intents) == 1 and intents[0].side == heavy:
            lone = intents[0].side
            return [], (f"inventory is {heavy}-heavy and only {lone} is "
                        f"quotable; a lone {lone} leg deepens the imbalance, "
                        f"so the couple is not placed"
                        + (f" -- {why}" if why else ""))
        return intents, why
    if len(intents) >= 2:
        return intents, why
    if not intents:
        return intents, why
    lone = intents[0].side
    missing = "DOWN" if lone == "UP" else "UP"
    return [], (f"flat inventory and only {lone} is quotable ({missing} "
                f"blocked); a lone resting leg is a naked position, so the "
                f"couple is not placed" + (f" -- {why}" if why else ""))


class MarketUnavailable(Exception):
    """The venue has no tradeable market for the condition id."""


class MarketQuoteError(Exception):
    """A venue read (book fetch) failed while evaluating one market's quote."""


@dataclass
class MarketEval:
    """The outcome of one quote-one-market evaluation, before any planning.

    Raw data only -- the caller decides what to print, log, or submit.
    `intents` and `why` are whatever the `decide` port returned.
    """
    cid: str
    market: Any
    up_book: dict
    down_book: dict
    inventory: Inventory
    intents: list
    why: str


def evaluate_market_quote(
    cid: str,
    cfg: MakerConfig,
    clob_host: str,
    *,
    fetch_market: Callable[[str], Any],
    fetch_books: Callable[[str, str], dict],
    inventory_for: Callable[[Any], Inventory],
    decide: Callable[..., tuple] = decide_quotes,
) -> MarketEval:
    """One market through the quoting pipeline: fetch -> books -> inventory -> decide.

    The sequence live_exec.decide and the fleet's per-market visit used to run
    in two copies; this is the single copy. Both callers are adapters over the
    four ports: the CLI wires engine.markets + inventory_from_registry +
    decide_quotes, the fleet wires its VenueSeam slots. The step is pure of
    venue imports -- it owns failure *detection* (MarketUnavailable when the
    market is missing, MarketQuoteError when a book read fails) and the caller
    owns failure *presentation*.
    """
    market = fetch_market(cid)
    if market is None:
        raise MarketUnavailable(cid)
    try:
        up_book = fetch_books(clob_host, market.up_token)
        down_book = fetch_books(clob_host, market.down_token)
    except Exception as e:
        raise MarketQuoteError(f"book fetch error: {e}") from e
    inv = inventory_for(market)
    intents, why = decide(cfg, up_book, down_book, inv, 1e9, None)
    return MarketEval(
        cid=cid, market=market, up_book=up_book, down_book=down_book,
        inventory=inv, intents=intents, why=why,
    )
