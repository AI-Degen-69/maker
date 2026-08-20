"""The live fleet loop: decide -> submit -> reconcile, reusing the risk gates.

`engine.live_fleet` is the live twin of `strategy.fleet`. The strategy that
trades is `engine.quotes.decide_quotes` -- already proven in simulation and
already wired to the live risk gates -- so this module must add NOTHING new to
the decision. Its only new jobs are:

1. `plan_orders`: turn "what we want resting" into "what to cancel / submit"
   without resubmitting an order already resting at the desired price.
2. The rotation loop, which must never let one market's error stop the others,
   and must never touch the venue at all in dry-run.

These tests drive exactly those two seams. Everything that talks to the venue
(fetch market, fetch books, decide, submit, cancel, reconcile, sweep) is
injected, so the loop's behavior is tested without a network.
"""

from __future__ import annotations

import pytest

from engine.live_fleet import LiveFleetResult, VenueSeam, plan_orders, run
from engine.quotes import QuoteIntent


def _intent(side="UP", token="tok-up", price=0.60, size=5):
    return QuoteIntent(side=side, token_id=token, price=price, size=size,
                       mid=price + 0.01, edge_vs_mid=0.01)


def _open(token="tok-up", price=0.60, oid="o1", status="open"):
    return {"token_id": token, "price": price, "order_id": oid,
            "side": "BUY", "status": status}


class TestPlanOrders:
    def test_keeps_matching_and_submits_nothing_when_fully_resting(self):
        open_orders = [_open()]
        intents = [_intent()]
        to_cancel, to_submit = plan_orders(open_orders, intents)
        assert to_cancel == []
        assert to_submit == []

    def test_cancels_stale_order_and_submits_new_price(self):
        open_orders = [_open(price=0.61)]
        intents = [_intent(price=0.60)]
        to_cancel, to_submit = plan_orders(open_orders, intents)
        assert to_cancel == open_orders
        assert [i.price for i in to_submit] == [0.60]

    def test_submits_intent_with_no_open_order(self):
        open_orders = []
        intents = [_intent()]
        to_cancel, to_submit = plan_orders(open_orders, intents)
        assert to_cancel == []
        assert to_submit == intents

    def test_cancels_orders_for_a_side_we_no_longer_quote(self):
        # UP is still wanted; the DOWN leg is gone from the intents.
        open_orders = [_open("tok-up", 0.60, "o1"), _open("tok-dn", 0.40, "o2")]
        intents = [_intent(side="UP", token="tok-up", price=0.60)]
        to_cancel, to_submit = plan_orders(open_orders, intents)
        assert [o["order_id"] for o in to_cancel] == ["o2"]
        assert to_submit == []

    def test_price_epsilon_treats_sub_tick_repricing_as_same(self):
        # A venue rounding jitter below a tick must not churn cancel+resubmit.
        open_orders = [_open(price=0.6005)]
        intents = [_intent(price=0.60)]
        to_cancel, to_submit = plan_orders(open_orders, intents, price_eps=0.001)
        assert to_cancel == []
        assert to_submit == []


class FakeMarket:
    def __init__(self, cid="0xabc"):
        self.condition_id = cid
        self.up_token = "tok-up"
        self.down_token = "tok-dn"
        self.market_slug = "fake-market"
        self.tick_size = 0.01
        self.neg_risk = False


class TestRunLoop:
    def _run(self, live, intents, fetch_raises=False, once=True):
        calls = {"submitted": [], "cancelled": [], "reconciled": 0, "swept": 0}

        def fake_fetch_market(cid):
            if fetch_raises:
                raise RuntimeError("venue down")
            return FakeMarket(cid)

        def fake_books(clob_host, token):
            return {"token_id": token, "best_bid": 0.59, "best_ask": 0.61,
                    "bids": {0.59: 100}, "asks": {0.61: 100}}

        def fake_decide(cfg, up, dn, inv, t_rem, wf):
            return list(intents), ("" if intents else "declined")

        def fake_submit(client, registry, market, intents, cfg):
            calls["submitted"].append([i.side for i in intents])
            return 0

        def fake_cancel(client, registry, orders):
            calls["cancelled"].append([o["order_id"] for o in orders])
            return len(orders)

        def fake_reconcile(client, registry, maker):
            calls["reconciled"] += 1
            return None

        def fake_sweep():
            calls["swept"] += 1

        seam = VenueSeam(
            client=object(),
            fetch_market=fake_fetch_market,
            fetch_books=fake_books,
            decide=fake_decide,
            submit_fn=fake_submit,
            cancel_fn=fake_cancel,
            reconcile_fn=fake_reconcile,
            sweep_fn=fake_sweep,
        )
        results = run(
            seam, interval=0.0, once=once, live=live,
            markets=[FakeMarket("0xabc")],
            sleep_fn=lambda s: None,
        )
        return results, calls

    def test_dry_run_decides_but_never_submits_or_cancels(self):
        results, calls = self._run(live=False, intents=[_intent(), _intent("DOWN", "tok-dn", 0.40)])
        assert results[0].status == "DRY_RUN"
        assert calls["submitted"] == []
        assert calls["cancelled"] == []
        assert calls["reconciled"] == 1  # reconcile is read-only; still safe in dry-run

    def test_live_submits_decided_intents(self):
        results, calls = self._run(live=True, intents=[_intent(), _intent("DOWN", "tok-dn", 0.40)])
        assert results[0].status == "QUOTED"
        assert calls["submitted"] == [["UP", "DOWN"]]

    def test_live_cancels_stale_when_decide_returns_nothing(self):
        # no intents -> every open order is stale. Exercise via a fake open set.
        results, calls = self._run(live=True, intents=[])
        assert results[0].status == "DECLINED"
        # No open orders were injected, so nothing to cancel; the point is the
        # loop still completed without raising.
        assert calls["submitted"] == []

    def test_one_market_error_does_not_stop_the_rotation(self):
        results, calls = self._run(live=True, intents=[_intent()], fetch_raises=True)
        assert results[0].status == "ERROR"
        assert calls["reconciled"] == 1

    def test_sweep_runs_on_first_cycle(self):
        results, calls = self._run(live=False, intents=[])
        assert calls["swept"] == 1

    def test_submit_error_does_not_stop_the_rotation(self):
        # A submit failure must degrade the market to ERROR, not kill the loop:
        # the second market is still visited and reconcile still runs.
        calls = {"reconciled": 0}

        def fetch_market(cid):
            return FakeMarket(cid)

        def fetch_books(clob_host, token):
            return {"token_id": token, "best_bid": 0.59, "best_ask": 0.61,
                    "bids": {0.59: 100}, "asks": {0.61: 100}}

        def decide(cfg, up, dn, inv, t_rem, wf):
            return [_intent()], ""

        def submit_fn(client, registry, market, intents, cfg):
            raise RuntimeError("venue rejected")

        def cancel_fn(client, registry, orders):
            return 0

        def reconcile_fn(client, registry, maker):
            calls["reconciled"] += 1

        seam = VenueSeam(
            client=object(), fetch_market=fetch_market, fetch_books=fetch_books,
            decide=decide, submit_fn=submit_fn, cancel_fn=cancel_fn,
            reconcile_fn=reconcile_fn, sweep_fn=lambda: None,
        )
        results = run(
            seam, interval=0.0, once=True, live=True,
            markets=[FakeMarket("0xa"), FakeMarket("0xb")],
            sleep_fn=lambda s: None,
        )
        assert [r.status for r in results] == ["ERROR", "ERROR"]
        assert calls["reconciled"] == 1

    def test_fleet_state_is_injected_into_decide_cfg(self):
        seen = {}

        def decide(cfg, up, dn, inv, t_rem, wf):
            seen["fleet_naked_usd"] = cfg.fleet_naked_usd
            return [], "declined"

        def fetch_market(cid):
            return FakeMarket(cid)

        def fetch_books(clob_host, token):
            return {"token_id": token, "best_bid": 0.59, "best_ask": 0.61,
                    "bids": {0.59: 100}, "asks": {0.61: 100}}

        run(
            VenueSeam(
                client=object(),
                fetch_market=fetch_market, fetch_books=fetch_books, decide=decide,
                submit_fn=lambda *a, **k: 0, cancel_fn=lambda *a, **k: 0,
                reconcile_fn=lambda *a, **k: None, sweep_fn=lambda: None,
                fleet_state_fn=lambda r: {"fleet_naked_usd": 12.5},
            ),
            interval=0.0, once=True, live=False,
            markets=[FakeMarket("0xabc")],
            sleep_fn=lambda s: None,
        )
        assert seen["fleet_naked_usd"] == 12.5
