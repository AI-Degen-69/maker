"""Tradability and horizon gate (U6).

The fleet ran ~74 hours and produced 9 tape-backed fills and zero resolutions.
Neither is a strategy result: the ranker sorts by reward income per dollar of
capital, and that metric prefers a thin book by construction (see the
`rank_markets` docstring -- a $50/day market with a thin book outranks a
$300/day one). Thin books are thin because nobody trades them.

Measured on the 11.6h run of 2026-07-31: 20 markets produced 48 tape prints
between them and 9 of the 20 traded not once. Every market in the universe
resolved between September 2026 and 2027, so no run of any practical length
could observe a settlement.

Two filters the ranker never had:

  * VOLUME. A market that does not trade cannot fill a resting order, whatever
    its reward yield. Reward income is real, but it is a different strategy and
    must not be measured with fill-based instruments.
  * HORIZON. A market resolving in 2027 cannot contribute a settled P&L
    observation to a run measured in days.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.rank_markets import (                            # noqa: E402
    MAX_DAYS_TO_RESOLVE, MIN_VOLUME_24H, days_to_resolve,
    gamma_spread_universe, tradable,
)
from strategy.config import load as load_cfg                  # noqa: E402


# --- the volume gate --------------------------------------------------------

def test_a_market_that_does_not_trade_is_rejected():
    """The 2026-07-31 universe, restated: no tape, no fills, at any yield."""
    ok, why = tradable(volume_24h=0.0, days=3.0)
    assert not ok
    assert "volume" in why


def test_a_thin_market_below_the_volume_floor_is_rejected():
    ok, why = tradable(volume_24h=MIN_VOLUME_24H - 1, days=3.0)
    assert not ok
    assert "volume" in why


def test_a_liquid_market_inside_the_horizon_is_accepted():
    ok, why = tradable(volume_24h=MIN_VOLUME_24H * 10, days=3.0)
    assert ok
    assert why == ""


# --- the horizon gate -------------------------------------------------------

def test_a_market_resolving_beyond_the_horizon_is_rejected():
    """"Will Canada's 2026 inflation be between 2.5% and 2.9%?" cannot settle
    inside a run, so it can never contribute a P&L observation."""
    ok, why = tradable(volume_24h=MIN_VOLUME_24H * 10,
                       days=MAX_DAYS_TO_RESOLVE + 1)
    assert not ok
    assert "horizon" in why


def test_an_already_expired_market_is_rejected():
    ok, why = tradable(volume_24h=MIN_VOLUME_24H * 10, days=-1.0)
    assert not ok
    assert "horizon" in why


def test_an_unknown_end_date_is_rejected_rather_than_assumed_near():
    """Unknown horizon is the long-dated case in disguise -- the universe that
    produced zero resolutions was entirely long-dated. Guessing 'near' would
    readmit exactly what this gate exists to exclude."""
    ok, why = tradable(volume_24h=MIN_VOLUME_24H * 10, days=None)
    assert not ok
    assert "horizon" in why


def test_an_unknown_volume_is_rejected_rather_than_assumed_liquid():
    ok, why = tradable(volume_24h=None, days=3.0)
    assert not ok
    assert "volume" in why


# --- horizon arithmetic -----------------------------------------------------

def test_days_to_resolve_reads_the_iso_end_date():
    # 2026-08-02T00:00:00Z is one day after 2026-08-01T00:00:00Z.
    d = days_to_resolve("2026-08-02T00:00:00Z", now_iso="2026-08-01T00:00:00Z")
    assert d == pytest.approx(1.0, abs=1e-6)


def test_days_to_resolve_is_none_when_the_venue_gives_no_end_date():
    assert days_to_resolve(None, now_iso="2026-08-01T00:00:00Z") is None
    assert days_to_resolve("", now_iso="2026-08-01T00:00:00Z") is None


class _StubResponse:
    def __init__(self, rows):
        self._rows = rows

    def json(self):
        return self._rows


class _StubGamma:
    """A Gamma that caps a page at 100 rows however large a `limit` is asked for.

    That is the real endpoint's behaviour, measured 2026-08-02: limit=100, 250
    and 500 all returned exactly 100 rows.
    """

    CAP = 100

    def __init__(self, total, volume=1e9):
        self.total = total
        self.volume = volume
        self.offsets = []

    def get(self, url, params=None, timeout=None):
        self.offsets.append(params["offset"])
        start = params["offset"]
        n = max(0, min(self.CAP, params["limit"], self.total - start))
        return _StubResponse([{
            "conditionId": f"0x{start + i:04x}",
            "volume24hr": self.volume,
            "enableOrderBook": True, "acceptingOrders": True,
            "clobRewards": None,
            "clobTokenIds": f'["{start + i}a", "{start + i}b"]',
            "spread": 0.01, "bestBid": 0.49, "bestAsk": 0.51,
            "endDate": "2026-08-03T00:00:00Z",
        } for i in range(n)])


def test_paging_advances_by_rows_returned_not_by_the_limit_requested():
    """The endpoint caps a page below the requested limit, so stepping the
    offset by the REQUESTED size skips whatever the cap withheld.

    At the old per_page=250 the second request started at offset 250 while the
    first response had ended at 99 -- rows 100-249 were never fetched, and they
    exist. Unreachable in practice only because the volume floor usually stops
    the scan inside the first page, which is luck rather than design.
    """
    g = _StubGamma(total=1000)
    rows = gamma_spread_universe(g, pages=3, per_page=250)

    assert g.offsets == [0, 100, 200], (
        f"offsets must follow the rows actually returned, got {g.offsets}")
    assert len(rows) == 300, "no row may be skipped between pages"
    ids = [r["condition_id"] for r in rows]
    assert len(set(ids)) == len(ids), "a page must not be fetched twice"


def test_paging_stops_on_a_short_page_without_a_wasted_request():
    """A page smaller than the established size is the last one.

    Short is judged against the size the endpoint actually served on the first
    response, not against `per_page` -- comparing to the request is what made
    every capped response look like the end of the listing.
    """
    g = _StubGamma(total=150)
    rows = gamma_spread_universe(g, pages=5, per_page=100)
    assert g.offsets == [0, 100], "the 50-row page ends it; no empty tail fetch"
    assert len(rows) == 150


def test_an_oversized_limit_no_longer_ends_the_scan_after_one_page():
    """The original bug, stated directly.

    Gamma caps a page at 100 however large a limit is asked for, so at
    per_page=250 `len(rows) < per_page` held on every response and the loop
    broke after page 0. `pages` was never honoured and the scan never saw past
    the first 100 markets.
    """
    g = _StubGamma(total=1000)
    gamma_spread_universe(g, pages=2, per_page=250)
    assert len(g.offsets) == 2, (
        f"an oversized limit must not end the scan after one page: {g.offsets}")


def test_days_to_resolve_survives_an_end_date_with_no_timezone():
    """A naive venue timestamp must not abort the ranking run.

    `fromisoformat` returns a naive datetime when the string carries no offset
    and no Z. Subtracting an aware `now` from it raises TypeError, which the
    function's `except ValueError` does not catch -- and `evaluate` calls this
    inside a ThreadPoolExecutor worker, so ONE unqualified endDate from gamma
    took down the whole run. Venue times are UTC, so it must read the same as
    the offset-qualified form.
    """
    naive = days_to_resolve("2026-08-02T00:00:00", now_iso="2026-08-01T00:00:00Z")
    aware = days_to_resolve("2026-08-02T00:00:00Z", now_iso="2026-08-01T00:00:00Z")
    assert naive == aware == pytest.approx(1.0)


def test_days_to_resolve_handles_a_naive_now_as_well():
    """Both sides of the subtraction can arrive unqualified."""
    assert days_to_resolve("2026-08-02T00:00:00",
                           now_iso="2026-08-01T00:00:00") == pytest.approx(1.0)


def test_days_to_resolve_is_negative_once_the_end_date_has_passed():
    d = days_to_resolve("2026-07-31T00:00:00Z", now_iso="2026-08-01T00:00:00Z")
    assert d < 0


# --- the script and the fleet must not drift --------------------------------

def test_the_script_gate_and_the_fleet_gate_agree():
    """Same drift risk the payout floor has: a ranker that admits markets the
    fleet would refuse writes a universe the fleet cannot quote."""
    base = load_cfg()
    assert MIN_VOLUME_24H == base.select_min_volume_24h_usd
    assert MAX_DAYS_TO_RESOLVE == base.select_max_days_to_resolve
