"""The allocator must be able to fund a market it defunded (U6).

Measured on run/fleet.db, a 13.4h forward run: reward samples with a non-zero
`our_score` decayed monotonically -- 67/219 in the first ten minutes, 9/190 by
hour seven, then 0/190 for the last 5.5 hours straight. The last quote of the
run went out at T+8.1h. Nothing had crashed and the books were still being
sampled; the fleet had simply defunded every market and had no way back.

The latch was two gates keyed on measurements that only exist WHILE quoting:

  * `_live["income"]` is `share * daily`, and `share` is our score in a book we
    are no longer resting in -- zero by construction once defunded.
  * `_live["capital"]` sums our open orders -- also zero by construction.

So the first gate defunds a market for earning nothing, and the second
(`if not share or not capital: continue`) then drops that same market out of
the observation set entirely, where it can never be reconsidered. One-way.

The competition's depth is the input that survives: `market_score` is scored
over the whole book and is measured whether or not we participate. Every
market in the stalled run still had a live `theirs` reading (1,504 on Taylor
Swift at the moment the fleet was quoting nothing at all). Sizing off that,
rather than off our own absent orders, is what makes the decision reversible.
"""
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.config import load as load_cfg          # noqa: E402
from strategy.fleet import MarketState, reallocate    # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    monkeypatch.setenv("MAKER_DB", str(tmp_path / "refund.db"))


def _spec(cid="c1", daily=50, min_size=100, shares=120):
    return {"cid": cid, "title": "t", "slug": "s", "daily": daily,
            "min_size": min_size, "max_spread": 4.5, "tick": 0.001,
            "shares": shares, "est_income": 5.0, "est_capital": 120.0,
            "return_pct_day": 4.0, "their_score": 100.0}


def _defunded(cid="c1", theirs=612.0, daily=50, base=None, min_size=100):
    """A market in exactly the state run/fleet.db ended in: quoting nothing,
    so share/capital/income all read zero, but the competition still measured.
    """
    st = MarketState(_spec(cid=cid, daily=daily, min_size=min_size),
                     base or load_cfg())
    st.spec["_live"] = {"share": 0.0, "ours": 0.0, "theirs": theirs,
                        "income": 0.0, "capital": 0}
    st.cfg = replace(st.cfg, quote_shares=0)
    st.observe_theirs(0.0, theirs, window_sec=1800.0)
    return st


def test_a_defunded_market_is_refunded_once_it_can_pay_again():
    """THE LATCH. Spider-Man's real numbers off run/fleet.db: a $50/day pot
    against 612 of competing score, i.e. $1,983 of competing depth. The
    water-fill funds it to $245, which is $5.50/day -- 3.6x the payout floor.

    The market earned nothing at the instant it was measured only because we
    were not in the book. Judging it on that reading is judging it on its own
    punishment.
    """
    base = load_cfg()
    st = _defunded()
    assert reallocate([st], base).get("c1", 0) > 0
    assert st.cfg.quote_shares > 0


def test_a_defunded_market_that_still_cannot_pay_stays_unfunded():
    """The floor has to keep working in the other direction, or the fix is
    just a removal of the floor. 400,000 of competing score against a $50 pot
    is under $1.50/day at any size the budget can reach."""
    base = load_cfg()
    st = _defunded(cid="crowded", theirs=400_000.0)
    assert reallocate([st], base).get("crowded", 0) == 0
    assert st.cfg.quote_shares == 0


def test_eligibility_is_judged_at_the_size_actually_funded():
    """Income is monotone in size, so a fixed probe answers the wrong
    question. Spider-Man earns $2.40/day at its 100-share minimum and
    $5.50/day at the $245 the water-fill gives it. Sizing must come from the
    allocation, not from whatever size the probe happened to pick."""
    base = load_cfg()
    st = _defunded()
    funded = reallocate([st], base).get("c1", 0)
    assert funded > 100, "sized at the minimum, not at the allocation"


def test_an_uncontested_market_is_promoted_to_its_minimum_lot():
    """Workhorse and Billie Eilish, both real: a $5/day pot with NO competing
    depth. T = 0 makes marginal() a step -- the first dollar takes the whole
    pot, the rest are worth nothing -- so the water-fill funds $5 and stops.
    $5 is unquotable under a 100-share minimum, and dropping it forfeits
    $5/day on $100, the best return on the board.

    An indivisible lot has to be judged on the lot's average return."""
    base = load_cfg()
    st = _defunded(cid="uncontested", theirs=0.0, daily=5)
    assert reallocate([st], base).get("uncontested", 0) == 100


def test_a_market_that_cannot_earn_its_minimum_lot_is_dropped():
    """The other side of the promotion: a lot only gets bought when the whole
    lot clears both floors. $1/day against $100 of minimum is 1%/day, under
    the 2%/day marginal floor, so the capital is better left idle."""
    base = load_cfg()
    st = _defunded(cid="thin", theirs=0.0, daily=1)
    out = reallocate([st], base)
    assert out.get("thin", 0) == 0
    assert st.cfg.quote_shares == 0


def test_a_promotion_never_overruns_the_budget():
    """Promotions are additive to the water-fill, so they are the one place
    the budget can be exceeded if the lot is not checked against what is left.
    """
    base = load_cfg()
    states = [_defunded(cid=f"m{i}", theirs=0.0, daily=50, min_size=500)
              for i in range(10)]
    out = reallocate(states, base)
    assert sum(out.values()) <= base.allocation_budget


def test_an_unmeasured_market_keeps_its_startup_size():
    """No `theirs` observation is not the same as a measured zero. A market we
    have never sampled must not be sized off a guess -- it keeps quoting at
    the size the ranker gave it until there is data."""
    base = load_cfg()
    st = MarketState(_spec(cid="fresh"), base)
    st.spec["_live"] = {"share": 0.0, "ours": 0.0, "income": 0.0, "capital": 0}
    out = reallocate([st], base)
    assert "fresh" not in out
    assert st.cfg.quote_shares == 120


def test_sizing_does_not_depend_on_whether_we_are_currently_resting():
    """The same market, same competition, measured once while quoting and once
    while defunded, must size the same. Any difference is the latch."""
    base = load_cfg()
    resting = MarketState(_spec(cid="c1"), base)
    resting.spec["_live"] = {"share": 120 * 0.3086 / (120 * 0.3086 + 612.0),
                             "ours": 120 * 0.3086, "theirs": 612.0,
                             "income": 5.0, "capital": 120.0}
    resting.observe_theirs(0.0, 612.0, window_sec=1800.0)

    assert reallocate([resting], base)["c1"] == reallocate([_defunded()],
                                                           base)["c1"]
