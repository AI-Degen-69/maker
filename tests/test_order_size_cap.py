"""The per-market cost cap has to bind on the ORDER, not on the inventory.

`max_cost_per_market` was enforced in one place, quotes.py:

    if inv.cost >= cfg.max_cost_per_market and mine >= theirs:

Both halves of that condition are post-hoc. `inv.cost` is what we ALREADY
hold, so a market holding nothing passes at any order size, and `mine >=
theirs` restricts the check to the heavy leg. Measured on run/fleet.db over
the 2026-08-02 run:

    dota2-rnx-yb1-2026-08-02-game2  UP  900sh @ 0.880 = $792.00  crossed=0

One resting order, one fill, $792 -- 79% of a $1,000 wallet and 1.98x a $400
cap, on a market whose inventory was $0.00 when the order went out. The cap
never had anything to bind against.

These tests pin the arithmetic that closes it: the size is chosen against
`max_cost_per_market - inv.cost` at the moment it is posted.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.config import load as load_cfg           # noqa: E402
from strategy.fleet import _affordable_rest_size       # noqa: E402


# --- the regression itself ---------------------------------------------------

def test_the_792_dollar_fill_is_now_arithmetically_impossible():
    """THE REGRESSION. Replays the exact order off run/fleet.db: 900 shares at
    0.880 into an empty market, with the whole $1,000 wallet available.

    The old code took `min(qi.size, available/price)`, and 900 * 0.88 = $792
    fits a $1,000 wallet, so nothing objected. The cap is now the binding
    term."""
    cfg = load_cfg()
    size = _affordable_rest_size(requested=900, price=0.880,
                                 available_usd=1000.0,
                                 market_room_usd=cfg.max_cost_per_market)
    assert size == 454                       # floor(400 / 0.88)
    assert size * 0.880 <= cfg.max_cost_per_market
    # And the quantity that actually mattered: notional, not share count.
    assert size * 0.880 < 792.0


def test_notional_never_exceeds_the_market_room():
    """The invariant, swept across the price band rather than asserted at one
    point. Share count is not the quantity being capped -- dollars are."""
    for price in (0.05, 0.30, 0.42, 0.50, 0.68, 0.88, 0.99):
        size = _affordable_rest_size(10_000, price, 1000.0, 400.0)
        assert size * price <= 400.0 + 1e-9, f"breached at price {price}"


# --- cap composes with existing inventory ------------------------------------

def test_room_shrinks_as_inventory_accumulates():
    """`market_room_usd` is max_cost_per_market - inv.cost. A market already
    holding $380 of a $400 cap may still quote, but only into the $20 left."""
    room = 400.0 - 380.0
    size = _affordable_rest_size(900, 0.50, available_usd=1000.0,
                                 market_room_usd=room)
    assert size == 40
    assert size * 0.50 <= room


def test_a_market_at_its_cap_gets_no_size_at_all():
    size = _affordable_rest_size(900, 0.50, available_usd=1000.0,
                                 market_room_usd=0.0)
    assert size == 0


def test_inventory_over_the_cap_floors_at_zero_not_negative():
    """An inventory past its cap has negative room. int() rounds toward zero,
    so an unclamped negative would come back as a positive-looking size."""
    size = _affordable_rest_size(900, 0.50, available_usd=1000.0,
                                 market_room_usd=-150.0)
    assert size == 0


# --- the wallet cap still binds independently --------------------------------

def test_the_wallet_cap_still_binds_when_it_is_the_tighter_one():
    """Fix 2 ADDS a constraint; it must not relax the committed-capital one.
    $30 of wallet against $400 of market room -- the wallet wins."""
    size = _affordable_rest_size(900, 0.50, available_usd=30.0,
                                 market_room_usd=400.0)
    assert size == 60
    assert size * 0.50 <= 30.0


def test_the_tighter_of_the_two_caps_always_wins():
    for available, room, bound in ((30.0, 400.0, 30.0),
                                   (1000.0, 400.0, 400.0),
                                   (250.0, 250.0, 250.0)):
        size = _affordable_rest_size(900, 0.50, available, room)
        assert size * 0.50 <= bound + 1e-9


def test_a_small_request_is_not_inflated_by_a_generous_cap():
    """The caps are ceilings, never targets. An allocator asking for 50 shares
    gets 50, not whatever the wallet could afford."""
    assert _affordable_rest_size(50, 0.50, 1000.0, 400.0) == 50


# --- degenerate inputs -------------------------------------------------------

def test_a_non_positive_price_yields_no_size():
    """Guards the division. A zero or negative price is an off-scale book, not
    an invitation to quote infinite size."""
    assert _affordable_rest_size(900, 0.0, 1000.0, 400.0) == 0
    assert _affordable_rest_size(900, -0.10, 1000.0, 400.0) == 0


def test_size_is_floored_never_rounded_up():
    """0.33 into $10 of room is 30.3 shares. Rounding up would breach the cap
    by a hair on every order, which compounds across a fleet."""
    size = _affordable_rest_size(900, 0.33, 1000.0, 10.0)
    assert size == 30
    assert size * 0.33 <= 10.0


# --- the config invariant behind the numbers ---------------------------------

def test_the_cap_is_a_real_constraint_against_the_wallet():
    """A per-market cap at or above the wallet cap could never bind. Every
    test above rests on that, so it is pinned here rather than left implicit
    in config.py's comments."""
    cfg = load_cfg()
    assert cfg.max_cost_per_market < cfg.max_committed_usd
