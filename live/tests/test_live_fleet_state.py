"""Fleet-wide state for the live fleet loop.

`engine.live_fleet._fleet_state` is what `run` merges into the per-cycle config
so the three fleet-level gates in `decide_quotes` -- naked dollars, committed
dollars, and the pooled-markout posture -- see live numbers rather than their
0.0 / NORMAL defaults. These tests pin the two new live inputs behind that
function: `_registry_committed_usd` (inventory cost + resting notional) and
`markout.fleet_stats` (the size-weighted pooled drift), and then that the wiring
returns all three aggregates together.
"""

from __future__ import annotations

import pytest

from engine.config import load
from engine.order_registry import registry_committed_usd
from engine.live_fleet import _fleet_state
from engine.markout import fleet_stats
from engine.order_registry import (
    CloseRecord,
    FillRecord,
    MarkoutRecord,
    OrderRecord,
    OrderRegistry,
)


@pytest.fixture
def reg(tmp_path):
    return OrderRegistry(tmp_path / "live.db")


def _order(reg, oid, cid, token, price, size, status="open", pair="p1", side="BUY"):
    reg.create_order(OrderRecord(
        id=oid, condition_id=cid, token_id=token, side=side, price=price,
        original_size=size, status=status, posted_ts=0, last_polled_ts=0,
        pair_id=pair,
    ))


def _fill(reg, trade_id, order_uuid, size, price):
    reg.record_fill(FillRecord(trade_id=trade_id, order_uuid=order_uuid,
                               size=size, price=price))


def _markout(reg, cid, size, drift, source="venue_clean", ref_mid=0.60,
             side="UP", extra_horizons=None):
    """One markout row whose 6h horizon carries `drift` (longest first)."""
    kw = {"mid_h2": ref_mid + drift}
    if extra_horizons:
        kw.update(extra_horizons)
    reg.log_markout(MarkoutRecord(
        ts=1.0, condition_id=cid, side=side, fill_price=ref_mid + drift,
        size=size, ref_mid=ref_mid, ref_mid_source=source, **kw,
    ))


class TestRegistryCommittedUsd:
    def test_inventory_cost_plus_resting_notional(self, reg):
        # UP filled 5 of 10 @ 0.60, DOWN filled 4 of 10 @ 0.40.
        _order(reg, "o1", "c1", "tok-up", 0.60, 10)
        _order(reg, "o2", "c1", "tok-dn", 0.40, 10)
        _fill(reg, "t1", "o1", 5, 0.60)
        _fill(reg, "t2", "o2", 4, 0.40)

        # Inventory cost 3.00 + 1.60; resting 5*0.60 + 6*0.40 = 3.00 + 2.40.
        assert registry_committed_usd(reg) == pytest.approx(10.0)

    def test_closed_condition_is_skipped_whole(self, reg):
        _order(reg, "o1", "c1", "tok-up", 0.60, 10)
        _fill(reg, "t1", "o1", 5, 0.60)          # 3.00 inventory + 3.00 resting

        _order(reg, "o2", "c2", "tok-up", 0.50, 10, pair="p2")
        _fill(reg, "t2", "o2", 10, 0.50)         # 5.00 inventory, no resting
        reg.log_close(CloseRecord(ts=1.0, condition_id="c2"))

        assert registry_committed_usd(reg) == pytest.approx(6.0)

    def test_filled_sell_reduces_cost_basis(self, reg):
        _order(reg, "o1", "c1", "tok-up", 0.60, 10)
        _order(reg, "o2", "c1", "tok-up", 0.60, 2, side="SELL")
        _fill(reg, "t1", "o1", 5, 0.60)          # +3.00
        _fill(reg, "t2", "o2", 2, 0.60)          # -1.20

        # Inventory 1.80 + resting 5*0.60 = 3.00.
        assert registry_committed_usd(reg) == pytest.approx(4.80)


class TestFleetStats:
    def test_pooled_weighted_mean_and_kish_effective_sample(self, reg):
        # One 200-share toxic fill vs nine 10-share good fills: the money
        # experiences the weighted mean, not the row mean.
        _markout(reg, "c1", 200.0, -0.05)
        for _ in range(9):
            _markout(reg, "c1", 10.0, +0.01)

        stats = fleet_stats(reg, min_sample=1)

        assert stats["mean_per_share"] == pytest.approx(-9.1 / 290.0)
        assert stats["verdict"] == "losing"
        # Kish: 290^2 / (200^2 + 9*10^2) = 84100/40900.
        assert stats["n"] == pytest.approx(84100.0 / 40900.0)
        assert stats["n_rows"] == 10

    def test_sample_dominated_by_one_fill_is_insufficient(self, reg):
        _markout(reg, "c1", 200.0, -0.05)
        for _ in range(9):
            _markout(reg, "c1", 10.0, +0.01)

        stats = fleet_stats(reg, min_sample=3)

        assert stats["verdict"] == "insufficient_sample"
        assert stats["mean_per_share"] is None
        assert stats["n_rows"] == 10

    def test_contaminated_rows_are_excluded_before_weighting(self, reg):
        for _ in range(30):
            _markout(reg, "c1", 1.0, -0.02, source="contaminated")

        stats = fleet_stats(reg, min_sample=1)

        assert stats["verdict"] == "insufficient_sample"
        assert stats["mean_per_share"] is None
        assert stats["n_rows"] == 0

    def test_longest_horizon_drift_wins_over_15m_read(self, reg):
        # 6h says -0.05, the shorter 15m read says -0.02. The gate judges the
        # longest matured horizon, never the shorter exit counterfactual.
        _markout(reg, "c1", 100.0, -0.05,
                 extra_horizons={"mid_h3": 0.60 - 0.02})

        stats = fleet_stats(reg, min_sample=1)

        assert stats["mean_per_share"] == pytest.approx(-0.05)

    def test_row_with_no_matured_horizon_contributes_nothing(self, reg):
        reg.log_markout(MarkoutRecord(
            ts=1.0, condition_id="c1", side="UP", fill_price=0.60, size=10.0,
            ref_mid=0.60, ref_mid_source="venue_clean",
        ))

        stats = fleet_stats(reg, min_sample=1)

        assert stats["verdict"] == "insufficient_sample"
        assert stats["n_rows"] == 0


class TestFleetStateWiring:
    def test_returns_all_three_fleet_wide_aggregates(self, reg):
        # A one-share naked pair, plus 25 clean markout rows deep enough to
        # trip the catastrophic threshold (-0.03 < -0.02).
        _order(reg, "o1", "c1", "tok-up", 0.60, 10)
        _order(reg, "o2", "c1", "tok-dn", 0.40, 10)
        _fill(reg, "t1", "o1", 5, 0.60)
        _fill(reg, "t2", "o2", 4, 0.40)
        for _ in range(25):
            _markout(reg, "c1", 1.0, -0.03)

        cfg = load()
        state = _fleet_state(reg, cfg)

        assert state["committed_usd"] == pytest.approx(10.0)
        assert state["fleet_naked_usd"] == pytest.approx(0.60)
        assert state["fleet_posture"] == "HALTED"
