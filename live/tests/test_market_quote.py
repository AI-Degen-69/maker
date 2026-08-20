"""The quote-one-market step: one copy of fetch -> books -> inventory -> decide.

Both production callers are adapters over the same four ports:
- live_exec.decide wires engine.markets + inventory_from_registry + decide_quotes
- the fleet wires its VenueSeam slots (fetch_market / fetch_books /
  inventory_fn / decide)

The step owns failure *detection*; each caller owns failure *presentation*.
"""
import pytest

from engine.config import load
from engine.quotes import (
    Inventory,
    MarketEval,
    MarketQuoteError,
    MarketUnavailable,
    decide_quotes,
    evaluate_market_quote,
)


def _cfg():
    return load()


def _books():
    up = {"best_bid": 0.52, "best_ask": 0.53,
          "bids": {0.52: 1000.0}, "asks": {0.53: 1000.0}}
    dn = {"best_bid": 0.47, "best_ask": 0.48,
          "bids": {0.47: 1000.0}, "asks": {0.48: 1000.0}}
    return up, dn


class _Market:
    def __init__(self, cid="0xabc", up="tok-up", dn="tok-dn"):
        self.condition_id = cid
        self.up_token = up
        self.down_token = dn
        self.market_slug = "test-market"
        self.tick_size = 0.01
        self.neg_risk = False


# ---------------------------------------------------------------------------
# Adapter 1 -- the CLI wiring (real decide_quotes, real book shapes)
# ---------------------------------------------------------------------------

def test_cli_adapter_returns_eval_from_real_decide_quotes():
    up, dn = _books()
    m = _Market()

    ev = evaluate_market_quote(
        m.condition_id, _cfg(), "https://clob.polymarket.com",
        fetch_market=lambda cid: m,
        fetch_books=lambda host, token: up if token == m.up_token else dn,
        inventory_for=lambda market: Inventory(),
        decide=decide_quotes,
    )

    assert isinstance(ev, MarketEval)
    assert ev.cid == m.condition_id
    assert ev.market is m
    assert ev.up_book == up
    assert ev.down_book == dn
    assert ev.inventory == Inventory()
    assert len(ev.intents) == 2
    assert ev.why == ""


def test_cli_adapter_passes_require_rewards_off_like_decide():
    """decide() fetches with require_rewards=False; the step must receive the
    market it would get, not re-fetch."""
    m = _Market()
    seen = {}

    def fetch_market(cid):
        seen["cid"] = cid
        return m

    ev = evaluate_market_quote(
        m.condition_id, _cfg(), "https://clob.polymarket.com",
        fetch_market=fetch_market,
        fetch_books=lambda host, token: _books()[0],
        inventory_for=lambda market: Inventory(),
    )
    assert seen["cid"] == m.condition_id
    assert ev.market is m


# ---------------------------------------------------------------------------
# Adapter 2 -- the fleet's VenueSeam slot shapes
# ---------------------------------------------------------------------------

def test_fleet_adapter_slot_shapes():
    """The exact shapes the fleet's VenueSeam passes: fetch_market(cid),
    fetch_books(host, token), inventory_for(market), decide(cfg, up, dn, inv,
    t_rem, wf)."""
    seen = {}
    m = _Market()

    def fetch_market(cid):
        seen["cid"] = cid
        return _Market(cid)

    def fetch_books(host, token):
        seen["host"] = host
        return {"token_id": token, "best_bid": 0.5, "best_ask": 0.6}

    def inventory_for(market):
        seen["market"] = market
        return Inventory(up_shares=1.0, down_shares=1.0)

    def decide(cfg, up, dn, inv, t_rem, wf):
        seen["decide_args"] = (cfg, up, dn, inv, t_rem, wf)
        return [], "declined"

    ev = evaluate_market_quote(
        "0xabc", _cfg(), "clob.example",
        fetch_market=fetch_market, fetch_books=fetch_books,
        inventory_for=inventory_for, decide=decide,
    )

    assert seen["cid"] == "0xabc"
    assert seen["host"] == "clob.example"
    assert seen["market"] is ev.market
    cfg, up, dn, inv, t_rem, wf = seen["decide_args"]
    assert inv == Inventory(up_shares=1.0, down_shares=1.0)
    assert t_rem == 1e9
    assert wf is None
    assert ev.intents == []
    assert ev.why == "declined"


# ---------------------------------------------------------------------------
# Error detection -- typed, so each caller formats its own presentation
# ---------------------------------------------------------------------------

def test_market_unavailable_raises_typed_error():
    with pytest.raises(MarketUnavailable):
        evaluate_market_quote(
            "0xabc", _cfg(), "clob.example",
            fetch_market=lambda cid: None,
            fetch_books=lambda host, token: {},
            inventory_for=lambda market: Inventory(),
        )


def test_book_fetch_failure_raises_typed_error():
    def boom(host, token):
        raise RuntimeError("venue down")

    with pytest.raises(MarketQuoteError, match="book fetch error"):
        evaluate_market_quote(
            "0xabc", _cfg(), "clob.example",
            fetch_market=lambda cid: _Market(),
            fetch_books=boom,
            inventory_for=lambda market: Inventory(),
        )
