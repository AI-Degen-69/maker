"""Quote-ledger lifecycle tests."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_mark_cancelled_preserves_partial_fill(monkeypatch, tmp_path):
    monkeypatch.setenv("MAKER_DB", str(tmp_path / "lifecycle.db"))
    from strategy import store

    quote_id = store.log_quote(
        market_slug="m", condition_id="c", token_id="t", side="UP",
        price=0.50, size=100.0, queue_ahead=0.0, mid=0.50,
        edge_vs_mid=0.01, t_remaining=None,
    )
    store.log_fill(
        quote_id=quote_id, market_slug="m", condition_id="c", token_id="t",
        side="UP", price=0.50, size=25.0, mid_at_post=0.50,
        edge_vs_mid=0.01, queue_waited=0.0, seconds_to_fill=1.0,
        crossed=False, reason="tape",
    )

    store.mark_cancelled([quote_id])

    with store.db() as c:
        row = c.execute(
            "SELECT filled, cancelled FROM quotes WHERE id=?", (quote_id,)
        ).fetchone()
    assert row == (25.0, 1)
