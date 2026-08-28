"""Tests for engine.registry_state — the read side of the live registry.

Moved verbatim from test_live_dash.py when the state summarization (reads,
capital math, hedge-state classification) was extracted out of the dashboard.
Only the import target changed (`engine.registry_state.summarize_state`); the
dashboard test file keeps one thin wiring test asserting /api/state returns
this reader's output. test_read_only_enforcement stays here because it pins the
read-only URI contract this module depends on: the page must never write.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from engine.order_registry import SCHEMA
from engine.registry_state import summarize_state


@pytest.fixture
def temp_db(tmp_path):
    """Create a temporary SQLite database initialized with the real registry schema."""
    db_file = tmp_path / "live.db"
    con = sqlite3.connect(str(db_file))
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    return db_file


def test_empty_database_non_existent(tmp_path):
    """When the DB file does not exist, it reports empty=True with a clean message."""
    missing_path = tmp_path / "missing.db"
    state = summarize_state(missing_path)
    assert state["empty"] is True
    assert state["orders"] == []
    assert state["pairs"] == []
    assert state["fills"] == []
    assert state["stale"] is False
    assert "not found" in state["message"] or "Database" in state["message"]


def test_empty_database_with_schema(temp_db):
    """When the DB has tables but 0 orders, it reports empty=True cleanly."""
    state = summarize_state(temp_db)
    assert state["empty"] is True
    assert state["orders"] == []
    assert state["pairs"] == []
    assert state["capital"]["total_committed"] == 0.0
    assert state["last_polled_ts"] is None
    assert state["seconds_since_poll"] is None


def test_resting_pair(temp_db):
    """A pair with 2 resting orders and 0 fills reports RESTING hedge state."""
    now_ms = int(time.time() * 1000)
    con = sqlite3.connect(str(temp_db))
    cur = con.cursor()

    pair_id = "pair_test_001"
    cur.execute("""
        INSERT INTO orders (id, order_id, condition_id, token_id, side, price, original_size, status, posted_ts, last_polled_ts, pair_id, max_pair_cost_at_post)
        VALUES ('uuid-up', 'clob-up', 'cond-1', 'tok-up', 'BUY', 0.54, 10.0, 'open', ?, ?, ?, 0.98),
               ('uuid-dn', 'clob-dn', 'cond-1', 'tok-dn', 'BUY', 0.43, 10.0, 'open', ?, ?, ?, 0.98)
    """, (now_ms, now_ms, pair_id, now_ms, now_ms, pair_id))
    con.commit()
    con.close()

    state = summarize_state(temp_db)
    assert state["empty"] is False
    assert len(state["orders"]) == 2
    assert len(state["pairs"]) == 1

    pair = state["pairs"][0]
    assert pair["pair_id"] == pair_id
    assert pair["hedge_state"] == "RESTING"
    assert pair["naked_info"] is None
    assert pair["combined_price"] == 0.97
    assert pair["max_pair_cost_at_post"] == 0.98

    # Capital checks: 10 * 0.54 + 10 * 0.43 = $9.70
    assert state["capital"]["total_committed"] == 9.70
    assert state["capital"]["resting_committed"] == 9.70
    assert state["capital"]["filled_committed"] == 0.0


def test_naked_pair(temp_db):
    """When one leg fills while the other has 0 fills, reports NAKED state with dollar risk."""
    now_ms = int(time.time() * 1000)
    con = sqlite3.connect(str(temp_db))
    cur = con.cursor()

    pair_id = "pair_test_naked"
    cur.execute("""
        INSERT INTO orders (id, order_id, condition_id, token_id, side, price, original_size, status, posted_ts, last_polled_ts, pair_id, max_pair_cost_at_post)
        VALUES ('uuid-up', 'clob-up', 'cond-1', 'tok-up', 'BUY', 0.54, 10.0, 'filled', ?, ?, ?, 0.98),
               ('uuid-dn', 'clob-dn', 'cond-1', 'tok-dn', 'BUY', 0.43, 10.0, 'open', ?, ?, ?, 0.98)
    """, (now_ms - 20000, now_ms, pair_id, now_ms - 20000, now_ms, pair_id))

    # Insert fill on UP order
    fill_ts = now_ms - 15000
    cur.execute("""
        INSERT INTO fills (trade_id, order_uuid, size, price, venue_ts)
        VALUES ('trade-001', 'uuid-up', 10.0, 0.54, ?)
    """, (fill_ts,))
    con.commit()
    con.close()

    state = summarize_state(temp_db)
    assert state["empty"] is False
    pair = state["pairs"][0]
    assert pair["hedge_state"] == "NAKED"
    assert pair["naked_info"] is not None

    naked = pair["naked_info"]
    assert naked["unhedged_shares"] == 10.0
    assert naked["unhedged_dollars"] == 5.40
    # Net direction of the exposure, not the side of the order that opened it:
    # a token can carry several orders, and an overshooting SELL leaves a net
    # SHORT that no single order side could express.
    assert naked["unhedged_side"] == "LONG"
    assert naked["naked_since_ts"] == fill_ts
    assert naked["seconds_naked"] >= 14.0

    # Capital: 10 * 0.54 filled ($5.40), 10 * 0.43 resting ($4.30) -> total $9.70
    assert state["capital"]["filled_committed"] == 5.40
    assert state["capital"]["resting_committed"] == 4.30
    assert state["capital"]["total_committed"] == 9.70


def test_balanced_pair(temp_db):
    """When both legs fill equally, reports BALANCED state."""
    now_ms = int(time.time() * 1000)
    con = sqlite3.connect(str(temp_db))
    cur = con.cursor()

    pair_id = "pair_test_balanced"
    cur.execute("""
        INSERT INTO orders (id, order_id, condition_id, token_id, side, price, original_size, status, posted_ts, last_polled_ts, pair_id, max_pair_cost_at_post)
        VALUES ('uuid-up', 'clob-up', 'cond-1', 'tok-up', 'BUY', 0.54, 10.0, 'filled', ?, ?, ?, 0.98),
               ('uuid-dn', 'clob-dn', 'cond-1', 'tok-dn', 'BUY', 0.43, 10.0, 'filled', ?, ?, ?, 0.98)
    """, (now_ms - 30000, now_ms, pair_id, now_ms - 30000, now_ms, pair_id))

    cur.execute("""
        INSERT INTO fills (trade_id, order_uuid, size, price, venue_ts)
        VALUES ('trade-001', 'uuid-up', 10.0, 0.54, ?),
               ('trade-002', 'uuid-dn', 10.0, 0.43, ?)
    """, (now_ms - 25000, now_ms - 20000))
    con.commit()
    con.close()

    state = summarize_state(temp_db)
    pair = state["pairs"][0]
    assert pair["hedge_state"] == "BALANCED"
    assert pair["naked_info"] is None
    assert state["capital"]["filled_committed"] == 9.70
    assert state["capital"]["resting_committed"] == 0.0


def test_stale_poll_detection(temp_db):
    """When max(last_polled_ts) is older than 30s, reports stale=True."""
    now_ms = int(time.time() * 1000)
    stale_poll_ms = now_ms - 45000  # 45 seconds ago

    con = sqlite3.connect(str(temp_db))
    cur = con.cursor()
    cur.execute("""
        INSERT INTO orders (id, order_id, condition_id, token_id, side, price, original_size, status, posted_ts, last_polled_ts, pair_id, max_pair_cost_at_post)
        VALUES ('uuid-stale', 'clob-1', 'cond-1', 'tok-1', 'BUY', 0.50, 10.0, 'open', ?, ?, 'pair_stale', 0.98)
    """, (stale_poll_ms, stale_poll_ms))
    con.commit()
    con.close()

    state = summarize_state(temp_db)
    assert state["stale"] is True
    assert state["seconds_since_poll"] is not None
    assert state["seconds_since_poll"] >= 40.0


def test_unattributed_status_flag(temp_db):
    """An order with status='unattributed' is explicitly flagged."""
    now_ms = int(time.time() * 1000)
    con = sqlite3.connect(str(temp_db))
    cur = con.cursor()
    cur.execute("""
        INSERT INTO orders (id, order_id, condition_id, token_id, side, price, original_size, status, posted_ts, last_polled_ts, pair_id, max_pair_cost_at_post)
        VALUES ('uuid-unattr', 'clob-unknown', 'cond-1', 'tok-1', 'BUY', 0.50, 10.0, 'unattributed', ?, ?, 'pair-1', 0.98)
    """, (now_ms, now_ms))
    con.commit()
    con.close()

    state = summarize_state(temp_db)
    assert len(state["orders"]) == 1
    assert state["orders"][0]["is_unattributed"] is True
    assert state["orders"][0]["status"] == "unattributed"


def test_reconcile_lock_status(temp_db):
    """Reconcile lock row is correctly read and formatted."""
    now_ms = int(time.time() * 1000)
    con = sqlite3.connect(str(temp_db))
    cur = con.cursor()
    cur.execute("""
        INSERT INTO reconcile_lock (id, holder, acquired_ts)
        VALUES (1, 'poll_worker_pid_4092', ?)
    """, (now_ms - 5000,))
    con.commit()
    con.close()

    state = summarize_state(temp_db)
    lock = state["reconcile_lock"]
    assert lock["held"] is True
    assert lock["holder"] == "poll_worker_pid_4092"
    assert lock["acquired_ts"] == now_ms - 5000
    assert lock["age_sec"] >= 4.0


def test_read_only_enforcement(temp_db):
    """Verifies that the registry read connection is strictly read-only and cannot write."""
    uri = f"file:{temp_db.resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    cur = con.cursor()
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        cur.execute("INSERT INTO reconcile_lock (id, holder, acquired_ts) VALUES (1, 'hack', 1000)")
    con.close()


def test_exit_shape_three_orders_two_tokens_reports_naked(temp_db):
    """A pair holding three orders on two tokens must still report NAKED.

    `exit` adds a SELL on a token already in the pair, so a genuinely unhedged
    position routinely carries three orders. Classifying by order count sent this
    shape to a calm RESTING with no warning -- the one state this page must never
    show while a leg is naked.
    """
    now_ms = int(time.time() * 1000)
    con = sqlite3.connect(str(temp_db))
    cur = con.cursor()

    pair_id = "pair_exit_shape"
    cur.execute("""
        INSERT INTO orders (id, order_id, condition_id, token_id, side, price, original_size, status, posted_ts, last_polled_ts, pair_id, max_pair_cost_at_post)
        VALUES ('uuid-up',   'clob-up',   'cond-1', 'tok-up', 'BUY',  0.54, 10.0, 'filled',    ?, ?, ?, 0.98),
               ('uuid-dn',   'clob-dn',   'cond-1', 'tok-dn', 'BUY',  0.43, 10.0, 'cancelled', ?, ?, ?, 0.98),
               ('uuid-sell', 'clob-sell', 'cond-1', 'tok-up', 'SELL', 0.52, 10.0, 'open',      ?, ?, ?, 0.98)
    """, (now_ms - 30000, now_ms, pair_id,
          now_ms - 30000, now_ms, pair_id,
          now_ms - 5000, now_ms, pair_id))
    cur.execute("""
        INSERT INTO fills (trade_id, order_uuid, size, price, venue_ts)
        VALUES ('trade-up', 'uuid-up', 10.0, 0.54, ?)
    """, (now_ms - 25000,))
    con.commit()
    con.close()

    pair = summarize_state(temp_db)["pairs"][0]
    assert len(pair["orders"]) == 3
    assert pair["hedge_state"] == "NAKED"
    assert pair["naked_info"]["unhedged_shares"] == 10.0
    assert pair["naked_info"]["unhedged_dollars"] == 5.40
    # Three orders, two tokens: the pair cost is the two token prices, not three.
    assert pair["combined_price"] == 0.97


def test_exit_sell_filled_flattens_to_closed(temp_db):
    """Once the SELL fills, the token nets to zero and exposure is gone."""
    now_ms = int(time.time() * 1000)
    con = sqlite3.connect(str(temp_db))
    cur = con.cursor()

    pair_id = "pair_flat"
    cur.execute("""
        INSERT INTO orders (id, order_id, condition_id, token_id, side, price, original_size, status, posted_ts, last_polled_ts, pair_id, max_pair_cost_at_post)
        VALUES ('uuid-up',   'clob-up',   'cond-1', 'tok-up', 'BUY',  0.54, 10.0, 'filled',    ?, ?, ?, 0.98),
               ('uuid-dn',   'clob-dn',   'cond-1', 'tok-dn', 'BUY',  0.43, 10.0, 'cancelled', ?, ?, ?, 0.98),
               ('uuid-sell', 'clob-sell', 'cond-1', 'tok-up', 'SELL', 0.52, 10.0, 'filled',    ?, ?, ?, 0.98)
    """, (now_ms - 30000, now_ms, pair_id,
          now_ms - 30000, now_ms, pair_id,
          now_ms - 5000, now_ms, pair_id))
    cur.executemany("""
        INSERT INTO fills (trade_id, order_uuid, size, price, venue_ts) VALUES (?, ?, ?, ?, ?)
    """, [("trade-up", "uuid-up", 10.0, 0.54, now_ms - 25000),
          ("trade-sell", "uuid-sell", 10.0, 0.52, now_ms - 2000)])
    con.commit()
    con.close()

    pair = summarize_state(temp_db)["pairs"][0]
    assert pair["hedge_state"] == "CLOSED"
    assert pair["naked_info"] is None


def test_pair_spanning_three_tokens_is_refused_not_reduced(temp_db):
    """More than two tokens is refused, matching live_pairs.load_pair.

    Reducing three tokens to the largest two would size a decision against a
    position that the dropped leg partly offsets.
    """
    now_ms = int(time.time() * 1000)
    con = sqlite3.connect(str(temp_db))
    cur = con.cursor()

    pair_id = "pair_three_tokens"
    cur.execute("""
        INSERT INTO orders (id, order_id, condition_id, token_id, side, price, original_size, status, posted_ts, last_polled_ts, pair_id, max_pair_cost_at_post)
        VALUES ('uuid-a', 'clob-a', 'cond-1', 'tok-a', 'BUY', 0.30, 10.0, 'filled', ?, ?, ?, 0.98),
               ('uuid-b', 'clob-b', 'cond-1', 'tok-b', 'BUY', 0.35, 10.0, 'open',   ?, ?, ?, 0.98),
               ('uuid-c', 'clob-c', 'cond-1', 'tok-c', 'BUY', 0.32, 10.0, 'open',   ?, ?, ?, 0.98)
    """, (now_ms, now_ms, pair_id, now_ms, now_ms, pair_id, now_ms, now_ms, pair_id))
    cur.execute("""
        INSERT INTO fills (trade_id, order_uuid, size, price, venue_ts)
        VALUES ('trade-a', 'uuid-a', 10.0, 0.30, ?)
    """, (now_ms - 1000,))
    con.commit()
    con.close()

    pair = summarize_state(temp_db)["pairs"][0]
    assert pair["hedge_state"] == "REFUSED"
    assert "3 token ids" in pair["refused_reason"]


def test_two_orders_on_one_token_is_naked_not_balanced(temp_db):
    """Two filled BUYs on the SAME token are one-sided, never a balanced pair.

    Comparing order slots rather than tokens made two same-side orders look like
    two opposing legs of equal size, reporting BALANCED on a fully naked position.
    """
    now_ms = int(time.time() * 1000)
    con = sqlite3.connect(str(temp_db))
    cur = con.cursor()

    pair_id = "pair_same_token"
    cur.execute("""
        INSERT INTO orders (id, order_id, condition_id, token_id, side, price, original_size, status, posted_ts, last_polled_ts, pair_id, max_pair_cost_at_post)
        VALUES ('uuid-1', 'clob-1', 'cond-1', 'tok-up', 'BUY', 0.50, 10.0, 'filled', ?, ?, ?, 0.98),
               ('uuid-2', 'clob-2', 'cond-1', 'tok-up', 'BUY', 0.50, 10.0, 'filled', ?, ?, ?, 0.98)
    """, (now_ms - 9000, now_ms, pair_id, now_ms - 8000, now_ms, pair_id))
    cur.executemany("""
        INSERT INTO fills (trade_id, order_uuid, size, price, venue_ts) VALUES (?, ?, ?, ?, ?)
    """, [("trade-1", "uuid-1", 10.0, 0.50, now_ms - 7000),
          ("trade-2", "uuid-2", 10.0, 0.50, now_ms - 6000)])
    con.commit()
    con.close()

    pair = summarize_state(temp_db)["pairs"][0]
    assert pair["hedge_state"] == "NAKED"
    assert pair["naked_info"]["unhedged_shares"] == 20.0
    assert pair["naked_info"]["unhedged_dollars"] == 10.00


def test_state_returns_all_orders_including_terminal(temp_db):
    """The read side emits ALL orders; the JS filter hides terminal rows.

    Regression for: live/run/live.db had 720 orders (668 cancelled, 29
    filled, 27 pending, 1 partial); all showed up in one table and buried
    the live view. A server-side filter would be brittle -- anyone calling
    /api/state from a tool would see a partial picture.
    """
    now_ms = int(time.time() * 1000)
    con = sqlite3.connect(str(temp_db))
    cur = con.cursor()
    cur.execute("""
        INSERT INTO orders (id, order_id, condition_id, token_id, side, price, original_size, status, posted_ts, last_polled_ts)
        VALUES ('open-1', 'clob-1', 'cond-1', 'tok-1', 'BUY', 0.5, 10.0, 'open', ?, ?),
               ('pending-1', 'clob-2', 'cond-1', 'tok-2', 'BUY', 0.4, 10.0, 'pending', ?, ?),
               ('partial-1', 'clob-3', 'cond-1', 'tok-3', 'BUY', 0.6, 10.0, 'partial', ?, ?),
               ('filled-1', 'clob-4', 'cond-1', 'tok-4', 'BUY', 0.5, 10.0, 'filled', ?, ?),
               ('cancelled-1', 'clob-5', 'cond-1', 'tok-5', 'BUY', 0.5, 10.0, 'cancelled', ?, ?)
    """, (now_ms, now_ms, now_ms, now_ms, now_ms, now_ms, now_ms, now_ms, now_ms, now_ms))
    con.commit()
    con.close()

    state = summarize_state(temp_db)
    assert len(state["orders"]) == 5, "The read side should return ALL orders"

    active_count = sum(1 for o in state["orders"] if o["status"] in {"open", "pending", "partial"})
    terminal_count = sum(1 for o in state["orders"] if o["status"] in {"filled", "cancelled"})
    assert active_count == 3
    assert terminal_count == 2
