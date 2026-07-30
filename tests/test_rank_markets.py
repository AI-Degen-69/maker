"""Market selection tests (U4).

Two failures the 18.7h run exposed, both in how markets get chosen rather
than how they get quoted:

  * A market projecting under Polymarket's $1 minimum payout earns exactly
    zero, not a small amount. 16 of 20 fleet markets were in that state,
    holding capital and paying nothing.
  * Competing depth was read once. That single snapshot reported a competing
    score of 35 for a market that measured 3,727 live, and the top-ranked
    market went on to deliver $0.25/day against $18.96 projected.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.rank_markets import FLOOR_MULTIPLE, MIN_PAYOUT  # noqa: E402
from strategy.config import load as load_cfg                 # noqa: E402
from strategy.fleet import MarketState, reallocate           # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    """MarketState rehydrates inventory from the store, so every test needs a
    database of its own or one test's fills become another's position."""
    monkeypatch.setenv("MAKER_DB", str(tmp_path / "rank.db"))


def _spec(cid="c1", daily=50, title="t", min_size=50, shares=120):
    return {"cid": cid, "title": title, "slug": "s", "daily": daily,
            "min_size": min_size, "max_spread": 4.5, "tick": 0.01,
            "shares": shares, "est_income": 5.0, "est_capital": 120.0,
            "return_pct_day": 4.0, "their_score": 100.0}


def _state(cid="c1", income=5.0, share=0.05, capital=120.0, ours=10.0,
           daily=50, base=None):
    st = MarketState(_spec(cid=cid, daily=daily), base or load_cfg())
    st.spec["_live"] = {"income": income, "share": share,
                        "capital": capital, "ours": ours}
    return st


# --- the payout floor -------------------------------------------------------

def test_a_market_under_the_payout_floor_is_not_funded():
    """Below $1/day the venue pays nothing at all, so funding it commits
    capital for exactly zero income."""
    base = load_cfg()
    poor = _state(cid="poor", income=0.25, base=base)
    assert reallocate([poor], base).get("poor", 0) == 0


def test_a_market_above_the_floor_is_funded():
    base = load_cfg()
    rich = _state(cid="rich", income=9.0, base=base)
    assert reallocate([rich], base).get("rich", 0) > 0


def test_the_floor_carries_headroom_above_the_bare_minimum():
    """A market at exactly $1.00 sits on the line, and projections are noisy
    -- one more competitor puts it under. The multiple buys margin."""
    base = load_cfg()
    marginal = _state(cid="edge", income=1.0, base=base)
    assert reallocate([marginal], base).get("edge", 0) == 0
    assert base.reward_min_payout_usd * base.reward_floor_multiple > 1.0


def test_an_unfunded_market_is_still_tracked_not_dropped():
    """Unfunded means stop quoting, never stop tending. Its inventory still
    needs merging, marking out and reconciling."""
    base = load_cfg()
    poor = _state(cid="poor", income=0.25, base=base)
    rich = _state(cid="rich", income=9.0, base=base)
    states = [poor, rich]
    reallocate(states, base)
    assert len(states) == 2                       # nothing removed


def test_the_script_floor_and_the_fleet_floor_agree():
    """Two places compute the same rule; a drift between them would fund
    markets the ranker rejected, or vice versa."""
    base = load_cfg()
    assert MIN_PAYOUT == base.reward_min_payout_usd
    assert FLOOR_MULTIPLE == base.reward_floor_multiple


# --- averaged competitor depth ----------------------------------------------

def test_competitor_depth_is_averaged_over_the_window():
    st = _state()
    for ts, theirs in ((0.0, 35.0), (10.0, 3727.0), (20.0, 1800.0)):
        st.observe_theirs(ts, theirs, window_sec=1800.0)
    assert st.avg_theirs() == (35.0 + 3727.0 + 1800.0) / 3


def test_samples_outside_the_window_are_dropped():
    st = _state()
    st.observe_theirs(0.0, 1000.0, window_sec=100.0)
    st.observe_theirs(500.0, 20.0, window_sec=100.0)
    assert st.avg_theirs() == 20.0                # the stale reading is gone


def test_depth_is_unknown_rather_than_zero_before_any_observation():
    """None, not 0.0. An empty book is the most attractive input the allocator
    can receive, so guessing it would concentrate capital into exactly the
    markets we know least about."""
    assert _state().avg_theirs() is None


def test_allocation_uses_the_average_not_the_latest_snapshot():
    """The regression that motivated this: one lucky reading sized the whole
    position. A market whose depth has been consistently heavy must not be
    funded as though it were empty because of one thin sample."""
    base = load_cfg()
    st = _state(cid="m", income=9.0, ours=10.0, share=0.99)
    for ts, theirs in ((0.0, 5000.0), (10.0, 5000.0), (20.0, 1.0)):
        st.observe_theirs(ts, theirs, window_sec=1800.0)
    thin = _state(cid="m2", income=9.0, ours=10.0, share=0.99)
    thin.observe_theirs(0.0, 1.0, window_sec=1800.0)

    heavy_alloc = reallocate([st], base).get("m", 0)
    thin_alloc = reallocate([thin], base).get("m2", 0)
    # Same reported share, same income: only the averaged depth differs, and
    # the crowded market must not be sized like the empty one.
    assert heavy_alloc < thin_alloc
