"""Markout: the cost of being filled.

These markets resolve in 2026-2027, so settlement P&L reads $0.00 for months.
Markout answers the same question in hours: after we were filled, where did the
price actually go?
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.markout import markout_per_share, _stats_from_rows  # noqa: E402


def _row(mk, source="venue_clean"):
    return {"markout": mk, "ref_mid_source": source}


def test_buy_that_drifts_down_is_a_loss():
    """Bought UP at 0.57, mid later 0.55 -- the fill was informed against us."""
    assert markout_per_share(0.57, 0.55, "UP") == pytest.approx(-0.02)


def test_buy_that_drifts_up_is_a_gain():
    """Each side is measured against its OWN token's mid, so one formula does
    both. Buying DOWN at 0.38 into a 0.40 DOWN mid is a 2c gain."""
    assert markout_per_share(0.38, 0.40, "DOWN") == pytest.approx(0.02)


def test_stats_ignore_markets_under_min_sample():
    """3 fills on a thin book is noise. Refusing to render a verdict is the
    point -- evicting a sound market on noise costs real rent."""
    stats = _stats_from_rows([_row(-0.02)] * 3, min_sample=20)
    assert stats["verdict"] == "insufficient_sample"
    assert stats["mean_per_share"] is None


def test_stats_report_mean_once_sample_is_adequate():
    stats = _stats_from_rows([_row(-0.02)] * 20, min_sample=20)
    assert stats["mean_per_share"] == pytest.approx(-0.02)
    assert stats["verdict"] == "losing"


def test_contaminated_rows_are_excluded():
    """A live run that cannot exclude our own resting size from the reference
    mid would measure our own footprint and report it as edge. Those rows are
    marked and must not count toward the sample."""
    rows = [_row(-0.02, source="contaminated")] * 30
    stats = _stats_from_rows(rows, min_sample=20)
    assert stats["verdict"] == "insufficient_sample"


def test_drift_excludes_our_own_entry_discount():
    """THE correction. Total markout bakes in the 2c we quote under mid, so a
    market whose price never moved reads '+2.15c, fills are great' and the
    gate can only ever trip on a catastrophe. Drift measures the move alone.
    """
    from strategy.markout import drift_per_share
    # bought 2c under a 0.59 mid; the mid never moved
    assert drift_per_share(ref_mid=0.59, mid_later=0.59) == pytest.approx(0.0)
    # same fill, but the price fell 1c afterwards -- that is the real cost
    assert drift_per_share(ref_mid=0.59, mid_later=0.58) == pytest.approx(-0.01)


def test_a_stationary_market_is_not_reported_as_edge():
    """Regression on the live reading: +2.11c captured spread, +0.04c drift.
    The verdict must follow the drift, not the 2.15c total."""
    from strategy.markout import _stats_from_rows
    rows = [{"markout": -0.004, "ref_mid_source": "venue_clean"}] * 25
    assert _stats_from_rows(rows, min_sample=20)["verdict"] == "losing"


def test_positive_mean_reads_as_earning():
    stats = _stats_from_rows([_row(0.01)] * 25, min_sample=20)
    assert stats["verdict"] == "earning"
