"""Reconciliation persistence (U6).

The engine classifying an outcome is worthless if the classification never
reaches the database -- the 2026-08-01 run had 29,742 evidence rows and still
could not answer why its fill count was zero.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy import store                                   # noqa: E402
from strategy.fills import QueueFillEngine                   # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    monkeypatch.setenv("MAKER_DB", str(tmp_path / "recon.db"))


def _rows(eng, cid="c1"):
    return [(r.ts, cid, r.token_id, r.side, r.price, r.tape_volume,
             r.queue_ahead, r.remaining, r.credited, r.outcome)
            for r in eng.reconciliation]


def test_a_behind_queue_miss_survives_the_round_trip():
    eng = QueueFillEngine()
    eng.post("T", "UP", 0.50, 120, {0.50: 300.0}, 0.0)
    eng.on_book("T", {0.50: 300.0}, 1.0, traded={})
    del eng.reconciliation[:]
    eng.on_book("T", {0.50: 100.0}, 2.0, traded={0.50: 200.0})
    store.log_fill_recon(_rows(eng))

    s = store.recon_summary()
    assert s["by_outcome"]["behind_queue"]["n"] == 1
    assert s["by_outcome"]["behind_queue"]["tape_volume"] == 200.0
    assert s["by_outcome"]["behind_queue"]["credited"] == 0.0


def test_the_summary_separates_a_quiet_market_from_a_queue_loss():
    """The whole point. Both are zero fills; only one is about selection."""
    eng = QueueFillEngine()
    eng.post("T", "UP", 0.50, 120, {0.50: 300.0}, 0.0)
    eng.on_book("T", {0.50: 300.0}, 1.0, traded={})
    del eng.reconciliation[:]
    eng.on_book("T", {0.50: 300.0}, 2.0, traded={})            # quiet
    eng.on_book("T", {0.50: 100.0}, 3.0, traded={0.50: 200.0})  # queue loss
    store.log_fill_recon(_rows(eng))

    s = store.recon_summary()
    assert s["by_outcome"]["no_trade_at_price"]["n"] == 1
    assert s["by_outcome"]["behind_queue"]["n"] == 1
    assert s["observations"] == 2
    assert s["traded_at_our_price_pct"] == 50.0


def test_a_tape_outage_does_not_count_against_the_market():
    """A gap in evidence is not an observation that the market was quiet.

    `tape_unavailable` used to sit in the denominator, so an hour of tape
    trouble dragged `traded_at_our_price_pct` down and a plumbing failure read
    as a verdict on market selection -- the one confusion this summary exists
    to remove.

    One credited row and one unreadable row is 100% of what was actually
    observed, not 50%.
    """
    store.log_fill_recon([
        (1.0, "c1", "T", "UP", 0.50, 200.0, 0.0, 0.0, 1, "credited"),
        (2.0, "c1", "T", "UP", 0.50, None, 0.0, 120.0, 0, "tape_unavailable"),
    ])
    s = store.recon_summary()
    assert s["observations"] == 2, "the outage is still recorded"
    assert s["tape_observations"] == 1, "but only one row could be read"
    assert s["traded_at_our_price_pct"] == 100.0


def test_an_entirely_unreadable_run_reports_none_not_zero():
    """No readable row means no answer, which is not the same as 0%."""
    store.log_fill_recon([
        (1.0, "c1", "T", "UP", 0.50, None, 0.0, 120.0, 0, "tape_unavailable"),
    ])
    s = store.recon_summary()
    assert s["observations"] == 1
    assert s["tape_observations"] == 0
    assert s["traded_at_our_price_pct"] is None


def test_an_empty_run_reports_none_rather_than_a_measured_zero():
    assert store.recon_summary()["traded_at_our_price_pct"] is None


def test_logging_no_rows_is_a_no_op():
    store.log_fill_recon([])
    assert store.recon_summary()["observations"] == 0
