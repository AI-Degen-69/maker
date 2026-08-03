"""The water-fill was read as a diversifier and is not one.

`marginal` is daily*T/(capital+T)^2. Whenever competitor depth T dominates our
own size that expression is nearly FLAT in capital -- so the argmax never
changes hands, one market wins every increment, and the loop is a
winner-take-all auction wearing a diminishing-returns costume.

Measured 2026-08-02: one market took the entire $900 budget, `shares_for`
turned it into a 900-share order, and it filled in a single print for $792 --
79% of a $1,000 wallet, against a nominal $400 per-market cost cap.

`max_market_frac` bounds any one market's share of the budget. 0.15 puts the
floor at roughly seven concurrently funded markets, and that number comes from
the variance rather than from taste: per-fill markout measured -$7.58 with a
standard deviation of $56.68, so at full concentration one fill moves the book
by more than the entire expected edge of a hundred fills.
"""
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.allocate import allocate, allocate_fundable, shares_for  # noqa: E402
from strategy.config import load as load_cfg                          # noqa: E402
from strategy.fleet import MarketState, reallocate                    # noqa: E402

BUDGET = 900.0
FRAC = 0.15
CAP = BUDGET * FRAC          # $135


def _m(cid, daily, share=0.05, capital=100.0, **kw):
    """A market as the allocator sees it. A low `share` means heavy
    competition, which is the regime where marginal() goes flat."""
    return {"cid": cid, "daily": daily, "capital": capital, "share": share, **kw}


# --- the concentration bound -------------------------------------------------

def test_one_dominant_market_took_the_whole_budget_before():
    """The bug, pinned so the fix is measured against it rather than asserted.
    A single market that outclasses the field absorbs every increment."""
    mk = [_m("dominant", 50.0), _m("second", 12.0), _m("third", 9.0)]
    out = allocate(mk, BUDGET, floor=0.02)
    assert out["dominant"] > CAP
    assert out["second"] == 0.0 and out["third"] == 0.0


def test_no_market_exceeds_the_fraction():
    mk = [_m("dominant", 50.0), _m("second", 12.0), _m("third", 9.0)]
    out = allocate(mk, BUDGET, floor=0.02, max_frac=FRAC)
    assert max(out.values()) <= CAP
    assert out["dominant"] == pytest.approx(CAP)


def test_the_bound_holds_across_field_sizes():
    """Swept rather than asserted at one shape: the cap is a property of the
    budget, so it must not depend on how many markets are competing."""
    for n in (1, 2, 3, 8, 20):
        mk = [_m(f"m{i}", 50.0 - i) for i in range(n)]
        out = allocate(mk, BUDGET, floor=0.02, max_frac=FRAC)
        assert max(out.values()) <= CAP + 1e-9, f"breached with {n} markets"


def test_a_lone_market_cannot_take_the_whole_budget():
    """The degenerate case, and the one that produced the $792 fill: with
    nothing to compete against, the old fill handed over everything it had."""
    out = allocate([_m("only", 50.0)], BUDGET, floor=0.02, max_frac=FRAC)
    assert out["only"] == pytest.approx(CAP)


def test_the_capped_size_is_quotable():
    """A cap landing under the venue minimum would defund rather than
    diversify. $135 against a 100-share minimum still buys a real order."""
    assert shares_for(CAP, min_size=100) == 135


# --- redistribution ----------------------------------------------------------

# These use `share=0.5` -- T = $100 of competing depth -- so every market in
# the field clears the marginal floor on its own merits. At the default
# `share=0.05` above, T is $1,900 and a $20/day market returns 0.0105 against a
# 0.02 floor: it is refused by the FLOOR, not by the cap, and a redistribution
# test built on that field would be asserting the wrong mechanism.
def _fair(cid, daily):
    return _m(cid, daily, share=0.5)


def test_the_surplus_flows_to_the_next_eligible_markets():
    """The cap must SKIP a full market, not stop the loop. Clamping the output
    afterwards would bound the leader and throw the freed dollars away."""
    mk = [_fair("best", 50.0), _fair("second", 40.0), _fair("third", 30.0)]
    out = allocate(mk, BUDGET, floor=0.02, max_frac=FRAC)
    assert out["best"] == pytest.approx(CAP)
    assert out["second"] > 0.0, "surplus never reached the runner-up"
    assert out["third"] > 0.0


def test_capping_does_not_destroy_budget():
    """Same field, with and without the cap: the cap redistributes, it does not
    shrink the amount put to work.

    True only while the field has enough capacity to absorb the budget --
    10 markets at $135 is $1,350 against a $900 budget. With fewer eligible
    markets the floor legitimately leaves the remainder idle, which is
    `test_the_floor_still_stops_the_fill_before_the_cap_does`."""
    mk = [_fair(f"m{i}", 50.0 - 2 * i) for i in range(10)]
    uncapped = sum(allocate(mk, BUDGET, floor=0.02).values())
    capped = sum(allocate(mk, BUDGET, floor=0.02, max_frac=FRAC).values())
    assert uncapped == pytest.approx(BUDGET)
    assert capped == pytest.approx(uncapped)


def test_the_leaders_surplus_moves_to_the_rest_of_the_field():
    """The fix stated as the quantity that matters. Counting funded markets is
    the wrong instrument -- the floor already funds a wide field here -- so
    measure the concentration itself: the leader's take falls and every dollar
    it loses turns up elsewhere."""
    mk = [_fair("dominant", 50.0)] + [_fair(f"m{i}", 20.0 - i)
                                      for i in range(9)]
    before = allocate(mk, BUDGET, floor=0.02)
    after = allocate(mk, BUDGET, floor=0.02, max_frac=FRAC)

    assert max(before.values()) > CAP          # 210 uncapped
    assert max(after.values()) == pytest.approx(CAP)
    rest_before = sum(before.values()) - before["dominant"]
    rest_after = sum(after.values()) - after["dominant"]
    assert rest_after > rest_before


def test_the_floor_still_stops_the_fill_before_the_cap_does():
    """Redistribution is not an instruction to empty the budget. Capital under
    the marginal floor is worse than idle capital, and the cap must not have
    turned the floor into a suggestion."""
    mk = [_m("good", 50.0), _m("dud", 0.001, share=0.001)]
    out = allocate(mk, BUDGET, floor=0.02, max_frac=FRAC)
    assert out["dud"] == 0.0
    assert sum(out.values()) < BUDGET


# --- the promotion path, which bypasses the water-fill -----------------------

def test_an_indivisible_lot_over_the_cap_is_not_funded():
    """`allocate_fundable` promotes a market to its venue minimum OUTSIDE the
    water-fill. Without the cap applied there too, a market whose minimum
    exceeds the concentration limit would be handed that minimum in full --
    the same bug through a different door."""
    mk = [_m("huge_min", 40.0, min_dollars=400.0)]
    out = allocate_fundable(mk, BUDGET, floor=0.02, payout_floor=1.5,
                            max_frac=FRAC)
    assert out["huge_min"] == 0.0


def test_a_lot_inside_the_cap_is_still_promoted():
    """The cap must not defund the ordinary case: a $100 venue minimum sits
    comfortably inside a $135 limit and the promotion still runs."""
    mk = [_m("normal_min", 40.0, min_dollars=100.0)]
    out = allocate_fundable(mk, BUDGET, floor=0.02, payout_floor=1.5,
                            max_frac=FRAC)
    assert out["normal_min"] >= 100.0
    assert out["normal_min"] <= CAP


def test_allocate_fundable_respects_the_bound_across_a_mixed_field():
    mk = [_m("a", 50.0, min_dollars=100.0), _m("b", 40.0, min_dollars=50.0),
          _m("c", 30.0, min_dollars=200.0), _m("d", 20.0, min_dollars=100.0)]
    out = allocate_fundable(mk, BUDGET, floor=0.02, payout_floor=1.5,
                            max_frac=FRAC)
    assert max(out.values()) <= CAP + 1e-9


# --- backward compatibility --------------------------------------------------

def test_the_default_is_unchanged_behaviour():
    """max_frac defaults to 1.0 so every existing caller -- and every test in
    test_allocate.py -- sees exactly the old allocator."""
    mk = [_m("dominant", 50.0), _m("second", 12.0)]
    assert allocate(mk, BUDGET, 0.02) == allocate(mk, BUDGET, 0.02,
                                                  max_frac=1.0)


# --- wired through the fleet -------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    monkeypatch.setenv("MAKER_DB", str(tmp_path / "conc.db"))


def _spec(cid, daily, min_size=100):
    return {"cid": cid, "title": "t", "slug": "s", "daily": daily,
            "min_size": min_size, "max_spread": 4.5, "tick": 0.001,
            "shares": 120, "est_income": 5.0, "est_capital": 120.0,
            "return_pct_day": 4.0, "their_score": 100.0}


def _state(cid, daily, theirs, base):
    st = MarketState(_spec(cid, daily), base)
    st.spec["_live"] = {"share": 0.0, "ours": 0.0, "theirs": theirs,
                        "income": 0.0, "capital": 0}
    st.cfg = replace(st.cfg, quote_shares=0)
    st.observe_theirs(0.0, theirs, window_sec=1800.0)
    return st


def test_the_fleet_runner_passes_the_configured_fraction():
    """End to end: `reallocate` reads `max_market_frac` off config, so no
    market can be sized past it in shares either. This is the number that
    became the 900-share order."""
    base = load_cfg()
    states = [_state("dominant", 500.0, 300.0, base)] + [
        _state(f"m{i}", 40.0, 600.0, base) for i in range(6)]
    sizes = reallocate(states, base)
    cap_shares = base.allocation_budget * base.max_market_frac
    assert max(sizes.values()) <= cap_shares
    assert max(sizes.values()) < 900, "the 900-share order is still reachable"


def test_the_configured_fraction_is_a_real_constraint():
    """Pins the config invariant the tests above assume. A fraction of 1.0
    would silently restore the old winner-take-all allocator."""
    cfg = load_cfg()
    assert 0.0 < cfg.max_market_frac < 1.0
    assert cfg.allocation_budget * cfg.max_market_frac <= cfg.max_cost_per_market
