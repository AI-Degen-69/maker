"""Sweep duration must be MEASURED by the fleet, not inferred by the page.

The dashboard reported "Fleet is live, but a full sweep is taking 30m41s"
against a fleet whose log showed a completed sweep every 21 seconds. The figure
came from `now - max(_live.ts)`: the age of the freshest per-market payload.
That is not a sweep duration. `visit` returns early -- before the `_live` write
-- for a market it cannot load, and 10 of 20 markets had settled overnight, so
the number was really "how long since the last market that still worked
answered", and it grew without bound.

The tests below pin the separation:

  * the fleet publishes the duration it actually measured,
  * a sweep in progress is visible before it completes,
  * unreadable markets no longer inflate the sweep figure,
  * a market that fails records WHY, without back-dating its figures.
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.fleet_dash import _sweep_duration                    # noqa: E402
from strategy import fleet                                       # noqa: E402

NOW = 1_780_000_000.0


def test_pulse_publishes_the_measured_sweep():
    p = fleet._Pulse()
    time.sleep(0.05)
    p.sweep_done()
    snap = p.snapshot()
    assert snap["sweep_sec"] >= 0.05
    assert snap["sweep_sec"] < 5.0


def test_no_completed_sweep_reports_none_not_zero():
    """A fleet ten markets into its first sweep has measured nothing yet."""
    assert fleet._Pulse().snapshot()["sweep_sec"] is None


def test_sweep_clock_restarts_each_sweep():
    """Two sweeps in a row must each be timed, not accumulated."""
    p = fleet._Pulse()
    p.sweep_done()
    time.sleep(0.05)
    p.sweep_done()
    second = p.snapshot()["sweep_sec"]
    assert 0.05 <= second < 5.0


def test_unreadable_markets_do_not_inflate_the_sweep():
    """THE BUG, STATED AS A TEST.

    Every market last answered half an hour ago -- they have since closed --
    but the fleet is completing a sweep every 21 seconds. The old derivation
    returned 1841s here.
    """
    got = _sweep_duration({"sweep_sec": 21.0, "sweep_elapsed": 3.0},
                          NOW, live_ts=NOW - 1841.0)
    assert got == 21.0


def test_a_wedged_sweep_reports_the_sweep_in_progress():
    """The last sweep took 21s; this one has been running for five minutes."""
    got = _sweep_duration({"sweep_sec": 21.0, "sweep_elapsed": 300.0},
                          NOW, live_ts=NOW - 10.0)
    assert got == 300.0


def test_a_fleet_with_no_pulse_fields_falls_back_to_the_old_figure():
    """An older fleet, mid-upgrade, must still show something."""
    got = _sweep_duration({}, NOW, live_ts=NOW - 60.0)
    assert got == 60.0


def test_nothing_at_all_is_none():
    assert _sweep_duration({}, NOW, live_ts=0.0) is None


def test_specs_mtime_tracks_the_rankers_write(tmp_path, monkeypatch):
    """The adoption trigger. The fleet boots with whatever markets.json is on
    disk, the ranker's first write lands seconds later, and with a one-hour
    interval the fleet traded the stale universe for the whole hour."""
    monkeypatch.setattr(fleet, "RUN", tmp_path)
    f = tmp_path / "markets.json"
    f.write_text("[]", encoding="utf-8")
    first = fleet.specs_mtime()
    assert first > 0

    time.sleep(0.01)
    f.write_text('[{"cid": "0xdeadbeef"}]', encoding="utf-8")
    assert fleet.specs_mtime() != first


def test_missing_markets_file_is_zero_not_an_exception(tmp_path, monkeypatch):
    """A stat that fails must not stop the trading loop."""
    monkeypatch.setattr(fleet, "RUN", tmp_path)
    assert fleet.specs_mtime() == 0.0


class _St:
    """The two attributes `_stamp_failure` touches."""

    def __init__(self, live=None):
        self.spec = {"_live": live} if live is not None else {}
        self.err = ""


def test_a_failed_visit_records_why():
    st = _St()
    fleet._stamp_failure(st, NOW, "closed / not accepting orders")
    assert st.spec["_live"]["err"] == "closed / not accepting orders"
    assert st.spec["_live"]["err_ts"] == NOW


def test_a_failed_visit_does_not_back_date_its_figures():
    """`ts` dates the FIGURES. A market failing for six hours must not look
    freshly measured just because the fleet keeps trying it."""
    st = _St({"ts": NOW - 21600.0, "income": 4.2})
    fleet._stamp_failure(st, NOW, "book fetch: timeout")
    assert st.spec["_live"]["ts"] == NOW - 21600.0
    assert st.spec["_live"]["income"] == 4.2
    assert st.spec["_live"]["err_ts"] == NOW


def test_a_market_that_never_loaded_still_gets_a_payload():
    """Ten of twenty markets had no `_live` at all, so the page could not say
    they were broken -- only that they were missing."""
    st = _St()
    fleet._stamp_failure(st, NOW, "market unloadable (cooling down)")
    assert st.spec["_live"]["err"]
    assert "ts" not in st.spec["_live"], "no figures were measured"
