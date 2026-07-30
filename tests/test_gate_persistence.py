"""Restart amnesia: an EXITED verdict must survive the process that made it.

EXITED is the only gate state that is expensive to rediscover. It is the
conclusion of `markout_min_sample` fills proving a market takes money off us,
and the fleet used to throw it away on every restart -- re-entering the market
and buying that same evidence again. A process restart is not new information
about the market.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy import gate  # noqa: E402


def _fresh(monkeypatch, tmp_path, name="gate.db"):
    """Point the store at an empty DB and hand back the module."""
    monkeypatch.setenv("MAKER_DB", str(tmp_path / name))
    from strategy import store
    return store


def test_exit_survives_a_round_trip(monkeypatch, tmp_path):
    store = _fresh(monkeypatch, tmp_path)
    store.save_gate_state("cid-toxic", gate.EXITED)
    assert store.get_gate_state("cid-toxic") == gate.EXITED


def test_an_unknown_market_reports_nothing(monkeypatch, tmp_path):
    store = _fresh(monkeypatch, tmp_path)
    assert store.get_gate_state("never-seen") is None


def test_the_verdict_is_upserted_not_duplicated(monkeypatch, tmp_path):
    """condition_id is the primary key: a market can be re-judged on a later
    run without accumulating rows that disagree with each other."""
    store = _fresh(monkeypatch, tmp_path)
    store.save_gate_state("cid-x", gate.WIDENED)
    store.save_gate_state("cid-x", gate.EXITED)
    with store.db() as c:
        rows = c.execute("SELECT gate_state FROM market_gate "
                         "WHERE condition_id='cid-x'").fetchall()
    assert rows == [(gate.EXITED,)]


def test_a_restarted_market_starts_exited(monkeypatch, tmp_path):
    """The fix itself: fleet's rehydrate reads the ledger, not a default."""
    store = _fresh(monkeypatch, tmp_path)
    from strategy import fleet
    store.save_gate_state("cid-toxic", gate.EXITED)
    assert fleet._gate_from_db("cid-toxic") == gate.EXITED


def test_a_market_with_no_history_starts_normal(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    from strategy import fleet
    assert fleet._gate_from_db("cid-new") == gate.NORMAL


def test_widened_is_deliberately_not_inherited(monkeypatch, tmp_path):
    """Only EXITED is asymmetric. WIDENED is cheap to recompute (one sample,
    at an offset that still earns rent) and recoverable, so carrying it across
    a restart buys nothing and risks freezing a mid-graduation position."""
    store = _fresh(monkeypatch, tmp_path)
    from strategy import fleet
    store.save_gate_state("cid-mid", gate.WIDENED)
    assert fleet._gate_from_db("cid-mid") == gate.NORMAL


def test_a_broken_read_degrades_to_normal(monkeypatch, tmp_path):
    """A rehydrate failure must cost the old bug, not the whole fleet."""
    _fresh(monkeypatch, tmp_path)
    from strategy import fleet, store as store_mod

    def boom(_cid):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store_mod, "get_gate_state", boom)
    assert fleet._gate_from_db("cid-any") == gate.NORMAL
