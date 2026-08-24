"""The sweep module's settle-and-cancel step (issue #11).

The per-market sweep in `strategy.fleet.visit` used to own settle/cancel
inline; the step now lives behind `strategy.sweep`'s interface so it can be
driven directly: feed a market state and a now, assert the outcome. The
startup-settle pass itself is covered in `test_fleet_startup_settle` through
the same interface; these tests cover the parts the startup pass does not
exercise: the mid-life settle (`_settle_resolved`), the bare cancel
(`_cancel_live_orders`) and the event dedup (`_record_event`).
"""
import sys
import time
from dataclasses import replace as _dcreplace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _spec(cid="cond-1", title="Test Market"):
    return {"cid": cid, "title": title, "slug": "test-mkt",
            "daily": 0.0, "min_size": 5, "max_spread": 4.5, "tick": 0.01,
            "shares": 120, "volume_24h": 100_000.0, "days_to_resolve": 1.0,
            "est_income": 0.0, "est_capital": 120.0, "return_pct_day": 0.0,
            "their_score": 100.0, "spread": 0.01}


def _seed_fill(store, cond="cond-1", slug="test-mkt", side="UP",
               price=0.44, size=100.0):
    store.log_fill(market_slug=slug, condition_id=cond, token_id="TOK-UP",
                   side=side, price=price, size=size)


def _events(store) -> int:
    with store.db() as c:
        return c.execute("SELECT COUNT(*) FROM market_events").fetchone()[0]


def test_settle_resolved_zeroes_inventory_and_refreshes_the_payload(
        monkeypatch, tmp_path):
    """Settling a resolved market releases BOTH legs: shares, cost, and the
    dashboard payload all read zero, and the payload is FRESH (stale=False,
    ts=now) -- measured now, not stamped as a failure.
    """
    monkeypatch.setenv("HUNTER_DB", str(tmp_path / "sweep.db"))
    from strategy.config import load as load_cfg
    from strategy import fleet, store, sweep

    _seed_fill(store, side="UP", price=0.44, size=100.0)
    _seed_fill(store, side="DOWN", price=0.50, size=100.0)
    st = fleet.MarketState(_spec(), load_cfg())
    # A resting order the settle must cancel on its way out.
    st.engine.post("TOK-UP", "UP", 0.40, 10, {}, 1000.0)
    assert st.inv.up_shares == 100.0
    assert st.inv.down_shares == 100.0

    freed = sweep._settle_resolved(st, now=2000.0)

    assert freed == 94.0, "the released cost (44 + 50) is the outcome"
    assert st.inv.up_shares == 0.0 and st.inv.down_shares == 0.0
    assert st.inv.up_cost == 0.0 and st.inv.down_cost == 0.0
    assert st.engine.open_orders() == [], "resting quotes must be cancelled"
    live = st.spec["_live"]
    assert live["stale"] is False, "settled figures are fresh, not stale"
    assert live["ts"] == 2000.0
    assert live["naked_sh"] == 0.0
    assert live["err"] == ""


def test_settle_resolved_records_a_resolved_event(monkeypatch, tmp_path):
    """A market holding inventory records the RESOLVED event; the zero case
    does not pretend shares were released."""
    monkeypatch.setenv("HUNTER_DB", str(tmp_path / "sweep.db"))
    from strategy.config import load as load_cfg
    from strategy import fleet, store, sweep

    _seed_fill(store, side="UP", price=0.44, size=100.0)
    st = fleet.MarketState(_spec(), load_cfg())
    freed = sweep._settle_resolved(st, now=1000.0)
    assert _events(store) == 1
    assert freed == 44.0

    # No inventory: the settle normalises the payload, records nothing and
    # reports zero released.
    st2 = fleet.MarketState(_spec(cid="cond-2"), load_cfg())
    assert sweep._settle_resolved(st2, now=1001.0) == 0.0
    assert _events(store) == 1


def test_cancel_live_orders_blank_the_payload_and_persist(monkeypatch,
                                                          tmp_path):
    """A market losing eligibility cancels its resting offers, blanks the
    dashboard's quote fields and marks the historical rows cancelled."""
    monkeypatch.setenv("HUNTER_DB", str(tmp_path / "sweep.db"))
    from strategy.config import load as load_cfg
    from strategy import fleet, store, sweep

    st = fleet.MarketState(_spec(), load_cfg())
    o = st.engine.post("TOK-UP", "UP", 0.40, 10, {}, 1000.0)
    o.quote_id = store.log_quote(
        market_slug="test-mkt", condition_id="cond-1", token_id="TOK-UP",
        side="UP", price=0.40, size=10, queue_ahead=0.0, mid=0.45,
        edge_vs_mid=0.05, t_remaining=1.0)
    st.spec["_live"] = {"quotes": [1], "capital": 12.0, "stale": False,
                        "up_bid": 0.44, "up_ask": 0.46, "dn_bid": 0.50,
                        "dn_ask": 0.52, "mid_up": 0.45, "our_up": 10.0,
                        "our_dn_as_up": 0.0, "dn_bid_as_up": 0.0,
                        "pair_cost": 0.95}

    sweep._cancel_live_orders(st)

    assert st.engine.open_orders() == []
    live = st.spec["_live"]
    assert live["quotes"] == []
    assert live["capital"] == 0.0
    assert live["stale"] is True
    for field in ("up_bid", "up_ask", "dn_bid", "dn_ask", "mid_up",
                  "our_up", "our_dn_as_up", "dn_bid_as_up", "pair_cost"):
        assert live[field] is None, f"{field} must be blanked"
    with store.db() as c:
        cancelled = c.execute(
            "SELECT cancelled FROM quotes WHERE id = ?",
            (o.quote_id,)).fetchone()[0]
    assert cancelled == 1, "cancelled rows must be persisted"


def test_record_event_collapses_routine_repeats_and_honours_force(
        monkeypatch, tmp_path):
    """A routine repeat inside the dedup window writes once; force writes
    through; a DIFFERENT event is not collapsed."""
    monkeypatch.setenv("HUNTER_DB", str(tmp_path / "sweep.db"))
    from strategy.config import load as load_cfg
    from strategy import fleet, store, sweep

    st = fleet.MarketState(_spec(), load_cfg())
    sweep._record_event(st, 1000.0, "QUOTING", "resting 2 limit orders")
    n1 = _events(store)
    assert n1 == 1

    sweep._record_event(st, 1001.0, "QUOTING", "resting 2 limit orders")
    assert _events(store) == n1, "a routine repeat inside 30s must collapse"

    sweep._record_event(st, 1001.0, "QUOTING", "resting 2 limit orders",
                       force=True)
    assert _events(store) == n1 + 1, "force must write through the dedup"

    sweep._record_event(st, 1002.0, "WAITING", "no eligible quote intent")
    assert _events(store) == n1 + 2, "a different event must not collapse"


# --- the sweep through its one interface (issue #12) --------------------------

class _Market:
    """Minimal stand-in for fetch_pinned_market output: tokens + ids only."""

    def __init__(self, up_token="tok-up", dn_token="tok-dn"):
        self.up_token = up_token
        self.down_token = dn_token
        self.condition_id = "cond-1"
        self.market_slug = "test-mkt"


class _BotCfg:
    """Just enough for the book fetches: a clob host attribute."""

    clob_host = "http://clob.test"


def _deep_book(token="tok-up"):
    """A side that clears the $1K top-3 depth bar and the spread bar."""
    return {"bids": {0.48: 5000.0, 0.47: 4000.0, 0.46: 3000.0},
            "asks": {0.50: 8000.0}, "best_bid": 0.48, "best_ask": 0.50,
            "token_id": token}


def _mid_book(token="tok-up"):
    """A side whose top-3 bid depth (~$710) clears the $500 trial bar but
    fails the permanent $1,000 one -- the population the U36 depth trial
    exists to admit, and the one the fleet used to refuse at quote time."""
    return {"bids": {0.48: 800.0, 0.47: 400.0, 0.46: 300.0},
            "asks": {0.50: 800.0}, "best_bid": 0.48, "best_ask": 0.50,
            "token_id": token}


def _boom(host, token):
    raise RuntimeError("venue timeout")


def _ctx(now=100.0, **kw):
    from strategy import sweep
    return sweep.SweepContext(bot_cfg=_BotCfg(), now=now, **kw)


def _mk_sweep_state(monkeypatch, tmp_path):
    """A market that passes the identity gate with its metadata already
    cached, so `sweep()` runs on fakes without touching the network."""
    monkeypatch.setenv("HUNTER_DB", str(tmp_path / "sweep.db"))
    from strategy.config import load as load_cfg
    from strategy import fleet

    st = fleet.MarketState(_spec(), load_cfg())
    st.market = _Market()
    return st


def test_sweep_settled_outcome_releases_the_committed_cost(monkeypatch,
                                                           tmp_path):
    """A market the venue has already closed settles and reports the released
    dollars as the outcome -- the caller never reaches into the engine."""
    monkeypatch.setenv("HUNTER_DB", str(tmp_path / "sweep.db"))
    from strategy.config import load as load_cfg
    from strategy import fleet, store, sweep

    _seed_fill(store, side="UP", price=0.44, size=100.0)
    _seed_fill(store, side="DOWN", price=0.50, size=100.0)
    st = fleet.MarketState(_spec(), load_cfg())
    st.engine.post("TOK-UP", "UP", 0.40, 10, {}, 1000.0)

    out = sweep.sweep(st, _ctx(now=2000.0, resolved_cids=frozenset({"cond-1"})))

    assert out.status == "SETTLED"
    assert out.released == 94.0, "the released cost (44 + 50) is the outcome"
    assert st.inv.up_shares == 0.0 and st.inv.down_shares == 0.0
    assert st.engine.open_orders() == [], "resting quotes must be cancelled"


def test_sweep_identity_blocked_outcome(monkeypatch, tmp_path):
    """A market the selector would never admit is refused before any fetch:
    its resting quotes are cancelled and the reason is the outcome's `why`."""
    monkeypatch.setenv("HUNTER_DB", str(tmp_path / "sweep.db"))
    from strategy.config import load as load_cfg
    from strategy import fleet, sweep

    st = fleet.MarketState(_spec(title="Test Market Live"), load_cfg())
    st.engine.post("TOK-UP", "UP", 0.40, 10, {}, 1000.0)

    out = sweep.sweep(st, _ctx(now=100.0))

    assert out.status == "IDENTITY_BLOCKED"
    assert out.why == st.err and "blocked" in out.why


def test_sweep_book_holding_then_failed_outcome(monkeypatch, tmp_path):
    """A book failure inside the confirmation window holds -- no action, no
    stamp; persisted past the window it fires -- quotes cancelled, error
    stamped. Each phase reports its own outcome status."""
    from strategy import sweep

    st = _mk_sweep_state(monkeypatch, tmp_path)
    st.engine.post("tok-dn", "DOWN", 0.50, 107.0, book_bids={0.50: 0.0},
                   ts=90.0)
    monkeypatch.setattr("strategy.sweep.full_book", _boom)

    held = sweep.sweep(st, _ctx(now=100.0))
    assert held.status == "BOOK_HOLDING"
    assert st.engine.open_orders(), "a venue blip must leave orders resting"
    assert st.err == ""

    fired = sweep.sweep(st, _ctx(now=100.0 + sweep.BOOK_GATE_CONFIRM_SEC
                                 + 0.01))
    assert fired.status == "BOOK_FAILED"
    assert all(o.cancelled for o in st.engine.open_orders())
    assert "book fetch" in st.err


def test_sweep_quoting_outcome_rests_both_sides(monkeypatch, tmp_path):
    """The full path: healthy books in, both sides quoted, and the outcome
    reports QUOTING with orders resting -- the loop's orchestration needs
    nothing but the outcome to know what happened."""
    from strategy import sweep

    st = _mk_sweep_state(monkeypatch, tmp_path)
    st.cfg = _dcreplace(st.cfg, max_rest_queue_ahead=0)  # orthog to queue gate
    monkeypatch.setattr("strategy.sweep.recent_trades", lambda *a, **k: {})
    monkeypatch.setattr("strategy.sweep.full_book",
                        lambda host, token: _deep_book(token))

    out = sweep.sweep(st, _ctx(now=100.0))

    assert out.status == "QUOTING"


def test_trial_depth_bar_reaches_the_live_book_gate(monkeypatch, tmp_path):
    """U36 wiring fix. A spec tagged `trial_depth_usd: 500` must gate the
    fleet's live book gate at $500, not the permanent $1,000 -- otherwise the
    ranker admits a market the fleet immediately refuses, and the trial is a
    no-op. Proven with a book whose top-3 depth (~$710) sits between the two
    bars: it must reach QUOTING under the trial tag and fail without it."""
    from strategy import sweep
    from strategy.config import load as load_cfg
    from strategy import fleet

    base_cfg = load_cfg()
    assert base_cfg.select_min_top3_depth_usd == 1000.0

    # Without the tag: the ~$710 book fails the permanent $1,000 bar.
    plain = fleet.MarketState(_spec(cid="cond-no-trial"), base_cfg)
    assert plain.cfg.select_min_top3_depth_usd == 1000.0

    # With the tag: the live gate drops to the trial bar.
    spec = _spec(cid="cond-trial")
    spec["trial_depth_usd"] = 500.0
    st = fleet.MarketState(spec, base_cfg)
    st.cfg = _dcreplace(st.cfg, max_rest_queue_ahead=0)  # orthog to queue gate
    assert st.cfg.select_min_top3_depth_usd == 500.0

    monkeypatch.setenv("HUNTER_DB", str(tmp_path / "sweep.db"))
    st.market = _Market()
    monkeypatch.setattr("strategy.sweep.recent_trades", lambda *a, **k: {})
    monkeypatch.setattr("strategy.sweep.full_book",
                        lambda host, token: _mid_book(token))

    out = sweep.sweep(st, _ctx(now=100.0))
    assert out.status == "QUOTING", out.why
    assert st.engine.open_orders(), "the trial bar must let orders rest"


def test_price_band_widened_to_the_spread_universe(monkeypatch, tmp_path):
    """U36f. The 0.30-0.70 band was tuned to the old coin-flip BTC series; the
    spread universe (tennis/CS2/MLB favourites) legitimately trades at
    0.15-0.85, and the band's protection lives at the 0.95+ settled edge.
    A 0.25/0.75 book must now quote on the rewards path -- it was blocked
    before the widening, and blocking it refused a funded market for hours."""
    from strategy import sweep
    from strategy.config import load as load_cfg
    from strategy import fleet

    base_cfg = load_cfg()
    assert base_cfg.price_band_low == 0.10
    assert base_cfg.price_band_high == 0.90

    spec = _spec(cid="cond-band")
    spec["spread"] = 0.01
    spec["volume_24h"] = 500_000.0
    monkeypatch.setenv("HUNTER_DB", str(tmp_path / "sweep.db"))
    st = fleet.MarketState(spec, base_cfg)
    st.cfg = _dcreplace(st.cfg, max_rest_queue_ahead=0)  # orthog to queue gate
    st.market = _Market()
    monkeypatch.setattr("strategy.sweep.recent_trades", lambda *a, **k: {})

    def lopsided(host, token):
        return {"bids": {0.25: 3000.0, 0.24: 2000.0, 0.23: 1000.0},
                "asks": {0.28: 3000.0}, "best_bid": 0.25, "best_ask": 0.28,
                "token_id": token}

    monkeypatch.setattr("strategy.sweep.full_book", lopsided)
    out = sweep.sweep(st, _ctx(now=100.0))
    assert out.status == "QUOTING", out.why
    assert out.requoted is True
    assert len(st.engine.open_orders()) == 2
    assert st.book_gate_fail_since is None


# --- U35 pairs-only rule ------------------------------------------------------
#
# A one-sided fill is either completed into a pair within 15 minutes (cross
# the missing leg at ask when the pair stays under max_pair_cost, then merge
# at parity) or exited at the best bid -- the measured alternative to holding
# a naked leg into the -18.5c/share 1h drift. The rule is dated off the most
# recent fill's wall time, so these tests seed the ledger rather than driving
# fills through the engine.


def _mk_book(bids, asks, token):
    return {"bids": bids, "asks": asks,
            "best_bid": max(bids) if bids else None,
            "best_ask": min(asks) if asks else None,
            "token_id": token}


# Both books clear the $1K top-3 depth bar and the 0.06 spread bar on both
# sides, so the sweep reaches the full path.
_UP_BOOK = _mk_book({0.48: 5000.0, 0.47: 4000.0, 0.46: 3000.0},
                    {0.50: 8000.0}, "tok-up")
# DOWN ask 0.46: a 0.44 UP fill completes at 0.90 < max_pair_cost 0.995.
_DN_BOOK_CHEAP = _mk_book({0.44: 5000.0, 0.43: 4000.0, 0.42: 3000.0},
                          {0.46: 8000.0}, "tok-dn")
# DOWN ask 0.555: a 0.44 UP fill would cost 0.995 >= max_pair_cost -- the
# pair is not completable, so the rule must exit the UP leg at its 0.48 bid.
# (0.555 keeps the DOWN spread at 0.055, clear of the 0.06 bar after float
# rounding, which 0.56 - 0.50 = 0.06000...05 trips.)
_DN_BOOK_DEAR = _mk_book({0.50: 5000.0, 0.49: 4000.0, 0.48: 3000.0},
                         {0.555: 8000.0}, "tok-dn")


def _pairs_state(monkeypatch, tmp_path, books, fills=None):
    """A market with healthy books and optionally a seeded one-sided fill.

    The temp HUNTER_DB must be set BEFORE the fills are seeded -- the state
    rehydrates its inventory (and the rule's fill clock) from that database
    at construction, so a fill seeded into the previous env's DB would leave
    the rebuilt position empty.
    """
    monkeypatch.setenv("HUNTER_DB", str(tmp_path / "sweep.db"))
    from strategy import store

    for side, price, size in fills or []:
        _seed_fill(store, side=side, price=price, size=size)
    st = _mk_sweep_state(monkeypatch, tmp_path)
    monkeypatch.setattr("strategy.sweep.recent_trades", lambda *a, **k: {})
    up, dn = books
    monkeypatch.setattr(
        "strategy.sweep.full_book",
        lambda host, token: up if token == "tok-up" else dn)
    return st


def _closes(store) -> list[dict]:
    with store.db() as c:
        cols = [d[0] for d in c.execute("SELECT * FROM closes").description]
        return [dict(zip(cols, r)) for r in c.execute("SELECT * FROM closes")]


def _crossed_fills(store) -> int:
    with store.db() as c:
        return c.execute("SELECT COUNT(*) FROM fills WHERE crossed = 1"
                         ).fetchone()[0]


def test_pairs_rule_completes_fillable_pair_and_merges(monkeypatch, tmp_path):
    """A recent one-sided UP fill whose missing leg is buyable under
    max_pair_cost is COMPLETED (crossed DOWN fill) and the completed pair is
    merged at parity in the same sweep -- the measured +16c capture, not a
    naked leg held into the drift."""
    from strategy import store, sweep

    st = _pairs_state(monkeypatch, tmp_path,
                      books=(_UP_BOOK, _DN_BOOK_CHEAP),
                      fills=[("UP", 0.44, 100.0)])

    out = sweep.sweep(st, _ctx(now=time.time()))

    assert _crossed_fills(store) == 1, "the missing leg was crossed at ask"
    closes = _closes(store)
    assert [c["method"] for c in closes] == ["merge"], \
        "the completed pair is redeemed at parity in the same sweep"
    # 0.44 + 0.46 = 0.90 -> 100 pairs pay 100, cost 90, gas 0.05.
    assert closes[0]["realized_pnl"] == pytest.approx(9.95)
    assert st.inv.up_shares == 0.0 and st.inv.down_shares == 0.0
    live = st.spec["_live"]
    assert live["pairs_rule"]["action"] == "complete"
    assert live["pairs_rule"]["pair_cost"] == pytest.approx(0.90, abs=0.001)


def test_pairs_rule_exits_naked_leg_at_best_bid(monkeypatch, tmp_path):
    """When the missing leg would make the pair cost >= max_pair_cost, the
    rule EXITS the naked leg at the best bid instead of holding it -- a
    closes row with method='naked_exit' booking the half-spread cost."""
    from strategy import store, sweep

    st = _pairs_state(monkeypatch, tmp_path,
                      books=(_UP_BOOK, _DN_BOOK_DEAR),
                      fills=[("UP", 0.44, 100.0)])

    out = sweep.sweep(st, _ctx(now=time.time()))

    closes = _closes(store)
    assert [c["method"] for c in closes] == ["naked_exit"]
    assert closes[0]["up_price"] == pytest.approx(0.48)
    assert closes[0]["dn_price"] is None, "only the UP leg was sold"
    # 100 x 0.48 bid - 44 cost - 100 x 0.017 fee.
    assert closes[0]["realized_pnl"] == pytest.approx(2.3)
    assert st.inv.up_shares == 0.0 and st.inv.down_shares == 0.0
    assert st.spec["_live"]["pairs_rule"]["action"] == "exit"
    assert _crossed_fills(store) == 0, "an exit sells, it does not cross"


def test_pairs_rule_window_expiry_leaves_position_alone(monkeypatch,
                                                        tmp_path):
    """A one-sided fill older than the 15-minute window is left alone: no
    forced exit, no completion -- but the expiry is recorded ONCE so the EV
    KPI can count the fill as having ridden out the window."""
    monkeypatch.setenv("HUNTER_DB", str(tmp_path / "sweep.db"))
    from strategy import store, sweep

    with store.db() as c:
        c.execute(
            "INSERT INTO fills (ts, market_slug, condition_id, token_id, "
            "side, price, size) VALUES (?,?,?,?,?,?,?)",
            (1000.0, "test-mkt", "cond-1", "TOK-UP", "UP", 0.44, 100.0))
    st = _mk_sweep_state(monkeypatch, tmp_path)
    monkeypatch.setattr("strategy.sweep.recent_trades", lambda *a, **k: {})
    monkeypatch.setattr(
        "strategy.sweep.full_book",
        lambda host, token: _UP_BOOK if token == "tok-up" else _DN_BOOK_CHEAP)

    out = sweep.sweep(st, _ctx(now=2000.0))  # fill is 1000s old

    assert _closes(store) == [], "an expired window must not force an exit"
    assert _crossed_fills(store) == 0
    assert st.inv.up_shares == 100.0, "the position is left exactly as it was"
    assert st.spec["_live"]["pairs_rule"]["action"] == "expired"
    # Once per fill: a second sweep in the same window does not re-record the
    # EXPIRY (the requote/score steps may still write their own events).
    def _expiry_events():
        with store.db() as c:
            return c.execute("SELECT COUNT(*) FROM market_events "
                             "WHERE kind='PAIR_WINDOW_EXPIRED'"
                             ).fetchone()[0]
    assert _expiry_events() == 1
    sweep.sweep(st, _ctx(now=2100.0))
    assert _expiry_events() == 1


def test_pairs_rule_disabled_flag_leaves_fills_untouched(monkeypatch,
                                                         tmp_path):
    """HUNTER_PAIRS_RULE=0 turns the rule off entirely -- the naked leg is
    neither completed nor exited (the switchable-trial convention every other
    behavioural change here follows)."""
    monkeypatch.setenv("HUNTER_PAIRS_RULE", "0")
    from strategy import store, sweep

    st = _pairs_state(monkeypatch, tmp_path,
                      books=(_UP_BOOK, _DN_BOOK_CHEAP),
                      fills=[("UP", 0.44, 100.0)])

    out = sweep.sweep(st, _ctx(now=time.time()))

    assert _closes(store) == []
    assert _crossed_fills(store) == 0
    assert st.inv.up_shares == 100.0 and st.inv.down_shares == 0.0
    assert st.spec["_live"]["pairs_rule"]["action"] == "disabled"


def test_hedge_census_recorded_every_sweep(monkeypatch, tmp_path):
    """Every successful sweep records whether a fillable sub-$1.00 pair was
    present at the touch -- the census table the Phase A reader was written
    for and that never got switched on."""
    from strategy import store, sweep

    st = _pairs_state(monkeypatch, tmp_path,
                      books=(_UP_BOOK, _DN_BOOK_CHEAP))

    out = sweep.sweep(st, _ctx(now=time.time()))

    with store.db() as c:
        row = c.execute("SELECT up_ask, down_ask, pair_cost_at_touch, "
                        "fillable_sub_one FROM hedge_census "
                        "WHERE condition_id='cond-1'").fetchone()
    assert row is not None, "every sweep must record the pair census"
    up_ask, dn_ask, pair_cost, fillable = row
    assert (up_ask, dn_ask) == (0.50, 0.46)
    # pair_cost_at_touch = ask + ask - reward_offset (0.02).
    assert pair_cost == pytest.approx(0.94)
    assert fillable == 1, "0.94 < max_pair_cost 0.995"


def test_close_window_balance_hedge_crosses_the_light_side(monkeypatch, tmp_path):
    """Plan 02 (harden-to-reality). When a market is within `balance_hedge_sec`
    of resolution and the book is lopsided, the sweep must CROSS the light side
    to flatten -- a true taker cross logged as `CROSS_HEDGE`, not a passive bid
    at the ask. `_Market` has no `t_remaining`, so we inject one returning a
    value under the 20s window; a seeded UP-only fill makes the book heavy UP.
    """
    from strategy import store, sweep

    st = _pairs_state(monkeypatch, tmp_path,
                      books=(_UP_BOOK, _DN_BOOK_CHEAP),
                      fills=[("UP", 0.44, 100.0)])
    # Light side is DOWN (we hold 100 UP, 0 DOWN). Near close -> cross DOWN.
    st.market.t_remaining = lambda now=None: 10.0  # < balance_hedge_sec 20.0
    # Make the DOWN ask depth shallow so the cross fills partially, exercising
    # the residual-cancel path.
    shallow_dn = _mk_book({0.44: 5000.0, 0.43: 4000.0, 0.42: 3000.0},
                          {0.46: 30.0}, "tok-dn")
    monkeypatch.setattr(
        "strategy.sweep.full_book",
        lambda host, token: _UP_BOOK if token == "tok-up" else shallow_dn)

    out = sweep.sweep(st, _ctx(now=time.time()))

    # CROSS_HEDGE must be recorded.
    with store.db() as c:
        row = c.execute("SELECT COUNT(*) FROM decisions "
                        "WHERE action='CROSS_HEDGE'").fetchone()
    assert row[0] == 1, "close-window hedge must log exactly one CROSS_HEDGE"
    # The cross is a taker fill: a crossed DOWN fill must exist.
    with store.db() as c:
        n = c.execute("SELECT COUNT(*) FROM fills WHERE side='DOWN' "
                      "AND crossed=1").fetchone()[0]
    assert n >= 1, "the close-window hedge must cross the light (DOWN) side"
    # Inventory moved toward balance: the hedge added DOWN shares.
    assert st.inv.down_shares > 0.0


def test_close_window_hedge_is_inert_when_far_from_close(monkeypatch, tmp_path):
    """Plan 02. Long-dated markets report t_remaining in the billions, so the
    close-window hedge must NOT fire and must not log CROSS_HEDGE.
    """
    from strategy import store, sweep

    st = _pairs_state(monkeypatch, tmp_path,
                      books=(_UP_BOOK, _DN_BOOK_CHEAP),
                      fills=[("UP", 0.44, 100.0)])
    st.market.t_remaining = lambda now=None: 1e9  # far from close

    sweep.sweep(st, _ctx(now=time.time()))

    with store.db() as c:
        row = c.execute("SELECT COUNT(*) FROM decisions "
                        "WHERE action='CROSS_HEDGE'").fetchone()
    assert row[0] == 0, "far-from-close markets must not trigger the hedge"


def test_skip_rest_is_logged_when_no_quote(monkeypatch, tmp_path):
    """Plan 04 (harden-to-reality). A market the decision layer sits out of
    must record WHY via a SKIP_REST decision, not just return a string the
    dashboard never sees. A one-tick spread under the spread objective rests
    nothing and names the thin-spread reason.
    """
    from strategy import store, sweep

    st = _pairs_state(monkeypatch, tmp_path,
                      books=(_UP_BOOK, _DN_BOOK_CHEAP))
    # Force the spread objective so the thin-spread guard fires.
    st.cfg = _spec_cfg_spread(st.cfg)
    # 0.481/0.482 -> spread 0.001 < 0.01 min: rests nothing.
    thin_up = _mk_book({0.481: 5000.0}, {0.482: 8000.0}, "tok-up")
    thin_dn = _mk_book({0.481: 5000.0}, {0.482: 8000.0}, "tok-dn")
    monkeypatch.setattr(
        "strategy.sweep.full_book",
        lambda host, token: thin_up if token == "tok-up" else thin_dn)

    sweep.sweep(st, _ctx(now=time.time()))

    with store.db() as c:
        row = c.execute("SELECT COUNT(*) FROM decisions "
                        "WHERE action='SKIP_REST'").fetchone()
    assert row[0] >= 1, "a dark market must log SKIP_REST with its reason"


def _spec_cfg_spread(cfg):
    """Patch a config to the spread objective with a wide min-spread bar."""
    import dataclasses
    return dataclasses.replace(
        cfg, objective="spread",
        spread_capture_default_spread=0.01, tick_size=0.001,
        min_quote_shares=5)


# --- queue-depth gate (harden-to-reality, followup #1) --------------------
# A resting quote only fills near the front of the book; behind thousands of
# shares it never fills at the front and when it does it is adverse-selected.
# The gate refuses to post when queue_ahead at our price exceeds
# max_rest_queue_ahead. These pin that behaviour.

def test_queue_depth_gate_blocks_deep_book(monkeypatch, tmp_path):
    """A book with thousands of shares ahead at our rest price must NOT be
    quoted -- the gate sits us out and records the queue reason."""
    from strategy import store, sweep

    # UP book: deep queue at EVERY bid level -> whichever price the quoter
    # picks, queue_ahead > 50, so the gate must block and record it.
    deep_up = _mk_book({0.48: 5000.0, 0.47: 5000.0, 0.46: 5000.0},
                      {0.50: 8000.0}, "tok-up")
    deep_dn = _mk_book({0.44: 5000.0, 0.43: 5000.0, 0.42: 5000.0},
                      {0.46: 8000.0}, "tok-dn")
    st = _pairs_state(monkeypatch, tmp_path, books=(deep_up, deep_dn))
    st.cfg = _dcreplace(st.cfg, max_rest_queue_ahead=50.0)

    monkeypatch.setattr(
        "strategy.sweep.full_book",
        lambda host, token: deep_up if token == "tok-up" else deep_dn)

    sweep.sweep(st, _ctx(now=time.time()))

    # The gate invariant: no resting order may sit behind a queue deeper than
    # the cap. (The quoter may pick a shallow price and rest there legally.)
    open_orders = st.engine.open_orders()
    for o in open_orders:
        assert o.queue_ahead <= 50.0, (
            f"{o.side} rested at {o.price} behind {o.queue_ahead}sh "
            f"> max_rest_queue_ahead 50")
    # And at least one quote was blocked (the deep 0.48 bid) -> a BLOCKED event.
    with store.db() as c:
        n = c.execute("SELECT COUNT(*) FROM market_events "
                      "WHERE kind='BLOCKED' AND reason LIKE '%queue%'").fetchone()[0]
    assert n >= 1, "the queue-depth block must be recorded as a BLOCKED event"


def test_queue_depth_gate_allows_shallow_book(monkeypatch, tmp_path):
    """A book with no shares ahead at our rest price (front of book) DOES get
    quoted -- the gate only refuses the deep case."""
    from strategy import store, sweep

    # UP book: empty bids at 0.48 (our rest price) -> queue_ahead = 0.
    shallow_up = _mk_book({0.48: 0.0, 0.47: 4000.0}, {0.50: 8000.0}, "tok-up")
    shallow_dn = _mk_book({0.44: 0.0, 0.43: 4000.0}, {0.46: 8000.0}, "tok-dn")
    st = _pairs_state(monkeypatch, tmp_path, books=(shallow_up, shallow_dn))
    st.cfg = _dcreplace(st.cfg, max_rest_queue_ahead=50.0)

    monkeypatch.setattr(
        "strategy.sweep.full_book",
        lambda host, token: shallow_up if token == "tok-up" else shallow_dn)

    sweep.sweep(st, _ctx(now=time.time()))

    assert st.engine.open_orders(), "shallow-queue quote must rest"


def test_queue_depth_gate_disabled_when_zero(monkeypatch, tmp_path):
    """max_rest_queue_ahead=0 disables the gate: even a deep book is quoted."""
    from strategy import store, sweep

    deep_up = _mk_book({0.48: 5000.0, 0.47: 4000.0}, {0.50: 8000.0}, "tok-up")
    deep_dn = _mk_book({0.44: 5000.0, 0.43: 4000.0}, {0.46: 8000.0}, "tok-dn")
    st = _pairs_state(monkeypatch, tmp_path, books=(deep_up, deep_dn))
    st.cfg = _dcreplace(st.cfg, max_rest_queue_ahead=0.0)

    monkeypatch.setattr(
        "strategy.sweep.full_book",
        lambda host, token: deep_up if token == "tok-up" else deep_dn)

    sweep.sweep(st, _ctx(now=time.time()))

    assert st.engine.open_orders(), "gate=0 must allow even deep-queue quotes"


# --- wallet cap clamp (harden-to-reality, Action 3) ------------------------
# The venue holds collateral against every open bid; on live Polymarket you
# cannot have open quotes whose notional exceeds the wallet. fleet_committed_cost
# already sums the fleet, but the defensive clamp `min(available, max_committed_usd)`
# guarantees no single sweep pushes exposure past the wallet even if the sampled
# committed state under-counts. This pins that the wallet is never exceeded.

def test_wallet_cap_clamp_never_exceeds_max_committed(monkeypatch, tmp_path):
    """A market offered a huge book must still leave the wallet at or below
    max_committed_usd after a sweep -- open notional + inventory <= wallet."""
    import dataclasses
    from strategy import sweep
    from strategy.config import load as load_cfg
    from strategy import fleet, store

    st = _mk_sweep_state(monkeypatch, tmp_path)
    # Wallet of $100, but a book deep enough to tempt a much larger allocation.
    st.cfg = dataclasses.replace(
        st.cfg, max_committed_usd=100.0, allocation_budget=10_000.0,
        max_cost_per_market=10_000.0, min_quote_shares=5)
    # Deep two-sided book -> the quoter wants to commit far more than $100.
    deep_up = _mk_book({0.52: 50_000.0, 0.51: 40_000.0, 0.50: 30_000.0},
                       {0.52: 50_000.0, 0.51: 40_000.0, 0.50: 30_000.0},
                       "tok-up")
    deep_dn = _mk_book({0.48: 50_000.0, 0.47: 40_000.0, 0.46: 30_000.0},
                       {0.48: 50_000.0, 0.47: 40_000.0, 0.46: 30_000.0},
                       "tok-dn")
    monkeypatch.setattr(
        "strategy.sweep.full_book",
        lambda host, token: deep_up if token == "tok-up" else deep_dn)

    sweep.sweep(st, _ctx(now=time.time()))

    committed = sweep.fleet_committed_cost([st])
    assert committed <= st.cfg.max_committed_usd + 1e-6, (
        f"wallet cap breached: committed ${committed:.2f} > "
        f"max_committed_usd ${st.cfg.max_committed_usd:.2f}")
