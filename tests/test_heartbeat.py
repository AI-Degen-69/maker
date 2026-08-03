"""The liveness indicator must measure the LOOP, not the sweep.

The dashboard reported "Fleet heartbeat is stale (3m26s old). Displayed figures
are historical, not live." against a fleet that was trading normally. The cause
was the signal itself: the only thing the page could see was
`run/fleet_state.json`, written once per COMPLETE sweep, so the indicator was
really measuring sweep duration. A healthy 20-market sweep is 50-70s and one
slow venue takes it past the 120s threshold.

The fix separates the two. `strategy.fleet` stamps an in-memory pulse once per
market visit and a background thread publishes it every 10s. The tests below
pin the three properties that make that honest:

  * a slow sweep no longer reads as dead,
  * a WEDGED loop still reads as dead even though the writer thread is alive,
  * the loop's own clock is what gets published, never the writer's.
"""
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.fleet_dash import STALE_AFTER_SEC, _heartbeat, _pulse  # noqa: E402
from strategy import fleet  # noqa: E402

NOW = 1_780_000_000.0


def test_slow_sweep_is_not_reported_dead():
    """The bug, stated as a test.

    Sweep finished 3m26s ago (the observed figure) but the loop pulsed two
    seconds ago. Before the pulse existed this was STALE.
    """
    ts, age, stale, src = _heartbeat(
        NOW, live_ts=NOW - 206.0, state_mtime=NOW - 206.0,
        pulse={"loop_ts": NOW - 2.0})
    assert not stale
    assert src == "loop"
    assert age < 3.0
    assert ts == NOW - 2.0


def test_wedged_loop_is_still_reported_dead():
    """The property the fix must not trade away.

    The writer thread is alive and the file is fresh -- `written_ts` is two
    seconds old -- but the loop has not advanced in five minutes. A heartbeat
    that published the WRITER's clock would call this fleet healthy, which is
    precisely the failure the indicator exists to catch.
    """
    _, age, stale, _ = _heartbeat(
        NOW, live_ts=0.0, state_mtime=0.0,
        pulse={"loop_ts": NOW - 300.0, "written_ts": NOW - 2.0})
    assert stale
    assert age > STALE_AFTER_SEC


def test_no_pulse_falls_back_to_the_state_file():
    """A fleet too old to publish a pulse must keep working as before."""
    _, _, stale, src = _heartbeat(NOW, live_ts=NOW - 10.0,
                                  state_mtime=NOW - 10.0, pulse={})
    assert not stale
    assert src == "sweep"


def test_nothing_at_all_is_stale_not_healthy():
    _, age, stale, src = _heartbeat(NOW, 0.0, 0.0, {})
    assert stale and age is None and src == "none"


def test_pulse_publishes_the_loop_clock_not_the_writers(tmp_path, monkeypatch):
    """`loop_ts` is the loop's stamp; `written_ts` is the thread's."""
    monkeypatch.setattr(fleet, "PULSE_FILE", tmp_path / "fleet_pulse.json")
    p = fleet._Pulse()
    p.touch("BTC above 100k", 20)
    touched = p.snapshot()["loop_ts"]

    time.sleep(0.05)
    fleet._write_pulse(p)
    out = json.loads((tmp_path / "fleet_pulse.json").read_text(encoding="utf-8"))

    assert out["loop_ts"] == touched
    assert out["written_ts"] > out["loop_ts"]
    assert out["market"] == "BTC above 100k"
    assert out["markets"] == 20
    assert out["iterations"] == 1


def test_pulse_writer_thread_keeps_running_after_a_write_failure(monkeypatch):
    """A heartbeat that cannot be written must not kill the thread.

    The writer is the only thing publishing liveness; if one bad write ended
    it, the fleet would go permanently STALE for a transient disk error.
    """
    calls = []

    def boom(_pulse_obj):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk full")

    monkeypatch.setattr(fleet, "_write_pulse", boom)
    stop = threading.Event()
    t = threading.Thread(target=fleet._pulse_writer,
                         args=(fleet._Pulse(), stop, 0.01), daemon=True)
    t.start()
    time.sleep(0.1)
    stop.set()
    t.join(timeout=1.0)

    assert not t.is_alive()
    assert len(calls) > 1, "thread stopped after the first failed write"


def test_dashboard_reads_a_published_pulse(tmp_path, monkeypatch):
    """End to end: what fleet writes is what the dashboard parses."""
    import server.fleet_dash as dash

    monkeypatch.setattr(fleet, "PULSE_FILE", tmp_path / "fleet_pulse.json")
    monkeypatch.setattr(dash, "RUN", tmp_path)

    p = fleet._Pulse()
    p.touch("ETH above 5k", 12)
    p.sweep_done()
    fleet._write_pulse(p)

    got = _pulse()
    assert got["markets"] == 12
    assert got["sweeps"] == 1
    assert got["loop_ts"] > 0


def test_unreadable_pulse_degrades_to_empty(tmp_path, monkeypatch):
    """Garbage on disk must not raise out of the endpoint."""
    import server.fleet_dash as dash

    monkeypatch.setattr(dash, "RUN", tmp_path)
    (tmp_path / "fleet_pulse.json").write_text("{truncated", encoding="utf-8")
    assert _pulse() == {}
