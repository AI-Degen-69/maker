"""The dashboard smoke-test fixture must exercise every Level 1 widget.

`live/scripts/seed_preview_fixture.py` is what the owner points `--db` at to
eyeball the live monitor. This test pins what that fixture must produce: a
run whose report has real closes, real markouts, real quotes, a synthetic
venue sweep, and a healthy (hedged, non-stale) order book.
"""
from __future__ import annotations

import sqlite3

import pytest

from engine.kpi import report
from engine.order_registry import SCHEMA, OrderRegistry
from scripts.seed_preview_fixture import RUN_ID, seed


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "live.db"
    con = sqlite3.connect(str(db_file))
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    return db_file


def test_fixture_exercises_the_adverse_selection_bell_curve(temp_db):
    """4 matured markouts over 4 fills produce a non-NULL size-weighted drift."""
    reg = OrderRegistry(temp_db)
    seed(reg)

    data = report(db_path=temp_db, run_id=RUN_ID)

    assert data["markout_samples"] == 4
    # Three of four fills drift against us; one drifts favourably.
    # Drifts: -0.02, -0.01, -0.02, +0.01 at 5 shares each over 20 filled sh.
    assert data["adverse_selection"] == pytest.approx(-0.01)
    assert data["filled_shares"] == pytest.approx(20.0)


def test_fixture_exercises_the_closes_based_tiles(temp_db):
    """The same fixture feeds win rate, distribution, and risk factors."""
    reg = OrderRegistry(temp_db)
    seed(reg)

    ta = report(db_path=temp_db, run_id=RUN_ID)["trade_analytics"]

    assert ta["n_closes"] == 8
    assert ta["wins"] == 5
    assert ta["losses"] == 3
    assert ta["win_rate"] == pytest.approx(5 / 8)
    # Dollar expectancy is positive, mean return % negative (the -100% trade).
    assert ta["expectancy_usd"] == pytest.approx(0.20 / 8)
    assert ta["mean_return_pct"] < 0
    assert ta["sharpe_ratio"] is not None
    assert ta["max_drawdown_usd"] is not None
    assert ta["max_naked_exposure_usd"] == pytest.approx(3.20)


def test_fixture_populates_the_maker_quote_widgets(temp_db):
    """Quotes and market events give fill rate, uptime, queue, and capture."""
    reg = OrderRegistry(temp_db)
    seed(reg)

    data = report(db_path=temp_db, run_id=RUN_ID)

    # 4 quotes of 5 shares = 20 posted; 4 fills of 5 shares = 20 filled.
    assert data["posted_shares"] == pytest.approx(20.0)
    assert data["fill_rate"] == pytest.approx(1.0)
    # 6 QUOTING of 8 market events.
    assert data["quote_uptime"] == pytest.approx(0.75)
    assert data["median_queue_ahead"] is not None
    assert data["spread_capture"] is not None
    assert {r["reason"] for r in data["top_skip_reasons"]} == {"SPREAD_WIDE", "PRICE_BAND"}


def test_fixture_populates_the_account_card(temp_db):
    """The synthetic venue sweep fills account value, basis, unrealized, committed."""
    reg = OrderRegistry(temp_db)
    seed(reg)

    a = report(db_path=temp_db, run_id=RUN_ID)["portfolio"]["account"]

    assert a["measured"] is True
    assert a["source"] == "fixture"
    assert a["account_value_usd"] == pytest.approx(101.08)
    assert a["collateral_usd"] == pytest.approx(96.38)
    assert a["positions_value_usd"] == pytest.approx(4.70)
    assert a["pnl_closed_usd"] == pytest.approx(0.20)
    assert a["unrealized_usd"] == pytest.approx(0.65)
    assert a["committed_usd"] == pytest.approx(9.60)
    assert a["closed_positions_count"] == 8


def test_fixture_order_book_is_hedged_and_fresh(temp_db):
    """Balanced pairs mean no naked-leg alarm; recent polls mean no stale banner."""
    from engine.registry_state import summarize_state

    reg = OrderRegistry(temp_db)
    seed(reg)

    state = summarize_state(temp_db)
    assert state["stale"] is False
    assert all(p["hedge_state"] != "NAKED" for p in state["pairs"])
    assert any(p["hedge_state"] == "BALANCED" for p in state["pairs"])
