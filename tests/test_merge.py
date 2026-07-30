"""Merge-at-parity tests (U2).

The arithmetic that makes this strategy compound rather than accumulate. A
matched pair redeems for exactly 1.00 through the collateral adapter, so the
measured 0.9728 median pair cost is +2.79% realizable now instead of in 2027.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.config import load as load_cfg   # noqa: E402
from strategy.merge import PARITY, pairing_rate, should_merge  # noqa: E402
from strategy.quotes import Inventory          # noqa: E402


def _inv(up_sh=100.0, dn_sh=100.0, up_px=0.50, dn_px=0.4728):
    """Defaults sit at the measured median pair cost of 0.9728."""
    return Inventory(up_shares=up_sh, down_shares=dn_sh,
                     up_cost=up_sh * up_px, down_cost=dn_sh * dn_px)


def _cfg():
    return load_cfg()


# --- the core decision ------------------------------------------------------

def test_a_pair_below_parity_merges():
    out = should_merge(_inv(), _cfg(), gas_cost=0.02)
    assert out["take"] is True
    assert out["shares"] == 100.0
    assert out["proceeds"] == 100.0 * PARITY
    # 100 shares x 2.72c gross, less 2c of gas.
    assert round(out["realized_pnl"], 4) == round(100 * 0.0272 - 0.02, 4)
    assert "merge 100 pairs" in out["why"]


def test_only_the_paired_portion_is_merged():
    """A merge consumes one share of EACH outcome, so the naked residue has
    nothing to pair with. It is skew's problem, not merge's."""
    out = should_merge(_inv(up_sh=100.0, dn_sh=60.0), _cfg(), gas_cost=0.01)
    assert out["shares"] == 60.0
    assert out["take"] is True


def test_naked_residue_is_ignored_in_both_directions():
    heavy_up = should_merge(_inv(up_sh=140.0, dn_sh=90.0), _cfg(), gas_cost=0.01)
    heavy_dn = should_merge(_inv(up_sh=90.0, dn_sh=140.0), _cfg(), gas_cost=0.01)
    assert heavy_up["shares"] == heavy_dn["shares"] == 90.0


def test_no_paired_shares_is_a_no_op():
    out = should_merge(_inv(up_sh=80.0, dn_sh=0.0), _cfg(), gas_cost=0.01)
    assert out["take"] is False
    assert out["shares"] == 0.0
    assert out["why"] == "no paired shares"


def test_per_leg_cost_removed_is_reported_separately():
    """The caller removes each leg at its own average. Splitting a combined
    basis after the fact is the bug the `closes` table already had once."""
    out = should_merge(_inv(up_px=0.60, dn_px=0.36), _cfg(), gas_cost=0.01)
    assert round(out["up_cost_removed"], 6) == 60.0
    assert round(out["dn_cost_removed"], 6) == 36.0
    assert round(out["cost_basis"], 6) == 96.0


def test_the_whole_paired_position_merges_regardless_of_book_depth():
    """The structural advantage over selling. Parity is a payout, not a price
    somebody has to bid, so no ladder can cap the size."""
    out = should_merge(_inv(up_sh=5000.0, dn_sh=5000.0), _cfg(), gas_cost=0.05)
    assert out["shares"] == 5000.0
    assert out["take"] is True


# --- gas ---------------------------------------------------------------------

def test_gain_below_gas_is_declined():
    """One transaction whatever the size, so the floor is on TOTAL gain. Ten
    shares at 2.72c is 27c of gain against a dollar of gas."""
    out = should_merge(_inv(up_sh=10.0, dn_sh=10.0), _cfg(), gas_cost=1.00)
    assert out["take"] is False
    assert "does not clear" in out["why"]


def test_a_thin_margin_on_a_large_pair_count_still_merges():
    """The mirror of the above, and why the floor is not a per-share rule."""
    out = should_merge(_inv(up_sh=5000.0, dn_sh=5000.0, up_px=0.50, dn_px=0.499),
                       _cfg(), gas_cost=0.05)
    assert out["take"] is True                 # 5000 x 0.1c = $5 against 5c


def test_gain_exactly_equal_to_gas_is_declined():
    """A merge that nets exactly zero is not worth a transaction."""
    inv = _inv(up_sh=100.0, dn_sh=100.0, up_px=0.50, dn_px=0.49)
    out = should_merge(inv, _cfg(), gas_cost=1.00)   # 100 x 1c = $1.00 exactly
    assert out["take"] is False


def test_unknown_gas_blocks_rather_than_defaulting_to_zero():
    """Zero-cost gas would make every merge look profitable -- the same silent
    failure shape as the `except: pass` that once reported $0.00 as good news."""
    out = should_merge(_inv(), _cfg(), gas_cost=None)
    assert out["take"] is False
    assert "gas cost unknown" in out["why"]


def test_negative_gas_is_rejected():
    out = should_merge(_inv(), _cfg(), gas_cost=-1.0)
    assert out["take"] is False
    assert "nonsensical" in out["why"]


# --- over-parity pairs (the U5 velocity exception refines this) --------------

def test_a_pair_above_parity_does_not_merge_on_the_gas_rule_alone():
    """4.5% of measured pairs cleared over 1.00. Under U2's rule they are held:
    the gain is negative, so no gas figure can make the arithmetic work. U5
    adds the bounded velocity exception that lets some of them through."""
    out = should_merge(_inv(up_px=0.51, dn_px=0.50), _cfg(), gas_cost=0.01)
    assert out["take"] is False
    assert out["gain_per_share"] < 0


# --- purity ------------------------------------------------------------------

def test_decision_never_mutates_inventory():
    inv = _inv()
    before = (inv.up_shares, inv.down_shares, inv.up_cost, inv.down_cost)
    should_merge(inv, _cfg(), gas_cost=0.01)
    assert (inv.up_shares, inv.down_shares, inv.up_cost, inv.down_cost) == before


# --- pairing rate ------------------------------------------------------------

def test_pairing_rate_measures_the_assumption_merge_economics_rest_on():
    assert pairing_rate(88.0, 100.0) == 0.88


def test_pairing_rate_is_undefined_with_nothing_filled():
    """No observation is not a pairing rate of zero."""
    assert pairing_rate(0.0, 0.0) is None


# --- one ledger, two methods (KTD2c) ----------------------------------------

def _fresh(monkeypatch, tmp_path, name="merge.db"):
    monkeypatch.setenv("MAKER_DB", str(tmp_path / name))
    from strategy import store
    return store


def test_merge_and_sell_share_one_table_and_reconstruct_identically(
        monkeypatch, tmp_path):
    """The reason KTD2c chose a discriminator over a second table: inventory
    reconstruction reads per-leg removed costs, which both methods record, so
    it needs no knowledge of how the pair was exited. A `merges` table would
    have required every P&L and rehydrate query to union two sources and stay
    in sync forever."""
    store = _fresh(monkeypatch, tmp_path)
    from strategy.fleet import _inventory_from_db

    store.log_fill(market_slug="s", condition_id="c", token_id="tu",
                   side="UP", price=0.50, size=100, reason="tape")
    store.log_fill(market_slug="s", condition_id="c", token_id="td",
                   side="DOWN", price=0.4728, size=100, reason="tape")
    store.log_close(condition_id="c", market_slug="s", method="merge",
                    gas=0.02, shares=40, cost_basis=40 * 0.9728,
                    proceeds=40.0, realized_pnl=40 * 0.0272 - 0.02,
                    forgone_vs_settlement=0.0,
                    up_cost_removed=40 * 0.50, dn_cost_removed=40 * 0.4728)

    inv = _inventory_from_db("c")
    assert inv.up_shares == 60.0 and inv.down_shares == 60.0
    # The residue keeps the basis it actually has -- merging at each leg's own
    # average leaves the remaining average untouched.
    assert round(inv.avg("UP"), 6) == 0.50
    assert round(inv.avg("DOWN"), 6) == 0.4728


def test_existing_close_rows_default_to_sell(monkeypatch, tmp_path):
    """Rows written before U2 predate merge entirely, so 'sell' is the true
    value for every one of them and the column default backfills itself."""
    store = _fresh(monkeypatch, tmp_path)
    store.log_close(condition_id="c", market_slug="s", shares=10,
                    up_price=0.51, dn_price=0.47, cost_basis=9.5,
                    proceeds=9.8, fee=0.34, realized_pnl=-0.04,
                    forgone_vs_settlement=0.54,
                    up_cost_removed=5.0, dn_cost_removed=4.5)
    with store.db() as c:
        method, gas = c.execute(
            "SELECT method, gas FROM closes WHERE condition_id='c'").fetchone()
    assert method == "sell"
    assert gas is None          # a sell has no gas, and must not report 0
