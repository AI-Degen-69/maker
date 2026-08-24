"""Tests for the two powerwinner entry rules added to decide_quotes.

Both are switchable, and each is tested with the OTHER one off, so a passing
test can only be explained by the rule it names.
"""
import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategy import gate, risk                       # noqa: E402
from strategy.config import load as load_cfg          # noqa: E402
from strategy.quotes import Inventory, decide_quotes  # noqa: E402

BASE = load_cfg()


def _cfg(**kw):
    """Config for the tests below, which all describe the "pair" objective.

    The band, the quoting window and the pair-cost cap are rules of that
    objective only. The default objective is now "rewards", which bypasses all
    three deliberately -- it is paid on resting size rather than on fill
    quality, so sitting out to protect a fill is what it must NOT do. Pinning
    the objective here keeps these tests honest about which rule set they
    cover; pass objective="rewards" explicitly to test the other one.
    """
    kw.setdefault("objective", "pair")
    return dataclasses.replace(BASE, **kw)


def _book(token, bid, ask):
    return {"token_id": token, "best_bid": bid, "best_ask": ask,
            "bids": {bid: 500.0}, "asks": {ask: 500.0}}


def _quote(cfg, up=(0.52, 0.53), dn=(0.46, 0.47), t_rem=200.0, frac=None,
           inv=None):
    return decide_quotes(cfg, _book("UPTOK", *up), _book("DNTOK", *dn),
                         inv or Inventory(), t_rem, frac)


# --- price band -------------------------------------------------------------

def test_mid_priced_market_is_quoted_when_only_the_band_is_on():
    intents, why = _quote(_cfg(enforce_quote_window=False))
    assert {q.side for q in intents} == {"UP", "DOWN"}, why


def test_near_certain_market_is_refused_by_the_band():
    """0.95/0.05 is outside 0.30-0.70: no spread to capture, full downside."""
    cfg = _cfg(enforce_quote_window=False)
    intents, why = _quote(cfg, up=(0.95, 0.96), dn=(0.03, 0.04))
    assert intents == []
    assert "outside band" in why


def test_turning_the_band_off_lets_the_same_market_through():
    """Isolates the band as the cause -- nothing else about the input moved."""
    cfg = _cfg(enforce_quote_window=False, enforce_price_band=False)
    intents, _ = _quote(cfg, up=(0.95, 0.96), dn=(0.03, 0.04))
    assert intents != []


# --- quoting window ---------------------------------------------------------

def test_quotes_early_in_the_window():
    intents, why = _quote(_cfg(enforce_price_band=False), frac=0.10)
    assert intents != [], why


def test_refuses_to_open_late_in_the_window():
    cfg = _cfg(enforce_price_band=False)
    intents, why = _quote(cfg, frac=0.80)
    assert intents == []
    assert "window" in why


def test_missing_window_clock_does_not_gate_quoting():
    """frac=None means 'unknown', and an unknown clock must not block every
    quote -- that would silently stop the bot rather than fail loudly."""
    intents, why = _quote(_cfg(enforce_price_band=False), frac=None)
    assert intents != [], why


def test_window_rule_off_allows_late_quotes():
    cfg = _cfg(enforce_price_band=False, enforce_quote_window=False)
    intents, _ = _quote(cfg, frac=0.80)
    assert intents != []


# --- the rules do not disturb the existing pair-cost guard ------------------

def test_pair_over_one_dollar_still_refused_inside_the_band():
    """Both legs in-band but the pair costs >= $1.00 on a $1.00 payout."""
    cfg = _cfg(enforce_quote_window=False)
    intents, why = _quote(cfg, up=(0.55, 0.56), dn=(0.45, 0.46))
    assert intents == []
    assert "sub-$1.00" in why


# --- rewards objective ------------------------------------------------------
#
# Measured on 2216 recorded book snapshots: the "pair" objective rested in the
# book on 31% of cycles and never once assembled a sub-$1.00 pair (median pair
# 1.010), because resting under the ASK puts each quote half a spread ABOVE
# mid. Quoting off MID instead reaches 86% in-book with a 0.960 median pair.
# These tests pin that behaviour.

def _rcfg(**kw):
    kw.setdefault("objective", "rewards")
    return dataclasses.replace(BASE, **kw)


def _band_extra(cfg, q, base=None):
    """The U4 widening this quote should be carrying, from its own price.

    The band factor reads the PROVISIONAL price (mid - base), because the
    widening it returns is one of the terms that produces the final price and
    reading that back would be circular.
    """
    base = cfg.reward_offset if base is None else base
    return risk.band_risk_factor(cfg, round(q.mid - base, 4)).extra_offset


def test_rewards_quotes_both_sides_under_mid():
    cfg = _rcfg(reward_offset=0.02)
    intents, why = _quote(cfg)
    assert {q.side for q in intents} == {"UP", "DOWN"}, why
    for q in intents:
        assert q.price < q.mid, "a reward quote must rest UNDER mid, never above"
        # `reward_offset` is no longer the whole offset: U4 adds a widening
        # that is a function of where the quote sits, so this book rests 3.5c
        # under mid on UP (at 0.505) and 3.2c on DOWN (at 0.445). Asserting a
        # flat 2.0c on both would now be asserting that R6 does not work. The
        # venue's 0.001 tick is the only slack allowed.
        s = q.mid - q.price
        assert s > 0.02, "the risk terms only ever widen"
        assert abs(s - (0.02 + _band_extra(cfg, q))) <= cfg.price_tick


def test_rewards_pair_is_under_one_dollar_by_construction():
    """The property the pair objective spent 60 markets failing to reach.

    mid_up + mid_down ~ 1.00, so bidding `offset` under mid on both sides costs
    ~1.00 - 2*offset. Nothing has to line up for this; it is arithmetic.
    """
    intents, _ = _quote(_rcfg(reward_offset=0.02))
    pair = sum(q.price for q in intents)
    mids = sum(q.mid for q in intents)
    assert pair < 1.0
    # At LEAST 2*offset under the sum of the mids, whatever that sum happens to
    # be. Asserting against `mids` states the mechanism rather than the fixture:
    # on a real book the two mids sum to ~1.00, so the pair lands near 0.96.
    # It is now strictly cheaper than that -- U4's price-risk widening only
    # ever moves a bid further under mid, so it can only make the pair cheaper.
    assert pair < mids - 2 * 0.02


def test_rewards_does_not_sit_out_a_wide_or_late_market():
    """The gates that cost 69% of cycles must not apply to this objective."""
    # Pair over $1.00 at the touch, and 90% into the window: the pair objective
    # refuses both. Rewards are paid on resting size, so refusing earns zero.
    intents, why = _quote(_rcfg(), up=(0.55, 0.56), dn=(0.45, 0.46), frac=0.9)
    assert {q.side for q in intents} == {"UP", "DOWN"}, why


def test_rewards_never_crosses_the_spread():
    """A bid at/above the ask is a taker order: fee 0.07*p*(1-p) dwarfs the edge."""
    intents, _ = _quote(_rcfg(reward_offset=-0.05), up=(0.52, 0.53), dn=(0.46, 0.47))
    for q in intents:
        ask = 0.53 if q.side == "UP" else 0.47
        assert q.price < ask


def test_reward_score_is_quadratic_and_zero_outside_the_window():
    from strategy.quotes import reward_score
    cfg = _rcfg()
    v = cfg.max_spread_from_mid
    assert reward_score(cfg, 0.0, 120) == 120                  # at mid, full
    assert reward_score(cfg, v, 120) == 0                      # at the edge
    assert reward_score(cfg, v + 0.001, 120) == 0              # outside
    assert reward_score(cfg, 0.02, 10) == 0                    # under min size
    # Quadratic: half the max spread keeps a quarter of the score.
    assert abs(reward_score(cfg, v / 2, 120) - 0.25 * 120) < 1e-9


def test_rewards_skews_to_flatten_instead_of_dropping_a_side():
    """Long UP -> UP moves away from mid, DOWN moves toward it, both stay up.

    Replaces an earlier rule that stopped quoting the heavy side outright.
    That forfeited two thirds of the score (one-sided books score at 1/c,
    c=3.0) and still left the position lopsided. Skew keeps both sides
    resting, so the score is preserved AND the light side fills first.
    """
    # 60 UP at a 0.52 average is $31.20 of naked cost, 26% of the $120 budget:
    # this test is about the skew, so neither the dollar cap NOR the U3 size
    # taper (which floors a side to zero above ~35% utilization, at a 120-share
    # base) may be what decides the outcome. Both sides must still rest.
    #
    # Quoted off-center (0.65/0.32 mids) so U4's coinflip cut, strengthened to
    # 55% on 2026-08-06, does not ALSO floor the heavy side here -- that is a
    # separate concern this test does not own.
    inv = Inventory(up_shares=60.0, down_shares=0.0, up_cost=31.20, down_cost=0.0)
    intents, why = _quote(_rcfg(), up=(0.65, 0.66), dn=(0.32, 0.33), inv=inv)
    assert {q.side for q in intents} == {"UP", "DOWN"}, why
    off = {q.side: q.mid - q.price for q in intents}
    assert off["UP"] > off["DOWN"], "heavy side must sit FURTHER from mid"
    # The light side is pulled TOWARD mid, so it is the one that fills next.
    # Measured against the same book held flat rather than against a bare
    # constant: every quote now also carries U4's price-risk widening, and a
    # fixed threshold would be reading that term rather than the skew.
    flat, _ = _quote(_rcfg(), up=(0.65, 0.66), dn=(0.32, 0.33), inv=Inventory())
    flat_down = [q.mid - q.price for q in flat if q.side == "DOWN"][0]
    assert off["DOWN"] < flat_down


def test_skew_is_symmetric_and_flat_when_balanced():
    """Balanced inventory bought at 0.375 a side, not 0.50.

    A 0.50/0.50 book was the old fixture and it is no longer a legal position
    to add to: the UP bid would land at 0.505 against a 0.50 DOWN average, a
    $1.005 pair on a $1.00 payout, which U4's pair cap now refuses outright.
    The fixture was quietly describing the loss the cap exists to prevent.
    """
    inv = Inventory(up_shares=120.0, down_shares=120.0, up_cost=45.0,
                    down_cost=45.0)
    cfg = _rcfg()
    assert risk.skew_offset(cfg, inv, "UP") == 0.0, "balanced: no spring"
    assert risk.skew_offset(cfg, inv, "DOWN") == 0.0
    intents, why = _quote(cfg, inv=inv)
    assert {q.side for q in intents} == {"UP", "DOWN"}, why
    off = {q.side: q.mid - q.price for q in intents}
    # What asymmetry is left is NOT skew -- it is R6. UP rests at 0.505 and
    # DOWN at 0.445, so a UP share carries 6c more downside than a DOWN share
    # and is quoted further out for it.
    assert off["UP"] > off["DOWN"]


def test_never_outbids_the_book_by_more_than_a_tick():
    """On a wide book, mid-minus-offset lands far above the best bid.

    Measured live: a market quoting 0.26/0.42 has its whole 3.5c reward window
    inside the spread, so an uncapped quote bids 0.32 against a 0.26 best bid.
    That is the most exposed order in the book by six cents -- which is exactly
    why the window was empty. Rewards do not compensate for being picked off
    six cents wide.
    """
    cfg = _rcfg(price_tick=0.01)
    intents, _ = _quote(cfg, up=(0.26, 0.42), dn=(0.58, 0.74))
    for q in intents:
        best_bid = 0.26 if q.side == "UP" else 0.58
        assert q.price <= round(best_bid + cfg.price_tick, 4) + 1e-9, (
            f"{q.side} bid {q.price} outbids the book's {best_bid} by over a tick")


def test_tight_book_still_quotes_at_the_intended_offset():
    """The best-bid cap must not bite on a normal, tight book.

    'Intended' is `reward_offset` plus U4's price-risk widening and nothing
    else: no skew (flat), no clamp (inside the window), no best-bid cap. If the
    cap were biting, the offset would be some function of the book's 0.52/0.46
    bids instead, which is what this pins.
    """
    cfg = _rcfg()
    intents, why = _quote(cfg, up=(0.52, 0.53), dn=(0.46, 0.47))
    assert {q.side for q in intents} == {"UP", "DOWN"}, why
    for q in intents:
        want = cfg.reward_offset + _band_extra(cfg, q)
        assert abs((q.mid - q.price) - want) <= cfg.price_tick


# --- U2: the hard cap, in dollars -------------------------------------------
#
# The cap these cover used to be stated in SHARES, and the unit was the defect.
# On a binary market the downside of one long share is the price paid for it,
# so 360 shares permitted $72 of risk at 0.20 and $293 at 0.8152 -- loosest
# exactly where a wrong resolution costs most. Measured 2026-08-05 on
# lol-maz-mg1: 233.40 UP shares, $190.26 at risk, and the share cap read 233
# against 360 and stayed silent while 85% of the fleet's -$223.32 unhedged
# float sat in that one market.

def test_the_dollar_cap_stops_the_heavy_side_where_the_share_cap_did_not():
    """150 UP at a 0.82 average is $123 of naked cost against a $120 budget.

    The share cap this replaces would have read 150 against 360 and permitted
    the add -- the same blindness that let lol-maz-mg1 build to $190.26 with
    three limits armed and none binding.
    """
    cfg = _rcfg(max_naked_usd=120.0)
    inv = Inventory(up_shares=150.0, down_shares=0.0,
                    up_cost=123.0, down_cost=0.0)
    intents, why = _quote(cfg, up=(0.82, 0.83), dn=(0.17, 0.18), inv=inv)
    sides = {q.side for q in intents}
    assert "UP" not in sides, f"heavy side must stop adding exposure: {why}"
    # The DOLLAR cap must be what refuses it. This book is also outside the
    # 0.30-0.70 band, which U4 now enforces on this path too, so emptiness
    # alone would no longer identify the rule -- and the gate order puts the
    # dollar cap first precisely so the operator reads what is already at stake
    # rather than what a new fill would be worth.
    why_up = risk.hard_block(cfg, inv, "UP", 0.805,
                             _book("UPTOK", 0.82, 0.83),
                             _book("DNTOK", 0.17, 0.18))
    assert "budget" in why_up and "band" not in why_up, why_up
    # The light side must keep quoting -- it is the only thing that flattens.
    assert "DOWN" in sides, f"light side must keep flattening: {why}"
    # And at FULL size: the side that reduces exposure must not be tapered.
    # 0.155 is far outside U4's coin-flip zone, so nothing trims it either.
    assert [q.size for q in intents if q.side == "DOWN"] == [120]


def test_one_dollar_under_the_budget_it_is_the_ladder_that_stops_the_add():
    """$119 against a $120 budget, on a book INSIDE the 0.30-0.70 band.

    Before U3 this rested a full 120 shares -- which is the cliff R5 removes:
    the largest order of the market's life arriving with $1 of headroom left.
    The heavy side is still refused, and the two dollar rules are told apart
    directly rather than through the log, because `_decide_quotes_rewards`
    returns an empty reason whenever ANY side quotes and DOWN quotes here.

    The book moved from 0.82/0.83 to 0.60/0.61 and the position from 150
    shares to 200 to hold the SAME $119 of naked cost. It had to: `hard_block`
    now enforces the band, so at 0.82 the assertion below would have passed for
    the band's reason rather than showing the dollar arm standing down. The
    claim being made is about the ladder, so the fixture has to reach it.
    """
    cfg = _rcfg(max_naked_usd=120.0)
    inv = Inventory(up_shares=200.0, down_shares=0.0,
                    up_cost=119.0, down_cost=0.0)
    intents, why = _quote(cfg, up=(0.60, 0.61), dn=(0.39, 0.40), inv=inv)
    assert "UP" not in {q.side for q in intents}, why
    assert "DOWN" in {q.side for q in intents}, why

    up_book = {"token_id": "UPTOK", "best_bid": 0.60, "best_ask": 0.61,
               "bids": {0.60: 500.0}, "asks": {0.61: 500.0}}
    dn_book = {"token_id": "DNTOK", "best_bid": 0.39, "best_ask": 0.40,
               "bids": {0.39: 500.0}, "asks": {0.40: 500.0}}
    # The BLOCK passes at $119 -- it is a `>=` against the budget and this is a
    # dollar under it. The LADDER is what refuses: 120*(1/120)^2 = 0.008
    # shares, floored to zero because anything under 50 scores nothing.
    assert risk.hard_block(cfg, inv, "UP", 0.585, up_book, dn_book) is None
    assert risk.size_for(cfg, inv, "UP", 0.585) == 0


def test_the_light_side_is_never_blocked_by_the_dollar_cap():
    """$180 of naked UP is 150% of the budget, and the DOWN bid is still the
    only resting order that brings it back down. R4: the gates bound orders
    that ADD exposure, never one that reduces it."""
    inv = Inventory(up_shares=300.0, down_shares=0.0,
                    up_cost=180.0, down_cost=0.0)
    intents, why = _quote(_rcfg(max_naked_usd=120.0),
                          up=(0.60, 0.61), dn=(0.39, 0.40), inv=inv)
    assert [q.side for q in intents] == ["DOWN"], why


def test_an_untradeable_hedge_token_blocks_the_healthy_side_too():
    """KTD2. The UP book here is a clean 0.52/0.53 with 500sh of depth, and UP
    must still not be quoted: DOWN is a 0.999 bid against no ask, the exact
    shape recorded on wta-kalinsk-kessler. A UP fill would create a naked leg
    that nothing on the venue could close, because on a binary market the only
    instrument that hedges UP is DOWN.
    """
    dn = {"token_id": "DNTOK", "best_bid": 0.999, "best_ask": None,
          "bids": {0.999: 500.0}}
    intents, why = decide_quotes(_rcfg(), _book("UPTOK", 0.52, 0.53), dn,
                                 Inventory(), 200.0, None)
    assert intents == []
    assert "hedge" in why and "DOWN" in why, why


def test_turning_the_hard_blocks_off_stands_the_cap_down():
    """$240 of naked cost against a $120 budget, with the blocks switched off.

    Isolates the BLOCK as the cause of the refusals above -- nothing else about
    the input moved, so the switch is the only explanation for the cap standing
    down. The heavy side still does not REST, because the switch governs
    `hard_block` and nothing else, exactly as the config documents: at 200% of
    the budget U3's ladder sizes it to zero independently. Turning a block off
    cannot conjure a sensible size out of a budget that is already spent.
    """
    cfg = _rcfg(max_naked_usd=120.0, enable_hard_blocks=False)
    inv = Inventory(up_shares=400.0, down_shares=0.0,
                    up_cost=240.0, down_cost=0.0)
    up_book = {"token_id": "UPTOK", "best_bid": 0.60, "best_ask": 0.61,
               "bids": {0.60: 500.0}, "asks": {0.61: 500.0}}
    dn_book = {"token_id": "DNTOK", "best_bid": 0.39, "best_ask": 0.40,
               "bids": {0.39: 500.0}, "asks": {0.40: 500.0}}
    assert risk.hard_block(cfg, inv, "UP", 0.58, up_book, dn_book) is None
    intents, why = _quote(cfg, up=(0.60, 0.61), dn=(0.39, 0.40), inv=inv)
    assert "DOWN" in {q.side for q in intents}, why
    assert "UP" not in {q.side for q in intents}, why


def test_unsetting_the_budget_quotes_both_sides_at_twice_the_old_limit():
    """The same $240 position with `max_naked_usd=0`: the whole DOLLAR system
    unset, the way every other cap here is unset. Block, ladder and skew all
    stand down together, and both sides rest.

    Sizes are 82 and 95 rather than a flat 120 because U4's price-risk cut is
    a separate rule with its own switch, keyed to WHERE the quote sits (0.585
    and 0.375) and not to what is at risk. That is the point of the assertion
    below: the utilization taper is gone -- a $240 position on a $0 budget rests
    the same size a flat one would -- while the price cut is untouched. (Sizes
    dropped from 113/115 when `coinflip_size_cut` was strengthened from 10% to
    55% on 2026-08-06.)
    """
    cfg = _rcfg(max_naked_usd=0.0)
    inv = Inventory(up_shares=400.0, down_shares=0.0,
                    up_cost=240.0, down_cost=0.0)
    intents, why = _quote(cfg, up=(0.60, 0.61), dn=(0.39, 0.40), inv=inv)
    assert {q.side for q in intents} == {"UP", "DOWN"}, why
    flat, _ = _quote(cfg, up=(0.60, 0.61), dn=(0.39, 0.40), inv=Inventory())
    assert ({q.side: q.size for q in intents}
            == {q.side: q.size for q in flat} == {"UP": 82, "DOWN": 95})


def test_fleet_cap_stops_the_heavy_side_even_when_this_market_is_fine():
    """The per-market cap bounds ONE market; nothing bounded the fleet.

    Measured live: 16 markets each individually inside the 360-share cap still
    summed to $1,630 of unhedged exposure -- a +/-$456 swing against a $62
    edge. This market holds 100 shares carrying only $30 of naked cost -- a
    quarter of its own $120 cap, and low enough that U3's size ladder still
    rests 67 shares on the heavy side -- but the fleet is over budget, so it
    must stop adding.
    """
    inv = Inventory(up_shares=100.0, down_shares=0.0,
                    up_cost=30.0, down_cost=0.0)
    cfg = _rcfg(fleet_naked_usd=2000.0, max_fleet_naked_usd=800.0)
    intents, why = _quote(cfg, inv=inv)
    sides = {q.side for q in intents}
    assert "UP" not in sides, f"fleet over budget must stop the heavy side: {why}"
    assert "DOWN" in sides, f"light side still flattens: {why}"


def test_fleet_under_budget_quotes_both_sides_normally():
    """Same position, same books; only the fleet total moved. Isolates the
    fleet cap as what bound above -- and the heavy side comes back TAPERED.

    $3 of naked cost (not $30: since `coinflip_size_cut` was strengthened to
    55% on 2026-08-06, the heavy side floors to zero under the 50-share reward
    minimum at almost any meaningful utilization near the coin flip, so this
    keeps utilization low enough that a heavy-but-nonzero order still isolates
    the fleet cap from the price-risk cut)."""
    inv = Inventory(up_shares=3.0 / 0.52, down_shares=0.0,
                    up_cost=3.0, down_cost=0.0)
    cfg = _rcfg(fleet_naked_usd=100.0, max_fleet_naked_usd=800.0)
    intents, why = _quote(cfg, inv=inv)
    assert {q.side for q in intents} == {"UP", "DOWN"}, why
    assert [q.size for q in intents if q.side == "UP"] == [52]
    assert [q.size for q in intents if q.side == "DOWN"] == [72]
    # The taper is still what separates them: the heavy side is the only one
    # utilization touches, and it rests well under the light side.
    assert risk.size_for(cfg, inv, "UP", 0.49) == 114


def test_skew_never_leaves_the_reward_window_or_crosses():
    """Skew must not push a quote outside 4.5c -- outside it scores nothing.

    Two readings. A loop over the huge case alone would say nothing about the
    side skew pushes AWAY from mid, because U3's ladder removes it: at 100% of
    the budget the heavy side is sized to zero and only the light side is left
    to iterate over. So the window is pinned at a utilization where BOTH sides
    still rest, and the clamp at the near end is pinned separately.
    """
    # 40% of the budget, on a 1000-share base so the taper still leaves an
    # order on the heavy side. (Was 70%/90 shares; lowered on 2026-08-06 when
    # `coinflip_size_cut` was strengthened to 55% -- at 70% utilization the
    # heavy side now floors to zero under the 50-share reward minimum, which
    # this test does not exist to demonstrate.)
    cfg = _rcfg(quote_shares=1000, min_quote_shares=50, max_naked_usd=120.0)
    inv = Inventory(up_shares=48.0 / 0.52, down_shares=0.0,
                    up_cost=48.0, down_cost=0.0)
    intents, why = _quote(cfg, inv=inv)
    assert {q.side for q in intents} == {"UP", "DOWN"}, why
    for q in intents:
        s = q.mid - q.price
        assert cfg.min_reward_offset - 1e-9 <= s <= cfg.max_spread_from_mid + 1e-9
        assert q.price < q.mid

    # 400x the budget: utilization clamps at 1.0, so the skew bottoms out
    # rather than walking the light side onto the touch, and the heavy side is
    # gone on size rather than on skew.
    huge = Inventory(up_shares=100000.0, down_shares=0.0,
                     up_cost=50000.0, down_cost=0.0)
    base = _rcfg()
    intents, why = _quote(base, inv=huge)
    assert [q.side for q in intents] == ["DOWN"], why
    only = intents[0]
    # 1.7c: skew alone would put this at min_reward_offset (0.020 - 0.015 =
    # 0.005), and U4's 1.17c price-risk term at 0.445 is added on top before
    # the venue's 0.001 tick rounds it. The property that matters is unchanged
    # -- the spring bottoms out INSIDE the window and never reaches the touch.
    assert abs((only.mid - only.price) - 0.017) < 1e-9
    assert base.min_reward_offset <= (only.mid - only.price)
    assert only.price < only.mid


# --- emergency stop-loss (taker exception) ----------------------------------
#
# The rewards objective otherwise never crosses: taker fee is 0.07*p*(1-p),
# 1.75c/share at p=0.50, larger than any edge in this book. These tests pin the
# one case where that arithmetic stops applying -- when the alternative is not
# a smaller profit but an unbounded naked leg in a market running away from us.

def _rw(**kw):
    kw.setdefault("objective", "rewards")
    kw.setdefault("max_naked_usd", 120.0)
    kw.setdefault("emergency_hedge_frac", 0.8)
    return dataclasses.replace(BASE, **kw)


def _lopsided(up_sh=400.0, dn_sh=0.0, up_px=0.52):
    """Long UP, nothing on DOWN: 400sh of deficit on the DOWN side.

    Valued at the heavy leg's 0.52 average that is $208 of hedge still to buy,
    past the $96 (0.8 x $120) emergency trigger. The trigger is stated in
    dollars for the same reason the cap it sits inside is: 400 shares is $80 of
    deficit at 0.20 and $340 at 0.85, and only one of those is an emergency.
    """
    return Inventory(up_shares=up_sh, down_shares=dn_sh, up_cost=up_sh * up_px,
                     down_cost=0.0)


def _losing_books():
    """UP mid 0.455, below the 0.52 we paid: the heavy leg is losing now."""
    return _book("UPTOK", 0.45, 0.46), _book("DNTOK", 0.53, 0.54)


def test_unhedged_and_losing_crosses_the_spread():
    up, dn = _losing_books()
    intents, why = decide_quotes(_rw(), up, dn, _lopsided(), 1e9, None)
    hedge = [q for q in intents if q.crossed]
    assert len(hedge) == 1, why
    assert hedge[0].side == "DOWN"
    assert hedge[0].price == 0.54            # the ask, not a tick under it
    assert hedge[0].size == 400              # the whole deficit


def test_a_big_deficit_in_a_flat_market_does_not_cross():
    """Size alone is not an emergency. UP mid 0.525 is above the 0.52 we paid,
    so the position is not losing and the fee would buy nothing."""
    up = _book("UPTOK", 0.52, 0.53)
    dn = _book("DNTOK", 0.46, 0.47)
    intents, _ = decide_quotes(_rw(), up, dn, _lopsided(), 1e9, None)
    assert [q for q in intents if q.crossed] == []


def test_a_losing_position_inside_the_trigger_does_not_cross():
    """150sh at a 0.52 average is $78 of deficit, under 0.8 x $120 = $96.

    Skew still owns this range, and paying the taker fee inside it buys nothing
    the resting light-side bid was not already going to get for free.
    """
    up, dn = _losing_books()
    intents, _ = decide_quotes(_rw(), up, dn, _lopsided(up_sh=150.0), 1e9, None)
    assert [q for q in intents if q.crossed] == []


def test_an_unhealthy_hedge_book_does_not_stop_the_emergency_cross():
    """R4, on the case that matters most. Both books here are 16c wide, which
    the book-health arm refuses for a NEW bid -- and this is not a new bid, it
    is the exit from a $208 naked leg whose mid (0.34) has fallen 18c below the
    0.52 we paid. A gate that refuses the hedge in the one market that needs it
    has inverted its own purpose.
    """
    up = _book("UPTOK", 0.26, 0.42)      # mid 0.34, far under the 0.52 average
    dn = _book("DNTOK", 0.58, 0.74)
    intents, why = decide_quotes(_rw(), up, dn, _lopsided(), 1e9, None)
    hedge = [q for q in intents if q.crossed]
    assert len(hedge) == 1, why
    assert hedge[0].side == "DOWN"
    assert hedge[0].price == 0.74        # the ask -- we are taking, not resting


def test_the_exception_is_switchable():
    """Isolates the exception as the cause -- nothing else about the input
    moved, and a taker order is the one thing this strategy otherwise never
    does, so it must be measurable on its own."""
    up, dn = _losing_books()
    intents, _ = decide_quotes(_rw(enable_emergency_hedge=False), up, dn,
                               _lopsided(), 1e9, None)
    assert [q for q in intents if q.crossed] == []


def test_ordinary_reward_quotes_are_never_marked_crossed():
    intents, _ = decide_quotes(_rw(), _book("UPTOK", 0.52, 0.53),
                               _book("DNTOK", 0.46, 0.47), Inventory(),
                               1e9, None)
    assert intents and all(not q.crossed for q in intents)
    assert all(q.price < q.mid for q in intents)


# --- U3: bounding what is committed, not just what is unhedged ---------------

def _rw_quote(cfg, inv=None, up=(0.52, 0.53), dn=(0.46, 0.47)):
    return decide_quotes(cfg, _book("UPTOK", *up),
                         _book("DNTOK", *dn), inv or Inventory(),
                         1e9, None)


def test_under_the_committed_cap_both_sides_quote():
    intents, _ = _rw_quote(_rw(max_committed_usd=2000.0, committed_usd=500.0))
    assert {q.side for q in intents} == {"UP", "DOWN"}


def test_at_the_committed_cap_a_balanced_book_stops_quoting():
    """Balanced means neither side reduces anything, so both are additions."""
    intents, why = _rw_quote(
        _rw(max_committed_usd=2000.0, committed_usd=2000.0))
    assert intents == []
    assert "committed" in why


def test_at_the_committed_cap_the_reducing_side_still_quotes():
    """The cap must never remove the only route back under itself. Merge needs
    a matched pair, and the light side is what produces one -- blocking it
    would freeze the fleet at maximum commitment permanently."""
    inv = Inventory(up_shares=300.0, down_shares=0.0, up_cost=300.0 * 0.52,
                    down_cost=0.0)
    intents, _ = _rw_quote(
        _rw(max_committed_usd=2000.0, committed_usd=2500.0), inv=inv)
    assert [q.side for q in intents] == ["DOWN"]      # the light side only


def test_inventory_alone_can_breach_the_cap_with_no_offers_resting():
    """The cap is on committed capital, not on open orders. $9,588 had left
    the wallet while only $1,369 was resting in offers."""
    intents, why = _rw_quote(
        _rw(max_committed_usd=2000.0, committed_usd=9588.0))
    assert intents == []
    assert "committed" in why


def test_the_committed_cap_names_itself_separately_from_the_naked_cap():
    """An operator reading 'not adding' has to be able to tell which limit
    bound, or the dashboard shows a dead market with no explanation."""
    _, why = _rw_quote(_rw(max_committed_usd=2000.0, committed_usd=2000.0,
                           fleet_naked_usd=0.0))
    assert "committed" in why and "unhedged" not in why


def test_a_zero_committed_cap_disables_the_rule():
    """Same escape hatch every other cap here has -- 0 means unset, not
    'commit nothing'."""
    intents, _ = _rw_quote(_rw(max_committed_usd=0.0, committed_usd=99999.0))
    assert intents


# --- U3: the fill cap that never ran ----------------------------------------

def test_the_fill_cap_applies_to_the_rewards_objective():
    """REGRESSION. max_fills_per_market was checked in `decide_quotes` several
    lines AFTER the rewards path had already returned, so it never executed on
    the objective the fleet actually runs. Three markets reached 26 fills
    against a nominal limit of 25."""
    inv = Inventory(fills=25)
    intents, why = _rw_quote(_rw(max_fills_per_market=25), inv=inv)
    assert intents == []
    assert "25 fills" in why


def test_one_fill_below_the_cap_still_quotes():
    inv = Inventory(fills=24)
    intents, _ = _rw_quote(_rw(max_fills_per_market=25), inv=inv)
    assert intents


def test_a_zero_allocation_actually_stops_quoting():
    """REGRESSION. reallocate has always documented that an unfunded market
    'gets 0 and stops quoting', and it never did -- `max(quote_shares,
    min_quote_shares)` promoted the 0 back to the venue minimum, so a
    deliberately defunded market kept posting 50-share orders."""
    intents, why = _rw_quote(_rw(quote_shares=0))
    assert intents == []
    assert "unfunded" in why


# --- U3: the continuous size ladder -----------------------------------------
#
# The dollar cap of U2 is a cliff: $119.99 of naked cost rests the full 120
# shares and $120.00 rests nothing. That shape makes the LAST order before the
# limit the LARGEST one -- on lol-maz-mg1 the position took its biggest single
# step toward the cap at the moment it was closest to it. R5 replaces the step
# with base*(1-u)^2, so size decays as the budget fills and reaches zero AT it.


def _heavy_up(usd, avg=0.52):
    """UP-heavy inventory holding exactly `usd` of naked cost at `avg`."""
    return Inventory(up_shares=(usd / avg) if usd else 0.0, down_shares=0.0,
                     up_cost=usd, down_cost=0.0)


def test_size_walks_down_to_nothing_instead_of_stepping_off_a_cliff():
    """U3's Done signal: flat -> budget produces a DECREASING size sequence.

    400 shares of base, a $120 budget, and a market walked from flat to the cap
    in $24 steps. The old rule produced 400, 400, 400, 400, 400, none. The
    ladder lands softly -- the last order placed is well under full size, and
    the sequence never steps from full size to zero.

    Every rung also carries U4's coin-flip cut at 0.505, a constant here and
    therefore changing the levels without changing the shape this test is
    about. (Raised from a 10% cut to 55% on 2026-08-06, per the 2026-08-05
    forensic audit's measured -10.68c / -$122.00 in the 0.40-0.60 band -- the
    sequence now reaches None one step earlier than before.)
    """
    cfg = _rw(quote_shares=400, min_quote_shares=50, max_naked_usd=120.0)
    seq = []
    for usd in (0.0, 24.0, 48.0, 72.0, 96.0, 120.0):
        intents, _ = _rw_quote(cfg, inv=_heavy_up(usd))
        up = [q.size for q in intents if q.side == "UP"]
        seq.append(up[0] if up else None)

    assert seq == [185, 91, 66, None, None, None]
    live = [s for s in seq if s is not None]
    assert live == sorted(live, reverse=True) and len(set(live)) == len(live)
    assert live[-1] < 400 * 0.25, "the last order before the cap must be small"


def test_the_light_side_keeps_full_size_all_the_way_up_the_ladder():
    """R4 in the sizing layer: the taper must never slow the exit. Same walk as
    above, reading the side that REDUCES exposure.

    'Full size' is now 240 rather than the 400-share base (was 371 at the old
    10% coin-flip cut; the cut was strengthened to 55% on 2026-08-06 per the
    2026-08-05 forensic audit): U4's price-risk cut reads the PRICE and applies
    to both legs of a binary market symmetrically, unlike the exposure taper,
    which is one-sided because only one side reduces exposure. What R4 demands
    is that utilization never touches this number, and it does not -- it is
    the same 240 at $24 of risk and at $120.
    """
    cfg = _rw(quote_shares=400, min_quote_shares=50, max_naked_usd=120.0)
    sizes = []
    for usd in (24.0, 48.0, 72.0, 96.0, 120.0):
        intents, why = _rw_quote(cfg, inv=_heavy_up(usd))
        down = [q.size for q in intents if q.side == "DOWN"]
        assert down, f"light side dropped at ${usd:.0f}: {why}"
        sizes.append(down[0])
    assert sizes == [240] * 5, sizes
    # And it IS the untapered base, only price-cut: `size_for` never taper the
    # light side, so the whole difference from 400 is the coin-flip factor.
    assert sizes[0] == int(400 * risk.band_risk_factor(cfg, 0.445).size_mult)


def test_the_taper_names_itself_when_it_zeroes_a_side():
    """A zero-size order is not an order: no intent, and a reason of its own.

    `_decide_quotes_rewards` returns an empty reason whenever ANY side quotes,
    so the log is only readable on a market where nothing rests. DOWN here has
    no ask to quote against, and the hard blocks are off, which leaves the
    ladder as the only rule that can refuse UP -- and it must not borrow the
    dollar cap's wording, or an operator would go looking for the wrong limit.
    """
    cfg = _rw(max_naked_usd=120.0, enable_hard_blocks=False)
    dn = {"token_id": "DNTOK", "best_bid": 0.46, "best_ask": None,
          "bids": {0.46: 500.0}}
    intents, why = decide_quotes(cfg, _book("UPTOK", 0.52, 0.53), dn,
                                 _heavy_up(240.0), 1e9, None)
    assert intents == []
    assert "tapered to 0" in why, why
    assert "not adding" not in why, why


def test_no_side_ever_rests_a_zero_size_order():
    """The whole walk, read for the property a zero would violate: an order
    below rewardsMinSize scores nothing and still buys inventory."""
    cfg = _rw(quote_shares=400, min_quote_shares=50, max_naked_usd=120.0)
    for usd in (0.0, 24.0, 48.0, 72.0, 96.0, 120.0):
        intents, _ = _rw_quote(cfg, inv=_heavy_up(usd))
        assert all(q.size >= cfg.min_quote_shares for q in intents)


def test_well_inside_the_budget_the_heavy_side_still_rests_at_a_reduced_size():
    """Isolates the ladder against 'the heavy side just stopped quoting'.

    $5 of $120 (was $36/30% before `coinflip_size_cut` was strengthened to
    55% on 2026-08-06 -- at 30% utilization the ladder's own 58.8 shares no
    longer survives a 55% price-risk cut and floors to zero under the
    50-share reward minimum, which this test does not exist to demonstrate).
    The side keeps scoring, at well under its ladder-only size.
    """
    intents, why = _rw_quote(_rw(max_naked_usd=120.0), inv=_heavy_up(5.0))
    up = [q.size for q in intents if q.side == "UP"]
    assert up == [51], why
    assert up[0] < 120, "size must respond to utilization at all"


def test_a_zero_budget_leaves_every_size_full():
    """0 unsets the DOLLAR machinery wholesale, the same escape hatch every
    other cap here has -- not 'taper everything to nothing'.

    Read against the same books held flat rather than against the 120-share
    base: U4's price-risk cut is a different rule with a different switch, and
    it still applies. What must be identical is a $240 position and a flat one,
    which is exactly what 'the budget is unset' means.
    """
    cfg = _rw(max_naked_usd=0.0)
    inv = Inventory(up_shares=400.0, down_shares=0.0, up_cost=240.0)
    intents, why = _rw_quote(cfg, inv=inv)
    assert {q.side for q in intents} == {"UP", "DOWN"}, why
    flat, _ = _rw_quote(cfg, inv=Inventory())
    # Was {"UP": 108, "DOWN": 111} before `coinflip_size_cut` was strengthened
    # from 10% to 55% on 2026-08-06.
    assert ({q.side: q.size for q in intents}
            == {q.side: q.size for q in flat} == {"UP": 55, "DOWN": 72})


# --- U4: the band, the pair cap and price risk on the live path -------------
#
# `_in_band` and the max_pair_cost comparison have existed since the beginning
# and neither has ever run on the objective the fleet quotes: both sit in the
# legacy branch of `decide_quotes`, BELOW the line where the rewards path
# returns. The telemetry reads exactly as absent rules would -- fills averaged
# 0.8152 against a nominal 0.30-0.70 band, and wta-kalinsk-kessler bought 14
# pairs at $1.0200 against a $0.995 cap on an instrument paying exactly $1.00.


def test_the_band_applies_to_the_rewards_objective():
    """U4's Done signal. A 0.95/0.96 market quotes TODAY under the default
    objective; nothing about the book stops it, because the band never ran.

    The reason must name the band specifically. Book health does not catch this
    shape -- 0.95 and 0.96 are both further than `decided_price` (0.02) from
    either end, the spread is a cent, and there is 500sh of depth -- so an
    assertion on emptiness alone would pass for the wrong reason.
    """
    intents, why = _quote(_rcfg(), up=(0.95, 0.96), dn=(0.03, 0.04))
    assert intents == []
    assert "outside band" in why, why
    assert "settled" not in why and "wide" not in why, why


def test_turning_the_band_off_lets_the_rewards_objective_quote_it():
    """Isolates the band as the cause -- nothing else about the input moved."""
    cfg = _rcfg(enforce_price_band=False)
    intents, why = _quote(cfg, up=(0.95, 0.96), dn=(0.03, 0.04))
    assert {q.side for q in intents} == {"UP", "DOWN"}, why


def test_the_pair_cap_applies_to_the_rewards_objective():
    """A 0.52-ish UP bid against a 0.49 DOWN average is a $1.01 pair on a $1.00
    payout -- the shape wta-kalinsk-kessler repeated 14 times at $1.0200.

    Balanced inventory on purpose: R4 exempts the side that REDUCES exposure,
    and with 100 shares of each neither side reduces anything, so both are
    additions and the cap is reachable.

    DOWN keeps quoting, and must: its 0.433 bid against the 0.52 UP average is
    a $0.953 pair, which is the trade this strategy exists to make. The cap is
    per-order and per-side, not a switch that shuts the market down. That also
    means `why` is empty here -- `_decide_quotes_rewards` reports no reason
    while any side quotes -- so the arm is read from `hard_block` directly.
    """
    cfg = _rcfg(max_pair_cost=0.995)
    inv = Inventory(up_shares=100.0, down_shares=100.0,
                    up_cost=52.0, down_cost=49.0)
    intents, why = _quote(cfg, inv=inv)
    assert "UP" not in {q.side for q in intents}, why
    assert "DOWN" in {q.side for q in intents}, why
    why_up = risk.hard_block(cfg, inv, "UP", 0.505,
                             _book("UPTOK", 0.52, 0.53),
                             _book("DNTOK", 0.46, 0.47))
    assert why_up is not None and "pair" in why_up, why_up


def test_the_same_market_against_a_cheaper_hedge_still_quotes_both_sides():
    """Isolates the pair cap: the same books, the same share counts, a 0.40
    DOWN average instead of 0.49 -- a $0.92 pair, which is the trade this
    strategy exists to make."""
    inv = Inventory(up_shares=100.0, down_shares=100.0,
                    up_cost=52.0, down_cost=40.0)
    intents, why = _quote(_rcfg(max_pair_cost=0.995), inv=inv)
    assert {q.side for q in intents} == {"UP", "DOWN"}, why


def test_a_clamped_offset_becomes_a_smaller_order_instead_of_nothing():
    """KTD3. Under WIDENED the base offset is already 0.035, so 0.035 plus the
    price-risk terms asks for more than the 4.5c reward window allows. The
    window wins -- outside it an order scores nothing -- but the truncated
    remainder must not simply evaporate, or risk aversion would have nowhere to
    go in exactly the state entered BECAUSE fills were losing money. It is
    converted into a proportional size reduction instead.
    """
    cfg = _rcfg(gate_state=gate.WIDENED)
    intents, why = _quote(cfg, inv=Inventory())
    up = [q for q in intents if q.side == "UP"]
    assert up, why
    q = up[0]
    # The offset lands exactly ON the window: the clamp bound.
    assert abs((q.mid - q.price) - cfg.max_spread_from_mid) < 1e-9
    # And the size is below what the ladder alone would rest at this price.
    assert q.size < risk.size_for(cfg, Inventory(), "UP", q.price)


def test_an_unclamped_offset_leaves_the_size_to_the_ladder_alone():
    """Isolates the truncation term. NORMAL keeps base+risk inside the window,
    so only the price-risk size cut applies and the KTD3 remainder is zero."""
    cfg = _rcfg()
    intents, why = _quote(cfg, inv=Inventory())
    up = [q for q in intents if q.side == "UP"][0]
    assert (up.mid - up.price) < cfg.max_spread_from_mid
    ladder = risk.size_for(cfg, Inventory(), "UP", up.price)
    mult = risk.band_risk_factor(cfg, round(up.mid - cfg.reward_offset, 4)).size_mult
    assert up.size == int(ladder * mult), why


def test_the_dearer_side_of_a_flat_book_is_quoted_wider():
    """R6 end to end, on an inventory that cannot be skewed. UP rests near
    0.505 and DOWN near 0.445: the same share count, but $0.06 more downside
    per share on UP, so UP is the side that must sit further from mid."""
    intents, why = _quote(_rcfg(), inv=Inventory())
    off = {q.side: q.mid - q.price for q in intents}
    assert off["UP"] > off["DOWN"], why


def test_the_skew_responds_to_dollars_not_to_share_count():
    """THE UNIT, read end to end. The SAME 100-share imbalance costing $34 and
    $6, and the expensive one is the urgent one. The share-denominated spring
    answered both identically, which is why it was still ramping on lol-maz-mg1
    at 233 of 240 shares while the position was already fully built.

    Both cases now run on the SAME 0.33/0.35 book, which makes this a stricter
    isolation than the 0.34-market-versus-0.06-market pairing it replaces: the
    price-risk terms of U4 are then identical on both sides of the comparison,
    so the only thing that can move the offsets is the dollar reading. The old
    0.06 fixture is no longer reachable in any case -- a 0.04 quote is outside
    the 0.30-0.70 band, which `hard_block` now enforces on this path, so that
    market is refused before the skew is ever consulted.
    """
    cfg = _rw(max_naked_usd=120.0)
    dear = Inventory(up_shares=100.0, down_shares=0.0, up_cost=34.0)
    cheap = Inventory(up_shares=100.0, down_shares=0.0, up_cost=6.0)
    assert dear.up_shares == cheap.up_shares, "same SHARE imbalance by design"

    a, why_a = decide_quotes(cfg, _book("UPTOK", 0.33, 0.35),
                             _book("DNTOK", 0.65, 0.67), dear, 1e9, None)
    b, why_b = decide_quotes(cfg, _book("UPTOK", 0.33, 0.35),
                             _book("DNTOK", 0.65, 0.67), cheap, 1e9, None)
    off_a = {q.side: q.mid - q.price for q in a}
    off_b = {q.side: q.mid - q.price for q in b}
    assert "UP" in off_a and "UP" in off_b, f"{why_a} / {why_b}"
    assert off_a["UP"] > off_b["UP"], (
        "the more expensive naked position must be pushed further from mid")
    assert off_a["DOWN"] < off_b["DOWN"], (
        "and its hedge must be pulled harder toward mid")


# --- U6: the fleet circuit breaker ------------------------------------------
#
# Every cap above bounds a QUANTITY -- dollars naked, dollars committed, shares
# per order. None of them reads whether the fills we are buying are good. The
# pooled markout does, and on the recorded run it reads -4.75c/share while
# every per-market gate sits at WIDENED, because no single market ever reaches
# a sample of its own. HALTED is that reading turned into an action: stop
# ADDING everywhere at once.
#
# It is a POSTURE, not a state (KTD5). It blocks the heavy side only, leaves
# the light side, the merge and the emergency exit open, and it is derived
# fresh each sweep -- so it lifts by itself when the pool recovers. EXITED is
# the opposite in all three respects, and the two must not be confused.

def test_the_fleet_halt_stops_the_heavy_side():
    """R9. The market itself is fine -- in band, two-sided, $30 of a $120
    budget -- and the fleet reading is what refuses the addition."""
    intents, why = _rw_quote(_rw(fleet_posture=gate.HALTED),
                             inv=_heavy_up(30.0))
    assert [q.side for q in intents] == ["DOWN"], why


def test_the_fleet_halt_leaves_the_light_side_at_full_size():
    """R4/R10. The light side is the only resting order that FLATTENS us, so a
    halt that touched it would freeze the fleet at maximum exposure with no
    route down -- and it must not be quietly shrunk either: the size is
    identical to the one the same market rests with no halt at all."""
    inv = _heavy_up(30.0)
    halted, why_h = _rw_quote(_rw(fleet_posture=gate.HALTED), inv=inv)
    normal, why_n = _rw_quote(_rw(fleet_posture=gate.NORMAL), inv=inv)
    dn_h = [q.size for q in halted if q.side == "DOWN"]
    dn_n = [q.size for q in normal if q.side == "DOWN"]
    assert dn_h and dn_h == dn_n, f"{why_h} / {why_n}"


def test_a_flat_market_rests_on_both_sides_under_the_halt():
    """Neither side of a flat book is the heavy one, so neither is an addition
    to an existing naked leg. A halt that shut flat markets down would forfeit
    the rent across the fleet to prevent exposure that does not exist."""
    intents, why = _rw_quote(_rw(fleet_posture=gate.HALTED), inv=Inventory())
    assert {q.side for q in intents} == {"UP", "DOWN"}, why


def test_the_halt_does_not_persist_into_the_next_sweep():
    """THE DIFFERENCE FROM EXITED, stated as behaviour. The posture is derived
    from the pooled reading every sweep and never written down, so a recovered
    fleet quotes the heavy side again with no re-entry rule to clear."""
    # Off-center books: this test is about HALT/recover, not the coinflip
    # band, and 55% (strengthened 2026-08-06) floors the heavy side at 0.52.
    inv = _heavy_up(30.0)
    halted, _ = _rw_quote(_rw(fleet_posture=gate.HALTED), inv=inv,
                          up=(0.65, 0.66), dn=(0.32, 0.33))
    assert "UP" not in {q.side for q in halted}
    recovered, why = _rw_quote(_rw(fleet_posture=gate.NORMAL), inv=inv,
                               up=(0.65, 0.66), dn=(0.32, 0.33))
    assert "UP" in {q.side for q in recovered}, why


def test_the_halt_names_itself_in_the_blocked_reason():
    """An operator reading 'not adding' has to be able to tell which limit
    bound. The fleet DOLLAR cap and the fleet MARKOUT halt both stop the heavy
    side fleet-wide for entirely different reasons, and the log must separate
    them. `min_quote_shares` is raised only so that nothing rests at all --
    `_decide_quotes_rewards` reports no reason while any side quotes.
    """
    cfg = _rw(fleet_posture=gate.HALTED, min_quote_shares=10_000)
    intents, why = _rw_quote(cfg, inv=_heavy_up(30.0))
    assert intents == []
    assert "UP: fleet HALTED" in why, why
    assert "unhedged" not in why and "committed" not in why, why


def test_the_emergency_exit_still_crosses_under_the_halt():
    """R4 on the case that matters most. A $208 naked leg whose mid has fallen
    below what we paid is the exact position the halt exists to stop us
    building -- and the order that CLOSES it must never be caught by the rule
    that objects to opening it."""
    up, dn = _losing_books()
    intents, why = decide_quotes(_rw(fleet_posture=gate.HALTED), up, dn,
                                 _lopsided(), 1e9, None)
    hedge = [q for q in intents if q.crossed]
    assert len(hedge) == 1, why
    assert hedge[0].side == "DOWN" and hedge[0].size == 400


def test_an_exited_market_is_unaffected_by_the_fleet_posture():
    """The two mechanisms are independent, in both directions: EXITED is a
    verdict on THIS market and outranks any per-market posture, and the posture is a
    verdict on the universe that can neither impose nor lift it."""
    for posture in (gate.NORMAL, gate.HALTED):
        intents, why = _rw_quote(
            _rw(gate_state=gate.EXITED, fleet_posture=posture),
            inv=_heavy_up(30.0))
        assert intents == [], posture


# --- Plan 03: the spread-capture objective --------------------------------
#
# Short-dated markets (t_remaining <= 1 day) switch to `objective="spread"`,
# which rests INSIDE the touch to skim the bid/ask spread instead of earning
# the rewards window. The pair-cost cap still binds (the instrument pays $1.00).


def test_spread_objective_rests_inside_the_touch():
    """A wide-spread book under the spread objective rests one tick above the
    best bid -- inside the touch, where a marketable sell hits us first."""
    cfg = _cfg(objective="spread", spread_capture_default_spread=0.01,
               tick_size=0.001, min_quote_shares=5)
    intents, why = _quote(cfg, up=(0.48, 0.52), dn=(0.46, 0.50))
    sides = {q.side for q in intents}
    assert sides == {"UP", "DOWN"}, why
    for q in intents:
        # price must be > best_bid (inside touch) and < best_ask.
        bb = 0.48 if q.side == "UP" else 0.46
        ba = 0.52 if q.side == "UP" else 0.50
        assert bb < q.price < ba, f"{q.side}: {q.price} not inside {bb}/{ba}"
        assert "spread capture" in q.reason, q.reason


def test_spread_objective_refuses_thin_spread():
    """A one-tick spread leaves nothing to capture, so the spread objective
    must sit out rather than rest at the touch for no edge."""
    cfg = _cfg(objective="spread", spread_capture_default_spread=0.01,
               tick_size=0.001, min_quote_shares=5)
    # 0.481/0.482 -> spread 0.001 < 0.01 min.
    intents, why = _quote(cfg, up=(0.481, 0.482), dn=(0.481, 0.482))
    assert intents == [], why
    assert "spread" in why, why
