import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.config import load as load_cfg  # noqa: E402
from strategy.profit_take import should_close  # noqa: E402
from strategy.quotes import Inventory  # noqa: E402


def _inv(up_sh=100.0, dn_sh=100.0, up_px=0.50, dn_px=0.45):
    return Inventory(up_shares=up_sh, down_shares=dn_sh,
                     up_cost=up_sh * up_px, down_cost=dn_sh * dn_px)


def _cfg():
    return load_cfg()


def _ladder(price, size=1000.0):
    """A single-level bid ladder deep enough to never cap a test's close."""
    return {price: size}


# --- opportunity cost -------------------------------------------------------

def _stagnant():
    """A pair that has barely moved: cost 0.95, bids sum to 0.98, so the 3c of
    gross appreciation does not quite cover the 3.4c of taker fees. Net is
    -0.4c/share -- comfortably under the normal 2c threshold, and just inside
    the -0.5c the relaxed one allows. Holds normally, closes under scarcity."""
    return _inv(up_px=0.50, dn_px=0.45), _ladder(0.51), _ladder(0.47), _cfg()


def test_a_stagnant_pair_is_held_when_capital_is_plentiful():
    inv, up, dn, cfg = _stagnant()
    out = should_close(inv, up, dn, cfg, capital_scarce=False)
    assert out["take"] is False
    assert out["realized_pnl"] < 0


def test_the_same_pair_is_liquidated_when_capital_is_scarce():
    """Nothing about the position changed -- only the value of the dollars it
    is sitting on. That is the whole point of the flag."""
    inv, up, dn, cfg = _stagnant()
    out = should_close(inv, up, dn, cfg, capital_scarce=True)
    assert out["take"] is True
    assert "capital scarce" in out["why"]


def test_scarcity_is_not_a_licence_to_dump():
    """A pair well under water fails even the relaxed threshold. The
    concession is a fraction of a fee, not permission to sell at any price."""
    inv = _inv(up_px=0.50, dn_px=0.45)
    out = should_close(inv, _ladder(0.45), _ladder(0.40), _cfg(),
                       capital_scarce=True)
    assert out["take"] is False


def test_scarcity_defaults_off_so_existing_callers_are_unchanged():
    inv, up, dn, cfg = _stagnant()
    assert should_close(inv, up, dn, cfg) == should_close(
        inv, up, dn, cfg, capital_scarce=False)


def test_no_paired_shares_never_closes():
    inv = Inventory(up_shares=100.0, up_cost=50.0)
    out = should_close(inv, _ladder(0.99), _ladder(0.99), _cfg())
    assert out["take"] is False


def test_missing_bid_never_closes():
    out = should_close(_inv(), None, _ladder(0.60), _cfg())
    assert out["take"] is False


def test_zero_depth_on_one_leg_closes_nothing():
    """An empty ladder on one leg (no resting bids at all) must close zero
    shares -- there is no size the book can actually absorb on that side,
    however deep the other leg is."""
    out = should_close(_inv(), _ladder(0.56), {}, _cfg())
    assert out["take"] is False
    assert out["shares"] == pytest.approx(0.0)


def test_move_that_only_covers_the_fees_does_not_close():
    # cost 0.95, exit 0.99 -> gross 4c, fees 3.4c, net 0.6c < 2c threshold
    out = should_close(_inv(), _ladder(0.54), _ladder(0.45), _cfg())
    assert out["take"] is False


def test_move_past_the_threshold_closes():
    # cost 0.95, exit 1.01 -> gross 6c, fees 3.4c, net 2.6c >= 2c threshold
    out = should_close(_inv(), _ladder(0.56), _ladder(0.45), _cfg())
    assert out["take"] is True
    assert out["shares"] == pytest.approx(100.0)


def test_realized_pnl_is_proceeds_minus_cost_minus_fee():
    out = should_close(_inv(), _ladder(0.56), _ladder(0.45), _cfg())
    assert out["proceeds"] == pytest.approx(101.0)
    assert out["cost_basis"] == pytest.approx(95.0)
    assert out["fee"] == pytest.approx(3.4)      # 2 legs x 100sh x 0.017
    assert out["realized_pnl"] == pytest.approx(2.6)


def test_only_the_paired_portion_is_closed():
    # 150 UP vs 100 DOWN: the 50 naked UP shares are not this mechanism's
    # business and must be left alone.
    inv = _inv(up_sh=150.0, dn_sh=100.0)
    out = should_close(inv, _ladder(0.56), _ladder(0.45), _cfg())
    assert out["take"] is True
    assert out["shares"] == pytest.approx(100.0)


def test_forgone_vs_settlement_is_hold_value_minus_realized():
    # Holding 100 pairs to settlement nets (1.00 - 0.95) * 100 = $5.00.
    # Closing nets 2.6 (see above). Forgone = 5.00 - 2.6 = 2.4.
    out = should_close(_inv(), _ladder(0.56), _ladder(0.45), _cfg())
    assert out["forgone_vs_settlement"] == pytest.approx(2.4)


def test_thin_book_closes_only_the_sellable_part():
    """600 paired shares against a 50-share resting bid cannot really be
    closed at that size -- only 50 pairs can. The rest is left exactly as
    it was, not booked as a phantom close."""
    inv = _inv(up_sh=600.0, dn_sh=600.0)
    out = should_close(inv, _ladder(0.56, size=50.0), _ladder(0.45, size=1000.0),
                       _cfg())
    assert out["take"] is True
    assert out["shares"] == pytest.approx(50.0)
    # Consistent arithmetic on the closed quantity only.
    assert out["cost_basis"] == pytest.approx(50.0 * 0.95)
    assert out["proceeds"] == pytest.approx(50.0 * 1.01)
    assert out["fee"] == pytest.approx(50.0 * 0.034)


def test_walked_average_price_can_fail_where_top_of_book_would_pass():
    """Pricing the whole position at the top-of-book tick is fiction once the
    size sold has to walk down through worse levels. Here the top level alone
    would clear the threshold, but the ACHIEVED average across the full size
    does not -- and that average, not the top tick, is what decides `take`.
    """
    inv = _inv(up_sh=200.0, dn_sh=200.0)
    up_bids = {0.56: 50.0, 0.50: 150.0}   # first 50 good, next 150 worse
    dn_bids = _ladder(0.45, size=1000.0)

    # Sanity check: top-of-book alone would have cleared the threshold.
    # cost 0.95, top exit 1.01 -> net 2.6c/sh >= 2.0c threshold.
    top_net = (0.56 + 0.45) - 0.95 - 2 * 0.017
    assert top_net >= _cfg().profit_take_net_threshold

    out = should_close(inv, up_bids, dn_bids, _cfg())
    # Achieved average on UP: (50*0.56 + 150*0.50) / 200 = 0.515
    # exit = 0.515 + 0.45 = 0.965; net = 0.965 - 0.95 - 0.034 = -0.019
    assert out["shares"] == pytest.approx(200.0)   # book absorbs the full size
    assert out["take"] is False


def test_proceeds_equals_shares_times_achieved_average_price():
    """The `closes` table logs up_price/dn_price next to proceeds, and the
    two must not contradict each other: proceeds is derived from walking the
    ladder, so the price columns must be the ACHIEVED average for the size
    actually closed -- not the top-of-book tick, which would silently
    disagree with proceeds on any close that walks past the best level.

    Uses a partial close (300 paired, only 200 sellable) that walks a second,
    worse level on the UP leg, so top-of-book (0.60) and the achieved average
    (0.575) genuinely differ -- a test where they coincide would prove
    nothing about which one `up_avg_price` actually is.
    """
    inv = _inv(up_sh=300.0, dn_sh=300.0)
    up_bids = {0.60: 100.0, 0.55: 100.0}   # depth 200: first 100 good, next worse
    dn_bids = _ladder(0.45, size=1000.0)

    out = should_close(inv, up_bids, dn_bids, _cfg())
    assert out["shares"] == pytest.approx(200.0)     # book-limited, partial
    assert out["up_avg_price"] == pytest.approx(0.575)   # != top-of-book 0.60
    assert out["dn_avg_price"] == pytest.approx(0.45)
    assert out["proceeds"] == pytest.approx(
        out["shares"] * (out["up_avg_price"] + out["dn_avg_price"]))


def test_close_reconstruction_uses_per_leg_removed_cost_not_share_split(
        tmp_path, monkeypatch):
    """A close removes cost from each leg at that leg's OWN average price,
    not in proportion to the share counts. The two only coincide when both
    legs have the same average cost -- they don't here (0.50 vs 0.40) -- so
    reconstructing from a share-count split corrupts down_cost even though
    down_shares lands at exactly zero.

    Exercises the real store (via MAKER_DB pointed at a temp DB) for
    log_close and the fills/closes tables, and calls
    strategy.fleet._inventory_from_db directly so the fix under test -- the
    up_cost_removed/dn_cost_removed columns and their use in rehydration --
    is what actually runs, not a re-implementation of it in the test.
    """
    db_path = tmp_path / "profit_take_recon.db"
    monkeypatch.setenv("MAKER_DB", str(db_path))

    from strategy import store, fleet
    from strategy.quotes import Inventory as InvCls

    cid = "test-cid-skewed"

    # Seed the fills ledger with the original skewed position the live
    # process would have accumulated: 150 UP @ 0.50 (up_cost=75),
    # 100 DOWN @ 0.40 (down_cost=40).
    with store.db() as c:
        c.execute(
            "INSERT INTO fills (ts, market_slug, condition_id, token_id, "
            "side, price, size) VALUES (?,?,?,?,?,?,?)",
            (1.0, "s", cid, "t", "UP", 0.50, 150.0))
        c.execute(
            "INSERT INTO fills (ts, market_slug, condition_id, token_id, "
            "side, price, size) VALUES (?,?,?,?,?,?,?)",
            (2.0, "s", cid, "t", "DOWN", 0.40, 100.0))

    inv = InvCls(up_shares=150.0, down_shares=100.0, up_cost=75.0, down_cost=40.0)

    # Mimic the fleet close block exactly: capture removed cost BEFORE
    # mutating, and decrement cost before shares.
    n = 100.0
    up_removed = n * inv.avg("UP")
    dn_removed = n * inv.avg("DOWN")
    inv.up_cost -= up_removed
    inv.down_cost -= dn_removed
    inv.up_shares -= n
    inv.down_shares -= n

    # What the live process now holds.
    assert inv.up_cost == pytest.approx(25.0)
    assert inv.down_cost == pytest.approx(0.0)
    assert inv.up_shares == pytest.approx(50.0)
    assert inv.down_shares == pytest.approx(0.0)

    store.log_close(condition_id=cid, market_slug="s", shares=n,
                     up_price=0.60, dn_price=0.35,
                     cost_basis=up_removed + dn_removed,
                     proceeds=100.0, fee=3.4, realized_pnl=1.0,
                     forgone_vs_settlement=0.0,
                     up_cost_removed=up_removed, dn_cost_removed=dn_removed)

    rebuilt = fleet._inventory_from_db(cid)

    # The reconstruction after "restart" must match the live values exactly,
    # including down_cost being 0.0 on a zero-share leg -- the specific case
    # the share-count split got wrong (it would have left a residual
    # down_cost sitting on down_shares=0).
    assert rebuilt.up_shares == pytest.approx(50.0)
    assert rebuilt.down_shares == pytest.approx(0.0)
    assert rebuilt.up_cost == pytest.approx(25.0)
    assert rebuilt.down_cost == pytest.approx(0.0)
