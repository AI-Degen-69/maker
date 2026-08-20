"""Regression tests for venue_sync — idempotency, dedup, schema mapping."""

import pytest
from pathlib import Path


def test_venue_sync_dedupes_by_condition_asset(tmp_path, monkeypatch):
    """Two venue_sync runs with the same closed positions should write only once."""
    from engine import live_exec as exec_mod
    from engine import order_registry as reg_mod
    from engine.order_registry import OrderRegistry

    # Override LIVE_ROOT so the lock file lands in tmp_path
    lock_dir = tmp_path / "run"
    lock_dir.mkdir(parents=True)
    monkeypatch.setattr(reg_mod, "LIVE_ROOT", tmp_path)

    # Mock venue responses
    def mock_read_account(funder, collateral_usd):
        return {
            "collateral_usd": 100.0,
            "positions_value_usd": 10.0,
            "account_value_usd": 110.0,
            "pnl_usd": 1.5,
            "pnl_pct": 1.36,
            "pnl_closed_usd": 1.5,
            "pnl_series_usd": 1.5,
            "pnl_source_gap": 0.0,
            "unrealized_usd": 0.5,
            "committed_usd": 5.0,
            "open_positions_count": 2,
            "closed_positions_count": 1,
            "open_positions": [],
            "closed_positions": [
                {
                    "conditionId": "0xabc",
                    "asset": "0xasset1",
                    "realizedPnl": 1.5,
                    "totalBought": 10.0,
                    "avgPrice": 0.55,
                    "timestamp": 1700000000,
                    "slug": "test-market",
                    "eventSlug": "test-event",
                }
            ],
            "source": "venue",
        }

    def mock_fetch_closed_positions(funder, timeout=15.0):
        return [
            {
                "conditionId": "0xabc",
                "asset": "0xasset1",
                "realizedPnl": 1.5,
                "totalBought": 10.0,
                "avgPrice": 0.55,
                "timestamp": 1700000000,
                "slug": "test-market",
                "eventSlug": "test-event",
            }
        ]

    def mock_fetch_open_positions(funder, timeout=15.0):
        return []

    monkeypatch.setattr("engine.account.read_account", mock_read_account)
    monkeypatch.setattr("engine.account.fetch_closed_positions", mock_fetch_closed_positions)
    monkeypatch.setattr("engine.account.fetch_open_positions", mock_fetch_open_positions)
    monkeypatch.setattr(exec_mod, "fetch_live_balance", lambda *a, **kw: 100.0)

    db_path = str(tmp_path / "test.db")

    # First run — should write
    summary1 = exec_mod.venue_sync(funder="0xfunder", db_path=db_path, quiet=True)
    assert summary1["ok"] is True
    assert summary1["closes_written"] == 1
    assert summary1["closes_skipped_existing"] == 0

    # Second run — should skip
    summary2 = exec_mod.venue_sync(funder="0xfunder", db_path=db_path, quiet=True)
    assert summary2["ok"] is True
    assert summary2["closes_written"] == 0
    assert summary2["closes_skipped_existing"] == 1

    # Verify DB has exactly one close row
    reg = OrderRegistry(db_path=db_path)
    with reg._conn() as conn:
        rows = conn.execute(
            "SELECT * FROM closes WHERE condition_id = '0xabc'"
        ).fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["condition_id"] == "0xabc"
    assert row["tx_hash"] == "0xasset1"
    assert row["method"] == "venue_sync"
    assert row["realized_pnl"] == pytest.approx(1.5)


def test_venue_sync_writes_float_mark(tmp_path, monkeypatch):
    """venue_sync with open positions writes a float_marks row."""
    from engine import live_exec as exec_mod
    from engine import order_registry as reg_mod
    from engine.order_registry import OrderRegistry

    lock_dir = tmp_path / "run"
    lock_dir.mkdir(parents=True)
    monkeypatch.setattr(reg_mod, "LIVE_ROOT", tmp_path)

    def mock_read_account(funder, collateral_usd):
        return {
            "collateral_usd": 100.0,
            "positions_value_usd": 50.0,
            "account_value_usd": 150.0,
            "pnl_usd": 2.0,
            "pnl_pct": 1.33,
            "pnl_closed_usd": 0.0,
            "pnl_series_usd": 0.0,
            "pnl_source_gap": 0.0,
            "unrealized_usd": 2.0,
            "committed_usd": 50.0,
            "open_positions_count": 2,
            "closed_positions_count": 0,
            "open_positions": [
                {"initialValue": 25.0, "cashPnl": 1.0, "currentValue": 26.0},
                {"initialValue": 25.0, "cashPnl": 1.0, "currentValue": 24.0},
            ],
            "closed_positions": [],
            "source": "venue",
        }

    def mock_fetch_closed_positions(funder, timeout=15.0):
        return []

    def mock_fetch_open_positions(funder, timeout=15.0):
        # Balanced pair on same condition: YES=26, NO=24.
        # Per-market grouping: total=50, min=24, naked=50-2*24=2
        return [
            {"conditionId": "0xABC", "initialValue": 25.0, "cashPnl": 1.0, "currentValue": 26.0},
            {"conditionId": "0xABC", "initialValue": 25.0, "cashPnl": 1.0, "currentValue": 24.0},
        ]

    monkeypatch.setattr("engine.account.read_account", mock_read_account)
    monkeypatch.setattr("engine.account.fetch_closed_positions", mock_fetch_closed_positions)
    monkeypatch.setattr("engine.account.fetch_open_positions", mock_fetch_open_positions)
    monkeypatch.setattr(exec_mod, "fetch_live_balance", lambda *a, **kw: 100.0)

    db_path = str(tmp_path / "test.db")

    summary = exec_mod.venue_sync(funder="0xfunder", db_path=db_path, quiet=True)
    assert summary["ok"] is True
    assert summary["raw_open_rows"] == 2

    reg = OrderRegistry(db_path=db_path)
    with reg._conn() as conn:
        rows = conn.execute("SELECT * FROM float_marks ORDER BY id DESC LIMIT 1").fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    # unrealized = 1.0 + 1.0 = 2.0
    assert row["unrealized_usd"] == pytest.approx(2.0)
    # committed = 25.0 + 25.0 = 50.0
    assert row["committed_open_usd"] == pytest.approx(50.0)
    # Naked per-market: 0xABC has 26+24=50, min=24, naked=50-2*24=2
    assert row["naked_usd"] == pytest.approx(2.0)


def test_venue_sync_preserves_existing_closes_adds_new(tmp_path, monkeypatch):
    """Mixed: existing closes from engine execution + new from venue."""
    from engine import live_exec as exec_mod
    from engine import order_registry as reg_mod
    from engine.order_registry import OrderRegistry, CloseRecord

    lock_dir = tmp_path / "run"
    lock_dir.mkdir(parents=True)
    monkeypatch.setattr(reg_mod, "LIVE_ROOT", tmp_path)

    db_path = str(tmp_path / "test.db")
    reg = OrderRegistry(db_path=db_path)

    # Pre-seed a close from an engine execution
    reg.log_close(CloseRecord(
        ts=1700000000.0,
        condition_id="0xengine",
        market_slug="engine-market",
        method="merge",
        shares=10.0,
        up_price=0.6,
        dn_price=0.4,
        cost_basis=5.0,
        proceeds=5.5,
        fee=0.05,
        realized_pnl=0.45,
        tx_hash="0xengine-tx",
        run_id="run-engine",
    ))

    # Mock venue returns TWO closed positions: one that matches the engine
    # one (different asset/condition) and one brand new
    def mock_read_account(funder, collateral_usd):
        return {
            "collateral_usd": 100.0,
            "positions_value_usd": 0.0,
            "account_value_usd": 100.0,
            "pnl_usd": 0.5,
            "pnl_pct": 0.5,
            "pnl_closed_usd": 0.5,
            "pnl_series_usd": 0.5,
            "pnl_source_gap": 0.0,
            "unrealized_usd": 0.0,
            "committed_usd": 0.0,
            "open_positions_count": 0,
            "closed_positions_count": 2,
            "open_positions": [],
            "closed_positions": [
                {
                    "conditionId": "0xvenue_new",
                    "asset": "0xasset_new",
                    "realizedPnl": 0.5,
                    "totalBought": 5.0,
                    "avgPrice": 0.5,
                    "timestamp": 1700000100,
                    "slug": "venue-market",
                    "eventSlug": "venue-event",
                }
            ],
            "source": "venue",
        }

    def mock_fetch_closed_positions(funder, timeout=15.0):
        return [
            {
                "conditionId": "0xvenue_new",
                "asset": "0xasset_new",
                "realizedPnl": 0.5,
                "totalBought": 5.0,
                "avgPrice": 0.5,
                "timestamp": 1700000100,
                "slug": "venue-market",
                "eventSlug": "venue-event",
            }
        ]

    def mock_fetch_open_positions(funder, timeout=15.0):
        return []

    monkeypatch.setattr("engine.account.read_account", mock_read_account)
    monkeypatch.setattr("engine.account.fetch_closed_positions", mock_fetch_closed_positions)
    monkeypatch.setattr("engine.account.fetch_open_positions", mock_fetch_open_positions)
    monkeypatch.setattr(exec_mod, "fetch_live_balance", lambda *a, **kw: 100.0)

    summary = exec_mod.venue_sync(funder="0xfunder", db_path=db_path, quiet=True)
    assert summary["closes_written"] == 1
    assert summary["closes_skipped_existing"] == 0

    # DB should have 2 closes: the original engine one + the new venue one
    with reg._conn() as conn:
        rows = conn.execute("SELECT * FROM closes ORDER BY id").fetchall()
    assert len(rows) == 2

    # Original engine close
    assert dict(rows[0])["condition_id"] == "0xengine"
    assert dict(rows[0])["method"] == "merge"

    # New venue close
    assert dict(rows[1])["condition_id"] == "0xvenue_new"
    assert dict(rows[1])["method"] == "venue_sync"
    assert dict(rows[1])["tx_hash"] == "0xasset_new"


def test_venue_sync_endpoint_exists():
    """Dashboard endpoint /api/system/venue-sync is registered."""
    from dash.live_dash import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    # Without auth token -> 403
    r = client.post("/api/system/venue-sync")
    assert r.status_code == 403
    # The endpoint exists (not 404)
    assert r.status_code != 404