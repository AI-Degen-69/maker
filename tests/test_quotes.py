"""Tests for the two powerwinner entry rules added to decide_quotes.

Both are switchable, and each is tested with the OTHER one off, so a passing
test can only be explained by the rule it names.
"""
import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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


def test_rewards_quotes_both_sides_under_mid():
    intents, why = _quote(_rcfg(reward_offset=0.02))
    assert {q.side for q in intents} == {"UP", "DOWN"}, why
    for q in intents:
        assert q.price < q.mid, "a reward quote must rest UNDER mid, never above"
        assert abs((q.mid - q.price) - 0.02) < 1e-6


def test_rewards_pair_is_under_one_dollar_by_construction():
    """The property the pair objective spent 60 markets failing to reach.

    mid_up + mid_down ~ 1.00, so bidding `offset` under mid on both sides costs
    ~1.00 - 2*offset. Nothing has to line up for this; it is arithmetic.
    """
    intents, _ = _quote(_rcfg(reward_offset=0.02))
    pair = sum(q.price for q in intents)
    mids = sum(q.mid for q in intents)
    assert pair < 1.0
    # Exactly 2*offset under the sum of the mids, whatever that sum happens to
    # be. On a real book the two mids sum to ~1.00 so the pair lands near 0.96;
    # asserting against `mids` states the mechanism rather than the fixture.
    assert abs(pair - (mids - 2 * 0.02)) < 1e-6


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
    inv = Inventory(up_shares=240.0, down_shares=0.0, up_cost=120.0, down_cost=0.0)
    intents, why = _quote(_rcfg(), inv=inv)
    assert {q.side for q in intents} == {"UP", "DOWN"}, why
    off = {q.side: q.mid - q.price for q in intents}
    assert off["UP"] > off["DOWN"], "heavy side must sit FURTHER from mid"
    # The light side is pulled toward mid, so it is the one that fills next.
    assert off["DOWN"] < 0.02


def test_skew_is_symmetric_and_flat_when_balanced():
    inv = Inventory(up_shares=120.0, down_shares=120.0, up_cost=60.0, down_cost=60.0)
    intents, _ = _quote(_rcfg(), inv=inv)
    off = {q.side: round(q.mid - q.price, 4) for q in intents}
    assert off["UP"] == off["DOWN"], "a flat book must not be skewed"


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
    """The cap must not bite on a normal, tight book."""
    cfg = _rcfg()
    intents, _ = _quote(cfg, up=(0.52, 0.53), dn=(0.46, 0.47))
    for q in intents:
        assert abs((q.mid - q.price) - cfg.reward_offset) < 1e-6


def test_heavy_side_is_dropped_once_imbalance_passes_the_hard_cap():
    """Past the cap the heavy side must stop quoting entirely.

    Skew saturates at skew_full_shares (240): beyond that, more imbalance buys
    no more response, because `skew` is already clamped to max_skew. Measured
    live, positions ran to 681 shares unhedged -- 2.8x saturation -- while the
    only hard stop, max_cost_per_market, sat at $400 against a $109 position
    and never engaged. A spring that bottoms out is not a brake, so the cap is
    what actually bounds directional exposure.
    """
    inv = Inventory(up_shares=700.0, down_shares=0.0,
                    up_cost=112.0, down_cost=0.0)
    intents, why = _quote(_rcfg(), inv=inv)
    sides = {q.side for q in intents}
    assert "UP" not in sides, f"heavy side must stop adding exposure: {why}"
    # The light side must keep quoting -- it is the only thing that flattens.
    assert "DOWN" in sides, f"light side must keep flattening: {why}"


def test_cap_leaves_the_saturation_point_itself_still_two_sided():
    """At skew saturation we still want both sides: skew has authority there.

    The cap exists for the region where skew has none. Firing it at 240 too
    would trade away two thirds of the reward score (a one-sided book scores
    at 1/c, c=3.0) at exactly the imbalance skew is designed to handle.
    """
    inv = Inventory(up_shares=240.0, down_shares=0.0,
                    up_cost=120.0, down_cost=0.0)
    intents, why = _quote(_rcfg(), inv=inv)
    assert {q.side for q in intents} == {"UP", "DOWN"}, why


def test_fleet_cap_stops_the_heavy_side_even_when_this_market_is_fine():
    """The per-market cap bounds ONE market; nothing bounded the fleet.

    Measured live: 16 markets each individually inside the 360-share cap still
    summed to $1,630 of unhedged exposure -- a +/-$456 swing against a $62
    edge. This market holds only 100 shares of imbalance, well under its own
    cap, but the fleet is over budget, so it must stop adding.
    """
    inv = Inventory(up_shares=100.0, down_shares=0.0,
                    up_cost=50.0, down_cost=0.0)
    cfg = _rcfg(fleet_naked_usd=2000.0, max_fleet_naked_usd=800.0)
    intents, why = _quote(cfg, inv=inv)
    sides = {q.side for q in intents}
    assert "UP" not in sides, f"fleet over budget must stop the heavy side: {why}"
    assert "DOWN" in sides, f"light side still flattens: {why}"


def test_fleet_under_budget_quotes_both_sides_normally():
    inv = Inventory(up_shares=100.0, down_shares=0.0,
                    up_cost=50.0, down_cost=0.0)
    cfg = _rcfg(fleet_naked_usd=100.0, max_fleet_naked_usd=800.0)
    intents, why = _quote(cfg, inv=inv)
    assert {q.side for q in intents} == {"UP", "DOWN"}, why


def test_skew_never_leaves_the_reward_window_or_crosses():
    """Skew must not push a quote outside 4.5c -- outside it scores nothing."""
    huge = Inventory(up_shares=100000.0, down_shares=0.0,
                     up_cost=50000.0, down_cost=0.0)
    cfg = _rcfg()
    intents, _ = _quote(cfg, inv=huge)
    for q in intents:
        s = q.mid - q.price
        assert cfg.min_reward_offset - 1e-9 <= s <= cfg.max_spread_from_mid + 1e-9
        assert q.price < q.mid


# --- emergency stop-loss (taker exception) ----------------------------------
#
# The rewards objective otherwise never crosses: taker fee is 0.07*p*(1-p),
# 1.75c/share at p=0.50, larger than any edge in this book. These tests pin the
# one case where that arithmetic stops applying -- when the alternative is not
# a smaller profit but an unbounded naked leg in a market running away from us.

def _rw(**kw):
    kw.setdefault("objective", "rewards")
    kw.setdefault("max_naked_shares", 360.0)
    kw.setdefault("emergency_hedge_frac", 0.8)
    return dataclasses.replace(BASE, **kw)


def _lopsided(up_sh=400.0, dn_sh=0.0, up_px=0.52):
    """Long UP, nothing on DOWN: 400sh of deficit on the DOWN side, past the
    288sh (0.8 x 360) emergency trigger."""
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
    """250sh is under 0.8 x 360 = 288. Skew still owns this range."""
    up, dn = _losing_books()
    intents, _ = decide_quotes(_rw(), up, dn, _lopsided(up_sh=250.0), 1e9, None)
    assert [q for q in intents if q.crossed] == []


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

def _rw_quote(cfg, inv=None):
    return decide_quotes(cfg, _book("UPTOK", 0.52, 0.53),
                         _book("DNTOK", 0.46, 0.47), inv or Inventory(),
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
