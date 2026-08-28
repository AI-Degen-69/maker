"""Unit tests for strategy/live_pairs.py — Stage 3 naked exit.

Stage 3 invariants:
1. The trigger fires at pair_cost >= max_pair_cost and holds one tick below it.
2. Cancel precedes sell. A failed cancel aborts before any sell is sent.
3. State is re-read between cancel and sell. A pair that completed in that
   window is routed to merge, never sold.
4. The sell size never exceeds what the registry says is held.
5. Registry/venue position divergence refuses the exit rather than acting on a
   view the venue does not share.
6. No network in any test.
"""

import uuid
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from engine.order_registry import (
    OrderRegistry, OrderRecord, FillRecord, QuoteRecord,
)
from engine import live_pairs as lp


MAX_PAIR_COST = 0.995
TOK_UP = "tok-up"
TOK_DN = "tok-dn"
COND = "0xcond-stage3"


@pytest.fixture
def registry(tmp_path: Path) -> OrderRegistry:
    return OrderRegistry(db_path=tmp_path / "live.db")


def _one_sided_pair(registry: OrderRegistry, filled_size: float = 10.0,
                    fill_price: float = 0.60) -> str:
    """A heavy UP leg fully filled, a light DOWN leg still resting."""
    pair_id = "pair-1"
    now = 1_000_000

    heavy = OrderRecord(
        id=str(uuid.uuid4()), order_id="venue-heavy", condition_id=COND,
        token_id=TOK_UP, side="BUY", price=fill_price, original_size=filled_size,
        status="filled", posted_ts=now, last_polled_ts=now, pair_id=pair_id,
        max_pair_cost_at_post=MAX_PAIR_COST,
    )
    registry.create_order(heavy)
    registry.record_fill(FillRecord(
        trade_id="trade-heavy", order_uuid=heavy.id, size=filled_size,
        price=fill_price, venue_ts=now,
    ))

    light = OrderRecord(
        id=str(uuid.uuid4()), order_id="venue-light", condition_id=COND,
        token_id=TOK_DN, side="BUY", price=0.38, original_size=filled_size,
        status="open", posted_ts=now, last_polled_ts=now, pair_id=pair_id,
        max_pair_cost_at_post=MAX_PAIR_COST,
    )
    registry.create_order(light)

    # The fleet logs a quote row for every order with its UP/DOWN side -- the
    # quotes ledger is how the exit resolves which leg a token is (the closes
    # table encodes the sold leg via up_price/dn_price, not a token id).
    registry.log_quote(QuoteRecord(
        ts=now / 1000.0, condition_id=COND, token_id=TOK_UP, side="UP",
        price=fill_price, size=filled_size,
    ))
    registry.log_quote(QuoteRecord(
        ts=now / 1000.0, condition_id=COND, token_id=TOK_DN, side="DOWN",
        price=0.38, size=filled_size,
    ))
    return pair_id


class FakeClient:
    """Records every venue call. No network."""

    def __init__(self, best_ask=0.40, best_bid=0.55, cancel_ok=True,
                 bid_depth=100.0, ask_depth=100.0, bid_levels=None,
                 venue_matched=None, get_order_ok=True, tick_size="0.01"):
        self.best_ask = best_ask
        self.best_bid = best_bid
        self.cancel_ok = cancel_ok
        self.bid_depth = bid_depth
        self.ask_depth = ask_depth
        self.bid_levels = bid_levels
        self.tick_size = tick_size
        # What the VENUE says each order has matched. Deliberately separate from
        # the registry: the whole point of the post-cancel read is that the two
        # can disagree until a reconcile pass lands.
        self.venue_matched = dict(venue_matched or {})
        self.get_order_ok = get_order_ok
        self.calls: list[str] = []
        self.orders: list[dict] = []
        self.creds = object()

    def get_order_book(self, token_id):
        self.calls.append(f"book:{token_id}")
        asks = ([] if self.best_ask is None
                else [{"price": str(self.best_ask), "size": str(self.ask_depth)}])
        bids = (self.bid_levels if self.bid_levels is not None
                else [{"price": str(self.best_bid), "size": str(self.bid_depth)}])
        return {
            "asset_id": token_id,
            "bids": bids,
            "asks": asks,
            "tick_size": self.tick_size,
        }

    def get_order(self, order_id):
        self.calls.append(f"get_order:{order_id}")
        if not self.get_order_ok:
            raise RuntimeError("venue order read failed")
        return {"orderID": order_id,
                "size_matched": self.venue_matched.get(order_id, 0.0)}

    def cancel_order(self, payload):
        self.calls.append(f"cancel:{getattr(payload, 'orderID', payload)}")
        if not self.cancel_ok:
            raise RuntimeError("venue refused the cancel")
        return {"canceled": ["venue-light"]}

    def create_and_post_market_order(self, order_args, options=None,
                                     order_type="FOK", defer_exec=False):
        verb = "sell" if order_args.side == "SELL" else "buy"
        self.calls.append(
            f"{verb}:{order_args.token_id}:{order_args.amount}:{order_args.side}"
        )
        # Kept structured as well as stringified: `amount` means shares on a
        # SELL and USDC on a BUY, and a test that only reads the string cannot
        # tell those apart.
        self.orders.append({
            "side": order_args.side,
            "token_id": order_args.token_id,
            "amount": order_args.amount,
            "price": getattr(order_args, "price", None),
        })
        return {"success": True, "orderID": f"venue-{verb}"}


# ---------------------------------------------------------------------------
# Invariant 1 — the trigger
# ---------------------------------------------------------------------------


def test_trigger_fires_at_the_cap():
    """0.60 fill + 0.395 ask == 0.995 == max_pair_cost. `>=`, not `>`."""
    assert lp.should_exit(fill_cost=0.60, light_ask=0.395,
                          max_pair_cost=MAX_PAIR_COST) is True


def test_trigger_holds_one_tick_below_the_cap():
    assert lp.should_exit(fill_cost=0.60, light_ask=0.394,
                          max_pair_cost=MAX_PAIR_COST) is False


def test_trigger_fires_when_the_light_leg_has_no_ask():
    """No ask at all is not a completable pair; it is a naked leg."""
    assert lp.should_exit(fill_cost=0.60, light_ask=None,
                          max_pair_cost=MAX_PAIR_COST) is True


# ---------------------------------------------------------------------------
# Invariant 2 — cancel before sell, and a failed cancel aborts
# ---------------------------------------------------------------------------


def test_cancel_precedes_the_sell(registry: OrderRegistry):
    pair_id = _one_sided_pair(registry)
    client = FakeClient(best_ask=0.40)

    result = lp.exit_naked_leg(client, registry, pair_id,
                               max_pair_cost=MAX_PAIR_COST, live=True)

    assert result["action"] == "exited"
    cancel_i = next(i for i, c in enumerate(client.calls) if c.startswith("cancel:"))
    sell_i = next(i for i, c in enumerate(client.calls) if c.startswith("sell:"))
    assert cancel_i < sell_i, "selling first leaves a resting order that can refill"


def test_a_failed_cancel_aborts_before_any_sell(registry: OrderRegistry):
    pair_id = _one_sided_pair(registry)
    client = FakeClient(best_ask=0.40, cancel_ok=False)

    with pytest.raises(lp.PairExitRefused, match="cancel"):
        lp.exit_naked_leg(client, registry, pair_id,
                          max_pair_cost=MAX_PAIR_COST, live=True)

    assert not any(c.startswith("sell:") for c in client.calls)


# ---------------------------------------------------------------------------
# Invariant 3 — re-read between cancel and sell
# ---------------------------------------------------------------------------


def test_a_pair_that_completed_between_cancel_and_sell_routes_to_merge(
    registry: OrderRegistry,
):
    """The cancel can race a match that already happened.

    Market-selling one leg of a now-complete pair converts a position worth
    $1.00 at merge into a realized loss. That is the worst outcome on this path.
    """
    pair_id = _one_sided_pair(registry)
    light = next(o for o in registry.get_active_orders() if o.token_id == TOK_DN)

    class RacingClient(FakeClient):
        def cancel_order(self, payload):
            out = super().cancel_order(payload)
            # The other leg filled while the cancel was in flight. Only the
            # VENUE knows -- the registry learns at the next reconcile pass,
            # which is exactly the window this test covers.
            self.venue_matched["venue-light"] = 10.0
            return out

    client = RacingClient(best_ask=0.40)
    assert light is not None
    result = lp.exit_naked_leg(client, registry, pair_id,
                               max_pair_cost=MAX_PAIR_COST, live=True)

    assert result["action"] == "route_to_merge"
    assert not any(c.startswith("sell:") for c in client.calls)


# ---------------------------------------------------------------------------
# Invariant 4 — never sell more than the registry says is held
# ---------------------------------------------------------------------------


def test_sell_size_is_capped_by_the_registry(registry: OrderRegistry):
    pair_id = _one_sided_pair(registry, filled_size=10.0)
    client = FakeClient(best_ask=0.40, bid_depth=1_000.0)

    result = lp.exit_naked_leg(client, registry, pair_id,
                               max_pair_cost=MAX_PAIR_COST, live=True)

    assert result["size"] == pytest.approx(10.0)
    sell_call = next(c for c in client.calls if c.startswith("sell:"))
    assert sell_call.split(":")[2] == "10.0"


def test_sell_size_is_capped_by_bid_depth(registry: OrderRegistry):
    """Depth below the held size is a partial exit, not an oversized one."""
    pair_id = _one_sided_pair(registry, filled_size=10.0)
    client = FakeClient(best_ask=0.40, bid_depth=4.0)

    result = lp.exit_naked_leg(client, registry, pair_id,
                               max_pair_cost=MAX_PAIR_COST, live=True)

    assert result["size"] == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# Invariant 5 — registry/venue divergence refuses
# ---------------------------------------------------------------------------


def test_position_divergence_refuses_the_exit(registry: OrderRegistry):
    """The Data API says we hold less than the registry believes.

    Selling the registry's number would be selling shares we may not have.
    """
    pair_id = _one_sided_pair(registry, filled_size=10.0)
    client = FakeClient(best_ask=0.40)

    with pytest.raises(lp.PairExitRefused, match="diverge"):
        lp.exit_naked_leg(client, registry, pair_id,
                          max_pair_cost=MAX_PAIR_COST, live=True,
                          venue_positions={TOK_UP: 3.0})

    assert not any(c.startswith("cancel:") for c in client.calls)
    assert not any(c.startswith("sell:") for c in client.calls)


def test_matching_positions_allow_the_exit(registry: OrderRegistry):
    pair_id = _one_sided_pair(registry, filled_size=10.0)
    client = FakeClient(best_ask=0.40)

    result = lp.exit_naked_leg(client, registry, pair_id,
                               max_pair_cost=MAX_PAIR_COST, live=True,
                               venue_positions={TOK_UP: 10.0})
    assert result["action"] == "exited"


def test_divergence_check_tolerates_float_dust(registry: OrderRegistry):
    pair_id = _one_sided_pair(registry, filled_size=10.0)
    client = FakeClient(best_ask=0.40)

    result = lp.exit_naked_leg(client, registry, pair_id,
                               max_pair_cost=MAX_PAIR_COST, live=True,
                               venue_positions={TOK_UP: 10.0 - 1e-9})
    assert result["action"] == "exited"


# ---------------------------------------------------------------------------
# Dry run and no-trigger paths
# ---------------------------------------------------------------------------


def test_below_the_cap_holds_and_sends_nothing(registry: OrderRegistry):
    pair_id = _one_sided_pair(registry, fill_price=0.60)
    client = FakeClient(best_ask=0.30)  # 0.60 + 0.30 = 0.90, well under

    result = lp.exit_naked_leg(client, registry, pair_id,
                               max_pair_cost=MAX_PAIR_COST, live=True)

    assert result["action"] == "hold"
    assert not any(c.startswith(("cancel:", "sell:")) for c in client.calls)


def test_dry_run_sends_no_venue_writes(registry: OrderRegistry):
    pair_id = _one_sided_pair(registry)
    client = FakeClient(best_ask=0.40)

    result = lp.exit_naked_leg(client, registry, pair_id,
                               max_pair_cost=MAX_PAIR_COST, live=False)

    assert result["action"] == "would_exit"
    assert not any(c.startswith(("cancel:", "sell:")) for c in client.calls)


def test_a_balanced_pair_is_not_an_exit_candidate(registry: OrderRegistry):
    pair_id = _one_sided_pair(registry)
    light = next(o for o in registry.get_active_orders() if o.token_id == TOK_DN)
    registry.record_fill(FillRecord(
        trade_id="trade-light-pre", order_uuid=light.id, size=10.0,
        price=0.38, venue_ts=1_000_050,
    ))
    registry.update_order_status(light.id, "filled", 1_000_050)

    client = FakeClient(best_ask=0.40)
    result = lp.exit_naked_leg(client, registry, pair_id,
                               max_pair_cost=MAX_PAIR_COST, live=True)

    assert result["action"] == "balanced"
    assert not any(c.startswith(("cancel:", "sell:")) for c in client.calls)


# ---------------------------------------------------------------------------
# Stage 4 — second-leg completion
#
# Crossing to complete a half-open pair reduces exposure: the result is worth
# $1.00 at merge. It must never do the stop-loss's job badly, so it refuses any
# cross that would push the pair past the cap.
# ---------------------------------------------------------------------------


def test_completion_crosses_when_the_pair_stays_under_the_cap(registry: OrderRegistry):
    pair_id = _one_sided_pair(registry, filled_size=10.0, fill_price=0.60)
    client = FakeClient(best_ask=0.30)  # 0.60 + 0.30 = 0.90 < 0.995

    result = lp.complete_pair(client, registry, pair_id,
                              max_pair_cost=MAX_PAIR_COST, live=True)

    assert result["action"] == "completed"
    assert result["size"] == pytest.approx(10.0)
    assert result["pair_cost"] == pytest.approx(0.90)
    buy = next(c for c in client.calls if c.startswith("buy:"))
    assert buy.split(":")[1] == TOK_DN


def test_completion_refuses_a_cross_that_breaches_the_cap(registry: OrderRegistry):
    """That is the stop-loss's job, and this path must not do it badly."""
    pair_id = _one_sided_pair(registry, filled_size=10.0, fill_price=0.60)
    client = FakeClient(best_ask=0.40)  # 0.60 + 0.40 = 1.00 >= 0.995

    with pytest.raises(lp.PairCompletionRefused, match="max_pair_cost"):
        lp.complete_pair(client, registry, pair_id,
                         max_pair_cost=MAX_PAIR_COST, live=True)

    assert not any(c.startswith("buy:") for c in client.calls)


def test_completion_sizes_from_fills_not_from_intent(registry: OrderRegistry):
    """A leg that filled 4 of 10 is a 4-share position.

    Completing 10 would open 6 shares of fresh exposure on the other side --
    the opposite of what this path is for.
    """
    pair_id = "pair-partial"
    now = 1_000_000
    heavy = OrderRecord(
        id=str(uuid.uuid4()), order_id="venue-heavy-p", condition_id=COND,
        token_id=TOK_UP, side="BUY", price=0.60, original_size=10.0,
        status="partial", posted_ts=now, last_polled_ts=now, pair_id=pair_id,
        max_pair_cost_at_post=MAX_PAIR_COST,
    )
    registry.create_order(heavy)
    registry.record_fill(FillRecord(
        trade_id="trade-partial", order_uuid=heavy.id, size=4.0,
        price=0.60, venue_ts=now,
    ))
    light = OrderRecord(
        id=str(uuid.uuid4()), order_id="venue-light-p", condition_id=COND,
        token_id=TOK_DN, side="BUY", price=0.30, original_size=10.0,
        status="open", posted_ts=now, last_polled_ts=now, pair_id=pair_id,
        max_pair_cost_at_post=MAX_PAIR_COST,
    )
    registry.create_order(light)

    client = FakeClient(best_ask=0.30)
    result = lp.complete_pair(client, registry, pair_id,
                              max_pair_cost=MAX_PAIR_COST, live=True)

    assert result["size"] == pytest.approx(4.0)


def test_completion_is_capped_by_max_order_usd(registry: OrderRegistry):
    """The Stage 1 notional cap applies to this order like any other."""
    pair_id = _one_sided_pair(registry, filled_size=10.0, fill_price=0.60)
    client = FakeClient(best_ask=0.30)

    with pytest.raises(lp.PairCompletionRefused, match="MAX_ORDER_USD"):
        lp.complete_pair(client, registry, pair_id,
                         max_pair_cost=MAX_PAIR_COST, live=True,
                         max_order_usd=1.00)  # 10 * 0.30 = $3.00


def test_completion_is_capped_by_ask_depth(registry: OrderRegistry):
    pair_id = _one_sided_pair(registry, filled_size=10.0, fill_price=0.60)
    client = FakeClient(best_ask=0.30, ask_depth=3.0)

    result = lp.complete_pair(client, registry, pair_id,
                              max_pair_cost=MAX_PAIR_COST, live=True)
    assert result["size"] == pytest.approx(3.0)


def test_completion_refuses_without_an_ask(registry: OrderRegistry):
    pair_id = _one_sided_pair(registry, filled_size=10.0, fill_price=0.60)
    client = FakeClient(best_ask=None)

    with pytest.raises(lp.PairCompletionRefused, match="no ask"):
        lp.complete_pair(client, registry, pair_id,
                         max_pair_cost=MAX_PAIR_COST, live=True)


def test_completion_dry_run_sends_nothing(registry: OrderRegistry):
    pair_id = _one_sided_pair(registry, filled_size=10.0, fill_price=0.60)
    client = FakeClient(best_ask=0.30)

    result = lp.complete_pair(client, registry, pair_id,
                              max_pair_cost=MAX_PAIR_COST, live=False)

    assert result["action"] == "would_complete"
    assert not any(c.startswith("buy:") for c in client.calls)


def test_a_balanced_pair_needs_no_completion(registry: OrderRegistry):
    pair_id = _one_sided_pair(registry)
    light = next(o for o in registry.get_active_orders() if o.token_id == TOK_DN)
    registry.record_fill(FillRecord(
        trade_id="trade-light-done", order_uuid=light.id, size=10.0,
        price=0.30, venue_ts=1_000_050,
    ))
    registry.update_order_status(light.id, "filled", 1_000_050)

    client = FakeClient(best_ask=0.30)
    result = lp.complete_pair(client, registry, pair_id,
                              max_pair_cost=MAX_PAIR_COST, live=True)

    assert result["action"] == "balanced"
    assert not any(c.startswith("buy:") for c in client.calls)


def test_a_completed_pair_reads_as_held_on_both_legs(registry: OrderRegistry):
    """The acceptance condition: merge's pre-flight must see both legs.

    Completion is only worth doing if the result is mergeable, so the check is
    on the registry's own view of holdings per token, which is what the merge
    pre-flight reconciles against.
    """
    pair_id = _one_sided_pair(registry, filled_size=10.0, fill_price=0.60)
    light = next(o for o in registry.get_active_orders() if o.token_id == TOK_DN)
    client = FakeClient(best_ask=0.30)

    lp.complete_pair(client, registry, pair_id,
                     max_pair_cost=MAX_PAIR_COST, live=True)
    # The venue fill arrives through reconcile; simulate that landing.
    registry.record_fill(FillRecord(
        trade_id="trade-completion", order_uuid=light.id, size=10.0,
        price=0.30, venue_ts=1_000_200,
    ))

    pair = lp.load_pair(registry, pair_id)
    assert pair["naked"] == pytest.approx(0.0)
    assert pair["heavy"]["matched"] == pytest.approx(10.0)
    assert pair["light"]["matched"] == pytest.approx(10.0)


def test_completion_buy_amount_is_usdc_not_shares(registry: OrderRegistry):
    """MarketOrderArgsV2.amount is the maker amount -- USDC on a BUY.

    The SDK computes shares received as amount / price, so passing a share
    count submits a much larger buy than intended: 10 shares at $0.30 becomes a
    $10.00 order acquiring ~33 shares, which is 23 shares of fresh exposure on
    the leg this path exists to close. Every guard above the send validates the
    $3.00 we meant, so nothing else catches the unit.
    """
    pair_id = _one_sided_pair(registry, filled_size=10.0, fill_price=0.60)
    client = FakeClient(best_ask=0.30)

    result = lp.complete_pair(client, registry, pair_id,
                              max_pair_cost=MAX_PAIR_COST, live=True)

    sent = next(o for o in client.orders if o["side"] == "BUY")
    assert sent["amount"] == pytest.approx(3.00)            # 10 shares * $0.30
    assert sent["amount"] == pytest.approx(result["size"] * result["ask"])
    assert sent["amount"] != pytest.approx(result["size"])  # not the share count
    assert sent["price"] == pytest.approx(0.30)


def test_exit_sell_amount_stays_in_shares(registry: OrderRegistry):
    """The mirror of the BUY case: on a SELL, amount is the share count.

    Same field, opposite unit. Asserted so a future fix to one side cannot
    quietly convert the other.
    """
    pair_id = _one_sided_pair(registry, filled_size=10.0)
    client = FakeClient(best_ask=0.40)

    lp.exit_naked_leg(client, registry, pair_id,
                      max_pair_cost=MAX_PAIR_COST, live=True)

    sent = next(o for o in client.orders if o["side"] == "SELL")
    assert sent["amount"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Review round 2 — the holes the first pass left
# ---------------------------------------------------------------------------


def test_exit_cancels_working_orders_on_the_heavy_leg_too(registry: OrderRegistry):
    """A `partial` heavy leg still has working size that can refill after the sell."""
    pair_id = _one_sided_pair(registry, filled_size=10.0)
    now = 1_000_500
    extra = OrderRecord(
        id=str(uuid.uuid4()), order_id="venue-heavy-2", condition_id=COND,
        token_id=TOK_UP, side="BUY", price=0.60, original_size=5.0,
        status="partial", posted_ts=now, last_polled_ts=now, pair_id=pair_id,
        max_pair_cost_at_post=MAX_PAIR_COST,
    )
    registry.create_order(extra)

    client = FakeClient(best_ask=0.40)
    result = lp.exit_naked_leg(client, registry, pair_id,
                               max_pair_cost=MAX_PAIR_COST, live=True)

    assert "venue-heavy-2" in result["cancelled"]
    cancel_idx = [i for i, c in enumerate(client.calls) if c.startswith("cancel:")]
    sell_idx = next(i for i, c in enumerate(client.calls) if c.startswith("sell:"))
    assert max(cancel_idx) < sell_idx, "every working order must be quiet before the sell"


def test_exit_refuses_when_the_venue_order_read_fails(registry: OrderRegistry):
    """An unreadable order is not an unfilled one."""
    pair_id = _one_sided_pair(registry)
    client = FakeClient(best_ask=0.40, get_order_ok=False)

    with pytest.raises(lp.PairExitRefused, match="venue state"):
        lp.exit_naked_leg(client, registry, pair_id,
                          max_pair_cost=MAX_PAIR_COST, live=True)

    assert not any(c.startswith("sell:") for c in client.calls)


def test_exit_bounds_the_sell_price_and_counts_only_acceptable_depth(
    registry: OrderRegistry,
):
    """Depth below the slippage floor is not depth we would accept.

    Best bid 0.55, floor 0.53. The 40 shares resting at 0.40 must not be
    counted, and the submitted price must be the floor, not the best bid.
    """
    pair_id = _one_sided_pair(registry, filled_size=10.0)
    client = FakeClient(best_ask=0.40, bid_levels=[
        {"price": "0.55", "size": "3"},
        {"price": "0.54", "size": "2"},
        {"price": "0.40", "size": "40"},
    ])

    result = lp.exit_naked_leg(client, registry, pair_id,
                               max_pair_cost=MAX_PAIR_COST, live=True)

    assert result["size"] == pytest.approx(5.0)        # 3 + 2, not 45
    assert result["min_price"] == pytest.approx(0.53)  # 0.55 - 0.02, on tick
    sent = next(o for o in client.orders if o["side"] == "SELL")
    assert sent["price"] == pytest.approx(0.53)


def test_completion_cancels_the_resting_light_buy_before_crossing(
    registry: OrderRegistry,
):
    """Otherwise the maker BUY and the taker BUY can both fill and double the leg."""
    pair_id = _one_sided_pair(registry, filled_size=10.0, fill_price=0.60)
    client = FakeClient(best_ask=0.30)

    result = lp.complete_pair(client, registry, pair_id,
                              max_pair_cost=MAX_PAIR_COST, live=True)

    assert "venue-light" in result["cancelled"]
    cancel_i = next(i for i, c in enumerate(client.calls) if c.startswith("cancel:"))
    buy_i = next(i for i, c in enumerate(client.calls) if c.startswith("buy:"))
    assert cancel_i < buy_i


def test_completion_shrinks_to_the_remainder_after_a_partial_race(
    registry: OrderRegistry,
):
    """If the light leg partly filled during the cancel, cross only the rest."""
    pair_id = _one_sided_pair(registry, filled_size=10.0, fill_price=0.60)
    client = FakeClient(best_ask=0.30, venue_matched={"venue-light": 6.0})

    result = lp.complete_pair(client, registry, pair_id,
                              max_pair_cost=MAX_PAIR_COST, live=True)

    assert result["size"] == pytest.approx(4.0)
    sent = next(o for o in client.orders if o["side"] == "BUY")
    assert sent["amount"] == pytest.approx(4.0 * 0.30)


def test_completion_is_a_no_op_when_the_leg_filled_during_the_cancel(
    registry: OrderRegistry,
):
    pair_id = _one_sided_pair(registry, filled_size=10.0, fill_price=0.60)
    client = FakeClient(best_ask=0.30, venue_matched={"venue-light": 10.0})

    result = lp.complete_pair(client, registry, pair_id,
                              max_pair_cost=MAX_PAIR_COST, live=True)

    assert result["action"] == "balanced"
    assert not any(c.startswith("buy:") for c in client.calls)


def test_a_venue_surplus_does_not_block_the_exit(registry: OrderRegistry):
    """Holding more than the registry believes cannot cause an oversell.

    The same token may be held by another pair, or part of a position already
    merged. Refusing here would block the one action that closes exposure.
    """
    pair_id = _one_sided_pair(registry, filled_size=10.0)
    client = FakeClient(best_ask=0.40)

    result = lp.exit_naked_leg(client, registry, pair_id,
                               max_pair_cost=MAX_PAIR_COST, live=True,
                               venue_positions={TOK_UP: 25.0})
    assert result["action"] == "exited"


def test_a_token_absent_from_the_positions_read_refuses(registry: OrderRegistry):
    """Absence is not zero -- it is equally consistent with a filtered read."""
    pair_id = _one_sided_pair(registry, filled_size=10.0)
    client = FakeClient(best_ask=0.40)

    with pytest.raises(lp.PairExitRefused, match="no position at all"):
        lp.exit_naked_leg(client, registry, pair_id,
                          max_pair_cost=MAX_PAIR_COST, live=True,
                          venue_positions={"some-other-token": 5.0})

    assert not any(c.startswith("cancel:") for c in client.calls)


# ---------------------------------------------------------------------------
# Review round 3
# ---------------------------------------------------------------------------


def test_tick_floor_does_not_drop_a_whole_tick_on_binary_float_error():
    """`0.29 / 0.01` is 28.999999999999996 in binary floating point.

    A bare int() floors an already-aligned price one whole tick lower, which
    moves the sell bound below what we chose and lets depth_at_or_above count an
    extra level. Safe in direction, wrong in value.
    """
    assert lp._floor_to_tick(0.29, 0.01) == pytest.approx(0.29)
    assert lp._floor_to_tick(0.53, 0.01) == pytest.approx(0.53)
    assert lp._floor_to_tick(0.535, 0.01) == pytest.approx(0.53)
    assert lp._floor_to_tick(0.07, 0.001) == pytest.approx(0.07)


def test_completion_refusals_use_the_completion_exception(registry: OrderRegistry):
    """A failed cancel on the completion path must not raise the exit's type.

    `complete_pair_cmd` catches only PairCompletionRefused. An escaping
    PairExitRefused would fall to its generic handler, mark the audit row
    `interrupted` although nothing was sent, and block every later completion on
    that condition until --force.
    """
    pair_id = _one_sided_pair(registry, filled_size=10.0, fill_price=0.60)
    client = FakeClient(best_ask=0.30, cancel_ok=False)

    with pytest.raises(lp.PairCompletionRefused, match="Cancel"):
        lp.complete_pair(client, registry, pair_id,
                         max_pair_cost=MAX_PAIR_COST, live=True)

    assert not any(c.startswith("buy:") for c in client.calls)


def test_completion_venue_read_failure_uses_the_completion_exception(
    registry: OrderRegistry,
):
    pair_id = _one_sided_pair(registry, filled_size=10.0, fill_price=0.60)
    client = FakeClient(best_ask=0.30, get_order_ok=False)

    with pytest.raises(lp.PairCompletionRefused, match="venue state"):
        lp.complete_pair(client, registry, pair_id,
                         max_pair_cost=MAX_PAIR_COST, live=True)

    assert not any(c.startswith("buy:") for c in client.calls)


def test_exit_refusals_still_use_the_exit_exception(registry: OrderRegistry):
    """The default is unchanged; only the completion path overrides it."""
    pair_id = _one_sided_pair(registry)
    client = FakeClient(best_ask=0.40, cancel_ok=False)

    with pytest.raises(lp.PairExitRefused):
        lp.exit_naked_leg(client, registry, pair_id,
                          max_pair_cost=MAX_PAIR_COST, live=True)


# ---------------------------------------------------------------------------
# Review round 4 — the audit row must not block the recovery it recommends
# ---------------------------------------------------------------------------


def test_route_to_merge_does_not_leave_the_condition_blocked(
    registry: OrderRegistry, tmp_path, monkeypatch
):
    """`route_to_merge` sends no SELL, so it must not hold the condition open.

    `exit_pair` prints "now run merge" on this branch. If the audit row were
    marked `submitted`, `_check_idempotency_guard` would then refuse that merge
    until --force -- a guard blocking the recovery it just recommended.
    """
    from engine import live_exec

    monkeypatch.setattr(live_exec, "RUN", tmp_path)
    monkeypatch.setattr(live_exec, "client", lambda *a, **k: None)

    pair_id = _one_sided_pair(registry)
    light = next(o for o in registry.get_active_orders() if o.token_id == TOK_DN)

    def fake_exit(*args, **kwargs):
        return {"action": "route_to_merge", "pair_id": pair_id,
                "condition_id": COND, "cancelled": [light.order_id], "size": 0.0}

    monkeypatch.setattr(live_exec, "exit_naked_leg", fake_exit, raising=False)
    monkeypatch.setattr("engine.live_pairs.exit_naked_leg", fake_exit)

    live_exec.exit_pair(pair_id, live=True, db_path=registry.db_path,
                        skip_positions_check=True)

    # The guard must let the recommended merge through without --force.
    live_exec._check_idempotency_guard(COND, force=False)


def test_a_real_exit_does_hold_the_condition(registry: OrderRegistry, tmp_path,
                                             monkeypatch):
    """The mirror: a sell that actually went out must block a second one."""
    from engine import live_exec

    monkeypatch.setattr(live_exec, "RUN", tmp_path)
    monkeypatch.setattr(live_exec, "client", lambda *a, **k: None)

    pair_id = _one_sided_pair(registry)

    def fake_exit(*args, **kwargs):
        return {"action": "exited", "pair_id": pair_id, "condition_id": COND,
                "size": 10.0, "cancelled": []}

    monkeypatch.setattr("engine.live_pairs.exit_naked_leg", fake_exit)

    live_exec.exit_pair(pair_id, live=True, db_path=registry.db_path,
                        skip_positions_check=True)

    with pytest.raises(SystemExit):
        live_exec._check_idempotency_guard(COND, force=False)


def test_complete_pair_cmd_runs_end_to_end_and_holds_after_a_cross(
    registry: OrderRegistry, tmp_path, monkeypatch
):
    """The Stage 4 command has its own config load, audit row and result handling.

    It carried the same `from engine.config import Config` failure as the exit
    and was fixed alongside it, but the round-four tests only drove `exit_pair`.
    Fixing one entry point and testing the other is how that defect survived
    thirty-seven passing tests in the first place.
    """
    from engine import live_exec

    monkeypatch.setattr(live_exec, "RUN", tmp_path)
    monkeypatch.setattr(live_exec, "client", lambda *a, **k: None)

    pair_id = _one_sided_pair(registry, filled_size=10.0, fill_price=0.60)

    def fake_complete(*args, **kwargs):
        # Proves the call site was reached with a real max_pair_cost, which is
        # what the missing config import used to break before any work started.
        assert kwargs["max_pair_cost"] > 0
        return {"action": "completed", "pair_id": pair_id, "condition_id": COND,
                "size": 10.0, "notional": 3.0}

    monkeypatch.setattr("engine.live_pairs.complete_pair", fake_complete)

    live_exec.complete_pair_cmd(pair_id, live=True, db_path=registry.db_path,
                                skip_positions_check=True)

    # A cross went out, so the condition is held against a second unforced one.
    with pytest.raises(SystemExit):
        live_exec._check_idempotency_guard(COND, force=False)


def test_complete_pair_cmd_does_not_hold_the_condition_when_nothing_crossed(
    registry: OrderRegistry, tmp_path, monkeypatch
):
    """`balanced` sends no BUY, so it must not block a later merge or completion."""
    from engine import live_exec

    monkeypatch.setattr(live_exec, "RUN", tmp_path)
    monkeypatch.setattr(live_exec, "client", lambda *a, **k: None)

    pair_id = _one_sided_pair(registry, filled_size=10.0, fill_price=0.60)

    def fake_complete(*args, **kwargs):
        return {"action": "balanced", "pair_id": pair_id, "condition_id": COND,
                "size": 0.0}

    monkeypatch.setattr("engine.live_pairs.complete_pair", fake_complete)

    live_exec.complete_pair_cmd(pair_id, live=True, db_path=registry.db_path,
                                skip_positions_check=True)

    live_exec._check_idempotency_guard(COND, force=False)


def test_an_unknown_pair_id_refuses_cleanly_on_both_commands(tmp_path, monkeypatch):
    """A typo must produce the refusal message, not a traceback.

    Found by running the commands rather than by a test: `load_pair` raises
    before either command's try block, and the completion path does not
    otherwise catch the exit path's exception type.
    """
    from engine import live_exec

    monkeypatch.setattr(live_exec, "RUN", tmp_path)
    monkeypatch.setattr(live_exec, "client", lambda *a, **k: None)
    db = tmp_path / "empty.db"

    with pytest.raises(SystemExit, match="EXIT REFUSED"):
        live_exec.exit_pair("no-such-pair", live=False, db_path=db,
                            skip_positions_check=True)

    with pytest.raises(SystemExit, match="COMPLETION REFUSED"):
        live_exec.complete_pair_cmd("no-such-pair", live=False, db_path=db,
                                    skip_positions_check=True)


# ---------------------------------------------------------------------------
# Self-review round — heavy/light is a ranking, and rankings flip
# ---------------------------------------------------------------------------


def _overfilling_light_pair(registry: OrderRegistry) -> str:
    """Heavy UP 10 filled; light DOWN order for 12, not yet filled."""
    pair_id = "pair-overfill"
    now = 1_000_000
    heavy = OrderRecord(
        id=str(uuid.uuid4()), order_id="venue-heavy", condition_id=COND,
        token_id=TOK_UP, side="BUY", price=0.60, original_size=10.0,
        status="filled", posted_ts=now, last_polled_ts=now, pair_id=pair_id,
        max_pair_cost_at_post=MAX_PAIR_COST,
    )
    registry.create_order(heavy)
    registry.record_fill(FillRecord(trade_id="t-heavy", order_uuid=heavy.id,
                                    size=10.0, price=0.60, venue_ts=now))
    light = OrderRecord(
        id=str(uuid.uuid4()), order_id="venue-light", condition_id=COND,
        token_id=TOK_DN, side="BUY", price=0.30, original_size=12.0,
        status="open", posted_ts=now, last_polled_ts=now, pair_id=pair_id,
        max_pair_cost_at_post=MAX_PAIR_COST,
    )
    registry.create_order(light)
    registry.log_quote(QuoteRecord(
        ts=now / 1000.0, condition_id=COND, token_id=TOK_UP, side="UP",
        price=0.60, size=10.0,
    ))
    registry.log_quote(QuoteRecord(
        ts=now / 1000.0, condition_id=COND, token_id=TOK_DN, side="DOWN",
        price=0.30, size=12.0,
    ))
    return pair_id


def test_exit_survives_the_light_leg_overtaking_the_heavy_one(
    registry: OrderRegistry,
):
    """`heavy` and `light` are a ranking, and the ranking flips.

    If the light leg fills past the heavy one and reconcile records it, a second
    load_pair swaps the two. Deriving `naked` from the post-cancel ranking then
    subtracts a venue reading for one token from a registry reading for another,
    and the exit reports a complete pair while shares are still naked.
    """
    pair_id = _overfilling_light_pair(registry)
    light = next(o for o in registry.get_active_orders() if o.token_id == TOK_DN)

    class OvertakingClient(FakeClient):
        def cancel_order(self, payload):
            out = super().cancel_order(payload)
            # The light leg fills 12 -- more than the heavy leg's 10 -- and a
            # reconcile pass lands before the re-read.
            self.venue_matched["venue-light"] = 12.0
            registry.record_fill(FillRecord(
                trade_id="t-light", order_uuid=light.id, size=12.0,
                price=0.30, venue_ts=1_000_100,
            ))
            registry.update_order_status(light.id, "filled", 1_000_100)
            return out

    client = OvertakingClient(best_ask=0.40)

    # UP 10 against DOWN 12 is NOT a closed pair -- two DOWN shares are naked.
    # Reporting route_to_merge here would tell the operator to merge 10 pairs
    # and walk away from an open position on the other token.
    with pytest.raises(lp.PairExitRefused, match="changed token"):
        lp.exit_naked_leg(client, registry, pair_id,
                          max_pair_cost=MAX_PAIR_COST, live=True)

    assert not any(c.startswith("sell:") for c in client.calls)


def test_naked_after_keys_on_tokens_not_on_rank():
    """The reduction itself, isolated from the venue plumbing."""
    after = {"legs": {
        TOK_UP: {"token_id": TOK_UP, "matched": 10.0, "notional": 6.0},
        TOK_DN: {"token_id": TOK_DN, "matched": 12.0, "notional": 3.6},
    }}
    naked, fill_cost, unpriced = lp._naked_after(
        after, TOK_UP, TOK_DN, venue_light_extra=0.0)
    # Signed: negative means the ORIGINAL LIGHT token is the naked one.
    assert naked == pytest.approx(-2.0)
    assert fill_cost == pytest.approx(0.60)
    assert unpriced == pytest.approx(0.0)

    # And with the roles as originally ranked, the arithmetic is unchanged.
    naked2, _, _ = lp._naked_after(after, TOK_DN, TOK_UP, venue_light_extra=0.0)
    assert naked2 == pytest.approx(2.0)

    # Venue-only heavy size is reported as unpriced: we can see the shares but
    # not what they cost, so nothing that needs an average may use them.
    naked3, _, unpriced3 = lp._naked_after(
        after, TOK_UP, TOK_DN, venue_light_extra=0.0, venue_heavy_extra=3.0)
    assert naked3 == pytest.approx(1.0)   # (10 + 3) - 12
    assert unpriced3 == pytest.approx(3.0)


def test_completion_cancels_the_working_heavy_leg_too(registry: OrderRegistry):
    """A resting heavy order refills the pair right after the cross balances it."""
    pair_id = _one_sided_pair(registry, filled_size=10.0, fill_price=0.60)
    now = 1_000_500
    extra = OrderRecord(
        id=str(uuid.uuid4()), order_id="venue-heavy-2", condition_id=COND,
        token_id=TOK_UP, side="BUY", price=0.60, original_size=5.0,
        status="partial", posted_ts=now, last_polled_ts=now, pair_id=pair_id,
        max_pair_cost_at_post=MAX_PAIR_COST,
    )
    registry.create_order(extra)

    client = FakeClient(best_ask=0.30)
    result = lp.complete_pair(client, registry, pair_id,
                              max_pair_cost=MAX_PAIR_COST, live=True)

    assert "venue-heavy-2" in result["cancelled"]


def test_completion_rechecks_the_cap_after_the_cancel(registry: OrderRegistry):
    """The approved pair_cost was measured before the cancel.

    A heavy order filling at a worse price in that window pushes the real pair
    past the cap, and the cross must refuse rather than create the losing pair
    the cap exists to prevent.
    """
    pair_id = _one_sided_pair(registry, filled_size=10.0, fill_price=0.60)
    heavy = next(o for o in registry.get_orders_by_pair(pair_id)
                 if o.token_id == TOK_UP)

    class WorseningClient(FakeClient):
        def cancel_order(self, payload):
            out = super().cancel_order(payload)
            # 5 more heavy shares at 0.90 lift the average well past the cap.
            registry.record_fill(FillRecord(
                trade_id="t-heavy-2", order_uuid=heavy.id, size=5.0,
                price=0.90, venue_ts=1_000_200,
            ))
            return out

    client = WorseningClient(best_ask=0.30)
    with pytest.raises(lp.PairCompletionRefused, match="max_pair_cost"):
        lp.complete_pair(client, registry, pair_id,
                         max_pair_cost=MAX_PAIR_COST, live=True)

    assert not any(c.startswith("buy:") for c in client.calls)


def test_a_pair_spanning_three_tokens_refuses(registry: OrderRegistry):
    """Reducing three legs to the largest two would size against a partial view."""
    pair_id = _one_sided_pair(registry)
    now = 1_000_600
    third = OrderRecord(
        id=str(uuid.uuid4()), order_id="venue-third", condition_id=COND,
        token_id="tok-third", side="BUY", price=0.20, original_size=5.0,
        status="open", posted_ts=now, last_polled_ts=now, pair_id=pair_id,
        max_pair_cost_at_post=MAX_PAIR_COST,
    )
    registry.create_order(third)

    with pytest.raises(lp.PairExitRefused, match="token ids"):
        lp.load_pair(registry, pair_id)


def test_completion_refuses_when_a_heavy_fill_is_visible_only_at_the_venue(
    registry: OrderRegistry,
):
    """A matched-size read carries no execution price, and the cap is about price.

    Only the venue moves here; the registry is untouched, which is the actual
    live race. Simulating it by writing to the registry would test the wrong
    thing, because the registry is exactly the source that lags.
    """
    pair_id = _one_sided_pair(registry, filled_size=10.0, fill_price=0.60)
    now = 1_000_500
    extra = OrderRecord(
        id=str(uuid.uuid4()), order_id="venue-heavy-2", condition_id=COND,
        token_id=TOK_UP, side="BUY", price=0.60, original_size=5.0,
        status="partial", posted_ts=now, last_polled_ts=now, pair_id=pair_id,
        max_pair_cost_at_post=MAX_PAIR_COST,
    )
    registry.create_order(extra)

    client = FakeClient(best_ask=0.30, venue_matched={"venue-heavy-2": 5.0})

    with pytest.raises(lp.PairCompletionRefused, match="has none yet"):
        lp.complete_pair(client, registry, pair_id,
                         max_pair_cost=MAX_PAIR_COST, live=True)

    assert not any(c.startswith("buy:") for c in client.calls)


def test_completion_refuses_when_the_light_leg_overtakes(registry: OrderRegistry):
    """Stage 4 mirror of the exit case: negative naked is not `balanced`."""
    pair_id = _overfilling_light_pair(registry)

    class OvertakingClient(FakeClient):
        def cancel_order(self, payload):
            out = super().cancel_order(payload)
            self.venue_matched["venue-light"] = 12.0
            return out

    client = OvertakingClient(best_ask=0.30)

    with pytest.raises(lp.PairCompletionRefused, match="exceeds"):
        lp.complete_pair(client, registry, pair_id,
                         max_pair_cost=MAX_PAIR_COST, live=True)

    assert not any(c.startswith("buy:") for c in client.calls)


# ---------------------------------------------------------------------------
# Stage 4.5 pre-flight — `quote` must be visible to everything downstream
# ---------------------------------------------------------------------------


def test_quote_writes_both_legs_to_the_registry_under_one_pair_id(
    tmp_path, monkeypatch, capsys
):
    """Without this the poll loop has nothing to reconcile and exit/complete
    have no pair_id, so the two legs rest at the venue with real money and
    nothing tracking them."""
    from engine import live_exec

    monkeypatch.setattr(live_exec, "RUN", tmp_path)
    db = tmp_path / "live.db"

    class Market:
        market_slug = "btc-up-or-down-5m-x"
        up_token = "tok-up-live"
        down_token = "tok-dn-live"
        tick_size = "0.01"
        neg_risk = False

    monkeypatch.setattr("engine.markets.fetch_pinned_market",
                        lambda cid, require_rewards=True: Market())

    posted = []

    class Client:
        creds = object()

        def create_order(self, args):
            return MagicMock(tokenId=args.token_id, token=args.token_id)

        def post_orders(self, batch_args, post_only=False, **kwargs):
            res = []
            for arg in batch_args:
                tok = getattr(arg.order, "tokenId", None) or getattr(arg.order, "token", None)
                posted.append(tok)
                res.append({"orderID": f"venue-{tok}", "success": True})
            return res

        def get_order(self, order_id):
            tok = order_id.replace("venue-", "")
            return {"asset_id": tok}

        def get_open_orders(self, *a, **k):
            return []

    monkeypatch.setattr(live_exec, "client", lambda *a, **k: Client())

    live_exec.quote(COND, price=0.48, size=5.0, live=True, db_path=db)

    registry = OrderRegistry(db_path=db)
    pair_ids = {o.pair_id for o in registry.get_active_orders()}
    assert len(pair_ids) == 1, "both legs must share one pair_id"
    pair_id = pair_ids.pop()

    orders = registry.get_orders_by_pair(pair_id)
    assert len(orders) == 2
    assert {o.token_id for o in orders} == {"tok-up-live", "tok-dn-live"}
    # The venue id came back, so each row is `open` and reconcilable by id.
    assert all(o.order_id and o.status == "open" for o in orders)
    # And the operator is told the id rather than having to open live.db.
    assert pair_id in capsys.readouterr().out


def test_quote_leaves_the_row_pending_when_the_venue_returns_no_id(
    tmp_path, monkeypatch
):
    """The order may be live and simply unnamed.

    Guessing an id would bind our row to somebody else's order; `pending` is
    what reconcile's orphan adoption is built to claim.
    """
    from engine import live_exec

    monkeypatch.setattr(live_exec, "RUN", tmp_path)
    db = tmp_path / "live.db"

    class Market:
        market_slug = "m"
        up_token = "tok-up-live"
        down_token = "tok-dn-live"
        tick_size = "0.01"
        neg_risk = False

    monkeypatch.setattr("engine.markets.fetch_pinned_market",
                        lambda cid, require_rewards=True: Market())

    class Client:
        creds = object()

        def create_order(self, args):
            return MagicMock(tokenId=args.token_id, token=args.token_id)

        def post_orders(self, batch_args, post_only=False, **kwargs):
            return [{"success": True}, {"success": True}]          # no id of any spelling

        def get_open_orders(self, *a, **k):
            return []

    monkeypatch.setattr(live_exec, "client", lambda *a, **k: Client())

    live_exec.quote(COND, price=0.48, size=5.0, live=True, db_path=db)

    registry = OrderRegistry(db_path=db)
    orders = registry.get_active_orders()
    assert len(orders) == 2
    assert all(o.order_id is None and o.status == "pending" for o in orders)


def test_venue_order_id_accepts_the_spellings_and_refuses_to_guess():
    from engine import live_exec

    assert live_exec.venue_order_id({"orderID": "a"}) == "a"
    assert live_exec.venue_order_id({"orderId": "b"}) == "b"
    assert live_exec.venue_order_id({"order_id": "c"}) == "c"
    assert live_exec.venue_order_id({"success": True}) is None
    assert live_exec.venue_order_id(None) is None


def test_quote_says_why_a_market_was_rejected(monkeypatch, tmp_path):
    """An unfunded market quotes; an unusable one is refused, and says why.

    Rewards are not the income. Measured on run/fleet.db: 476 merge closes,
    +$1,172.35, mean pair cost $0.96006, against rebate accrual of roughly
    $0.22/day. Every market the ranker graduates pays zero rewards, so demanding
    them here refused the fleet's entire universe. What still earns a refusal is
    a market that is missing, closed, not accepting orders, or does not carry
    exactly two tokens -- and the message must say so rather than sending the
    operator hunting for a typo in a condition_id that is perfectly correct.
    """
    from engine import live_exec

    monkeypatch.setattr(live_exec, "RUN", tmp_path)

    class Market:
        market_slug = "some-unfunded-market"
        up_token = "u"
        down_token = "d"
        tick_size = "0.01"
        neg_risk = False

    # Pays no rewards -- which is every market the ranker graduates. It quotes,
    # and the dry run prints the legs it would rest.
    seen = {}

    def unfunded(cid, require_rewards=True):
        seen["require_rewards"] = require_rewards
        return None if require_rewards else Market()

    monkeypatch.setattr("engine.markets.fetch_pinned_market", unfunded)
    live_exec.quote(COND, price=0.48, size=5.0, live=False,
                    db_path=tmp_path / "live.db")
    assert seen["require_rewards"] is False

    # Unusable for a reason that is not funding: refused, and the message names
    # the causes that are left rather than blaming the id.
    monkeypatch.setattr("engine.markets.fetch_pinned_market",
                        lambda cid, require_rewards=True: None)
    with pytest.raises(SystemExit, match="no tradeable market") as exc_info:
        live_exec.quote(COND, price=0.48, size=5.0, live=False,
                        db_path=tmp_path / "live.db")
    msg = str(exc_info.value)
    assert COND[:12] in msg
    assert "not accepting orders" in msg


def test_taker_paths_never_set_post_only(registry):
    """exit_naked_leg and complete_pair are deliberate taker crosses and must NEVER pass post_only=True."""
    pair_id = _one_sided_pair(registry, filled_size=10.0, fill_price=0.60)
    client = FakeClient(best_ask=0.395, best_bid=0.55)

    # 1. Test exit_naked_leg (taker SELL)
    res_exit = lp.exit_naked_leg(
        client, registry, pair_id,
        max_pair_cost=MAX_PAIR_COST,
        live=True,
    )
    assert res_exit["action"] == "exited"
    assert len(client.orders) == 1
    assert client.orders[0]["side"] == "SELL"

    # 2. Test complete_pair (taker BUY)
    pair_id_2 = "pair-comp-1"
    now = 1_000_000
    heavy = OrderRecord(
        id=str(uuid.uuid4()), order_id="venue-h2", condition_id=COND,
        token_id=TOK_UP, side="BUY", price=0.60, original_size=10.0,
        status="filled", posted_ts=now, last_polled_ts=now, pair_id=pair_id_2,
        max_pair_cost_at_post=MAX_PAIR_COST,
    )
    registry.create_order(heavy)
    registry.record_fill(FillRecord(
        trade_id="trade-h2", order_uuid=heavy.id, size=10.0,
        price=0.60, venue_ts=now,
    ))
    light = OrderRecord(
        id=str(uuid.uuid4()), order_id="venue-l2", condition_id=COND,
        token_id=TOK_DN, side="BUY", price=0.38, original_size=10.0,
        status="open", posted_ts=now, last_polled_ts=now, pair_id=pair_id_2,
        max_pair_cost_at_post=MAX_PAIR_COST,
    )
    registry.create_order(light)

    client_comp = FakeClient(best_ask=0.35, best_bid=0.55)
    res_comp = lp.complete_pair(
        client_comp, registry, pair_id_2,
        max_pair_cost=MAX_PAIR_COST,
        live=True,
    )
    assert res_comp["action"] == "completed"
    assert len(client_comp.orders) == 1
    assert client_comp.orders[0]["side"] == "BUY"
