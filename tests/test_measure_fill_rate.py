"""Tests for the replay harness in scripts/measure_fill_rate.py.

The harness is about to produce the number that decides whether this strategy
is worth building. On this project the last two "findings" turned out to be
bugs in the measuring code, not facts about the market, so the harness gets
scripted books with a hand-computed answer before it is pointed at real data.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from measure_fill_rate import Poll, Window, replay        # noqa: E402
from strategy.config import load as load_cfg              # noqa: E402


def _book(token, bids, asks):
    live_b = {p: s for p, s in bids.items() if s > 0}
    live_a = {p: s for p, s in asks.items() if s > 0}
    return {
        "token_id": token, "bids": dict(bids), "asks": dict(asks),
        "best_bid": max(live_b) if live_b else None,
        "best_ask": min(live_a) if live_a else None,
    }


def _window(specs, start=0.0, end=300.0):
    """specs = [(ts, up_bids, up_asks, dn_bids, dn_asks)]"""
    polls = [Poll(ts, _book("UPTOK", ub, ua), _book("DNTOK", db, da))
             for ts, ub, ua, db, da in specs]
    return Window("cond1", "slug", start, end, polls)


CFG = load_cfg()
DN_STATIC = ({0.49: 100.0}, {0.50: 100.0})     # never moves -> never fills


def test_sweep_fills_the_whole_remainder_and_is_tagged_as_such():
    w = _window([
        (10.0, {0.50: 100.0}, {0.51: 100.0}, *DN_STATIC),
        (20.0, {0.50: 100.0}, {0.51: 100.0}, *DN_STATIC),
        (30.0, {0.50: 0.0, 0.49: 50.0}, {0.51: 100.0}, *DN_STATIC),
    ])
    r = replay(w, CFG, rule="bid", requote="on-change", price_band=None,
               time_frac=None, requote_interval=0.0)
    up = sorted([e for e in r.episodes if e.side == "UP"], key=lambda e: e.posted_ts)
    dn = [e for e in r.episodes if e.side == "DOWN"]
    # DOWN's book never moves, so one order rests the whole window. UP gets a
    # second episode because the sweep drops the best bid to 0.49 and `bid`
    # follows it down -- the fill lands on the first order, before the reprice.
    assert len(up) == 2 and len(dn) == 1
    assert up[0].queue_ahead == 100.0
    assert up[0].filled == CFG.quote_shares          # swept level -> all of it
    assert up[0].filled_sweep == CFG.quote_shares
    assert up[0].filled_queue == 0.0
    assert up[1].price == 0.49 and up[1].filled == 0.0
    assert dn[0].filled == 0.0                       # static book, no fill
    assert r.up_shares == CFG.quote_shares and r.down_shares == 0.0
    assert r.hedged == 0.0                           # one leg only -> no pair


def test_queue_fill_is_credited_only_past_the_shares_ahead():
    w = _window([
        (10.0, {0.50: 100.0}, {0.51: 100.0}, *DN_STATIC),   # 100 ahead of us
        (20.0, {0.50: 200.0}, {0.51: 100.0}, *DN_STATIC),   # +100 behind us
        (30.0, {0.50: 50.0}, {0.51: 100.0}, *DN_STATIC),    # 150 consumed
    ])
    r = replay(w, CFG, rule="bid", requote="on-change", price_band=None,
               time_frac=None, requote_interval=0.0)
    up = [e for e in r.episodes if e.side == "UP"][0]
    # 150 traded: the 100 queued ahead clears first, the remaining 50 is ours.
    assert up.filled == 50.0
    assert up.filled_queue == 50.0 and up.filled_sweep == 0.0


def test_requoting_every_cycle_resets_queue_position_and_costs_the_fill():
    """The churn cost strategy/main.py pays today, isolated.

    Same books as the test above. Cancelling and re-posting at an unchanged
    price sends us to the BACK of the queue, so the 100 shares we had already
    waited behind are replaced by the 200 now resting -- and the 150 that
    trade no longer reach us.
    """
    specs = [
        (10.0, {0.50: 100.0}, {0.51: 100.0}, *DN_STATIC),
        (20.0, {0.50: 200.0}, {0.51: 100.0}, *DN_STATIC),
        (30.0, {0.50: 50.0}, {0.51: 100.0}, *DN_STATIC),
    ]
    on_change = replay(_window(specs), CFG, "bid", "on-change", None, None, 0.0)
    always = replay(_window(specs), CFG, "bid", "always", None, None, 0.0)
    assert on_change.up_shares == 50.0
    assert always.up_shares == 0.0
    # and it shows up as churn: one episode held vs one per cycle
    assert len([e for e in on_change.episodes if e.side == "UP"]) == 1
    assert len([e for e in always.episodes if e.side == "UP"]) == 3


def test_no_quoting_inside_the_final_seconds():
    """Mirrors the bot: quoting stops at min_t_remaining_sec."""
    late = 300.0 - CFG.min_t_remaining_sec + 1.0
    w = _window([
        (late, {0.50: 100.0}, {0.51: 100.0}, *DN_STATIC),
        (late + 1, {0.50: 100.0}, {0.51: 100.0}, *DN_STATIC),
    ])
    r = replay(w, CFG, "bid", "on-change", None, None, 0.0)
    assert r.episodes == []


def test_time_and_price_filters_suppress_quotes():
    specs = [
        (10.0, {0.50: 100.0}, {0.51: 100.0}, *DN_STATIC),      # 3% into window
        (200.0, {0.50: 100.0}, {0.51: 100.0}, *DN_STATIC),     # 67% in
    ]
    early_only = replay(_window(specs), CFG, "bid", "on-change", None, 0.40, 0.0)
    assert all(e.posted_frac <= 0.40 for e in early_only.episodes)
    assert early_only.episodes                                  # but not empty

    # DOWN rests at 0.49, UP at 0.50; a 0.495-0.70 band keeps only UP.
    banded = replay(_window(specs), CFG, "bid", "on-change", (0.495, 0.70),
                    None, 0.0)
    assert {e.side for e in banded.episodes} == {"UP"}
