"""Capital allocation by marginal return.

quote_shares was a flat 120 everywhere, producing returns from 27.58%/day to
0.28%/day on identical $115 stakes. The numbers below are the real measured
state of the fleet on 2026-07-29, so a regression here means the model has
drifted away from the venue.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.allocate import (  # noqa: E402
    allocate, capital_scarcity, competitor_depth, income, marginal)


# --- capital scarcity -------------------------------------------------------

def _mkts():
    """Two markets whose marginal return stays far above any sane floor, so a
    small budget is unambiguously the binding constraint."""
    return [{"cid": "a", "daily": 50.0, "capital": 115.0, "share": 0.6334},
            {"cid": "b", "daily": 100.0, "capital": 115.0, "share": 0.30}]


def test_exhausted_budget_with_a_market_still_paying_well_is_scarce():
    m = _mkts()
    alloc = allocate(m, 100.0, 0.02)
    assert sum(alloc.values()) == pytest.approx(100.0)
    assert capital_scarcity(m, alloc, 100.0, 0.02) is True


def test_an_unspent_budget_is_never_scarce():
    """The floor stopped the water-fill, not the budget. The money is idle by
    choice, so there is nothing a freed dollar would go and do."""
    m = _mkts()
    alloc = allocate(m, 100_000.0, 0.02)
    assert sum(alloc.values()) < 100_000.0
    assert capital_scarcity(m, alloc, 100_000.0, 0.02) is False


def test_scarcity_needs_a_return_well_above_the_floor_not_merely_above_it():
    """The multiple is what separates 'above the floor' from 'worth
    liquidating a pair for'. Same allocation, same budget, different verdict."""
    m = _mkts()
    alloc = allocate(m, 100.0, 0.02)
    assert capital_scarcity(m, alloc, 100.0, 0.02, multiple=2.0) is True
    assert capital_scarcity(m, alloc, 100.0, 0.02, multiple=1e6) is False


def test_no_markets_is_not_scarcity():
    assert capital_scarcity([], {}, 1200.0, 0.02) is False


def test_competitor_depth_inverts_the_share_formula():
    """Taylor Swift: 63.34% of the score on $115 in => ~$66.6 of competition."""
    assert competitor_depth(115.0, 0.6334) == pytest.approx(66.56, abs=0.5)


def test_income_matches_the_observed_market():
    """$50/day pot, $115 in, $66.6 against us -> the $31.67/day we measured."""
    assert income(115.0, 50.0, 66.56) == pytest.approx(31.67, abs=0.05)


def test_marginal_return_is_higher_on_the_thin_market():
    """The $50 pot beats the $100 pot because the $100 pot is crowded. Pot
    size is not the signal; competition is."""
    thin = marginal(115.0, 50.0, 66.56)        # Taylor Swift
    crowded = marginal(115.0, 100.0, 9631.0)   # Wesley Bell
    assert thin > crowded * 5


def test_allocator_drops_markets_below_the_floor():
    markets = [
        {"cid": "good", "daily": 50.0, "capital": 115.0, "share": 0.6334},
        {"cid": "bad", "daily": 100.0, "capital": 115.0, "share": 0.0118},
    ]
    out = allocate(markets, budget=1200.0, floor=0.02)
    assert out["bad"] == 0.0
    assert out["good"] > 0.0


def test_allocator_respects_the_budget():
    markets = [{"cid": f"m{i}", "daily": 50.0, "capital": 115.0,
                "share": 0.6334} for i in range(5)]
    out = allocate(markets, budget=1200.0, floor=0.02)
    assert sum(out.values()) <= 1200.0 + 1e-6


def test_allocator_never_pushes_past_the_pot_ceiling():
    """A $50/day pot cannot pay more than $50/day however much we commit, so
    the allocator must stop long before spending an unlimited budget."""
    markets = [{"cid": "one", "daily": 50.0, "capital": 115.0, "share": 0.6334}]
    out = allocate(markets, budget=100000.0, floor=0.02)
    T = competitor_depth(115.0, 0.6334)
    assert income(out["one"], 50.0, T) < 50.0
    assert out["one"] < 100000.0


def test_shares_for_converts_dollars_at_pair_cost():
    """A pair costs ~$1: one UP at p plus one DOWN at ~(1-p). So N dollars of
    committed capital buys ~N shares on each side, not N/price."""
    from strategy.allocate import shares_for
    assert shares_for(240.0, min_size=20) == 240


def test_shares_for_refuses_to_quote_under_the_market_minimum():
    """Quoting under a market's min_size scores exactly zero, so a sub-minimum
    allocation is worse than none -- it commits capital and earns nothing."""
    from strategy.allocate import shares_for
    assert shares_for(15.0, min_size=100) == 0


def test_shares_for_drops_a_zero_allocation():
    from strategy.allocate import shares_for
    assert shares_for(0.0, min_size=20) == 0


def test_sole_maker_does_not_divide_by_zero():
    """Crashed the live fleet at 13:40 on 2026-07-29.

    share >= 1.0 means we are the entire book, so competitor_depth returns
    T = 0. The water-fill then evaluates marginal(capital=0, T=0), which is
    daily * 0 / 0**2 -- ZeroDivisionError, and the whole bot died mid-sweep.
    """
    assert marginal(0.0, 50.0, 0.0) > 0        # must not raise
    # Once we hold size and face no competition we already take the whole pot,
    # so further capital earns nothing.
    assert marginal(100.0, 50.0, 0.0) == 0.0


def test_allocator_survives_a_market_we_completely_own():
    markets = [{"cid": "sole", "daily": 50.0, "capital": 115.0, "share": 1.0}]
    out = allocate(markets, budget=1200.0, floor=0.02)
    assert out["sole"] >= 0.0                  # must not raise


def test_idle_capital_beats_capital_under_the_floor():
    """Leftover budget is deliberately not forced out into bad markets."""
    markets = [{"cid": "bad", "daily": 5.0, "capital": 115.0, "share": 0.01}]
    out = allocate(markets, budget=5000.0, floor=0.02)
    assert sum(out.values()) < 5000.0
