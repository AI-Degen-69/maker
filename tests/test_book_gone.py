"""A deleted orderbook is a market state, not a fleet fault.

Observed live on 2026-08-05. Two markets sat in the fleet showing

    book fetch: 404 Client Error: Not Found for url:
    https://clob.polymarket.com/book?token_id=1105290663...

One of them (`atp-shang-rublev-2026-08-04`) held 85 DOWN shares on a match that
had already been won by the other player. The venue answered `/book` with
`{"error": "No orderbook exists for the requested token id"}` on BOTH tokens,
while `/markets/{cid}` still reported `active: true, closed: false,
accepting_orders: true` -- UMA had not finalised it yet, so no settlement row
could be written and the market could not be dropped either.

Three things were wrong with how that read and what it cost:

  * the page called it unreadable and painted a raw HTTPError red, against a
    market an operator could see decided on the venue,
  * the metadata endpoint kept answering, so the market-load cooldown never
    engaged and every rotation spent two requests on a guaranteed 404,
  * the last successful sweep's `capital`/`quotes` stayed on the payload,
    reporting money committed to offers the venue had deleted.

These tests pin the replacement: a 404 is its own exception, a streak of them
is a verdict, and the verdict reads as waiting for settlement.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest                                                    # noqa: E402

from strategy import fleet, main                                 # noqa: E402
from strategy.main import BookGone                               # noqa: E402

NOW = 1_785_900_000.0


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} Client Error")


class _Inv:
    def __init__(self, up=0.0, down=0.0):
        self.up_shares = up
        self.down_shares = down


class _Market:
    up_token = "1105290663"
    down_token = "1452026022"


class _St:
    """Only what `visit` touches before it returns on a book failure."""

    def __init__(self, inv=None):
        self.cfg = None
        self.spec = {}
        self.err = ""
        self.market = _Market()
        self.market_retry_ts = 0.0
        self.book_gone = 0
        self.book_retry_ts = 0.0
        self.inv = inv or _Inv()


class _Cfg:
    clob_host = "https://clob.example"


def _visit(st, now=NOW):
    fleet.visit(st, _Cfg(), now, states=[st])


# --- the exception itself --------------------------------------------------

def test_a_404_is_book_gone_not_a_generic_http_error(monkeypatch):
    """A timeout and a deleted book are opposite facts: one is worth retrying
    next rotation, the other cannot change before settlement."""
    monkeypatch.setattr(main._SESSION, "get",
                        lambda *a, **k: _Resp(404, {"error": "No orderbook exists"}))
    with pytest.raises(BookGone):
        main.full_book("https://clob.example", "1105290663")


def test_other_failures_still_raise_normally(monkeypatch):
    monkeypatch.setattr(main._SESSION, "get", lambda *a, **k: _Resp(503))
    with pytest.raises(Exception) as e:
        main.full_book("https://clob.example", "1105290663")
    assert not isinstance(e.value, BookGone)


# --- the streak ------------------------------------------------------------

def _always_gone(monkeypatch):
    def _boom(host, token):
        raise BookGone(token)
    monkeypatch.setattr(fleet, "full_book", _boom)


def test_one_404_is_not_yet_a_verdict(monkeypatch):
    """The venue can 404 a token mid-deploy. Calling that 'awaiting settlement'
    would hide a real outage behind a reassuring string."""
    _always_gone(monkeypatch)
    st = _St()
    _visit(st)
    assert st.book_gone == 1
    assert st.book_retry_ts == 0.0, "no cooldown before the streak is believed"
    assert st.spec["_live"]["err"], "still reported as a failure"


def test_a_streak_of_404s_reads_as_awaiting_settlement(monkeypatch):
    _always_gone(monkeypatch)
    st = _St()
    for _ in range(fleet.BOOK_GONE_STREAK):
        _visit(st)
    live = st.spec["_live"]
    assert st.book_gone == fleet.BOOK_GONE_STREAK
    assert live["err"] == "", "not a fault -- the page must not call it unreadable"
    assert st.err == ""
    assert "awaiting" in live["why"]
    assert live["err_ts"] == NOW, "the state is still dated"


def test_the_verdict_stops_spending_requests(monkeypatch):
    """The metadata endpoint keeps answering for these markets, so the existing
    market-load cooldown never engages -- this is the one that does."""
    _always_gone(monkeypatch)
    st = _St()
    for _ in range(fleet.BOOK_GONE_STREAK):
        _visit(st)
    assert st.book_retry_ts == NOW + fleet.BOOK_GONE_RETRY_SEC

    calls = []

    def _count(host, token):
        calls.append(token)
        raise BookGone(token)

    monkeypatch.setattr(fleet, "full_book", _count)
    _visit(st, NOW + 1.0)
    assert calls == [], "inside the cooldown, no request is made"
    assert "awaiting" in st.spec["_live"]["why"]


def test_a_reopened_book_clears_the_verdict(monkeypatch):
    _always_gone(monkeypatch)
    st = _St()
    for _ in range(fleet.BOOK_GONE_STREAK):
        _visit(st)
    assert st.book_retry_ts > 0

    # Past the cooldown the market answers again. `visit` goes on to do real
    # work, which this stub state cannot support -- the assertion is only that
    # the verdict was retired before that point.
    monkeypatch.setattr(fleet, "full_book",
                        lambda host, token: {"token_id": token, "bids": {},
                                             "asks": {}, "best_bid": None,
                                             "best_ask": None})
    with pytest.raises(Exception):
        _visit(st, NOW + fleet.BOOK_GONE_RETRY_SEC + 1.0)
    assert st.book_gone == 0
    assert st.book_retry_ts == 0.0


# --- what the operator reads ----------------------------------------------

def test_status_names_the_position_it_is_waiting_on():
    st = _St(_Inv(up=0.0, down=85.0))
    why = fleet._settling_status(st)
    assert "85 DOWN" in why
    assert "settlement" in why


def test_status_without_inventory_says_resolution_not_settlement():
    """Nothing is owed to us here -- the only thing left is the market closing
    so the ranker can drop it."""
    why = fleet._settling_status(_St())
    assert "resolution" in why
    assert "holding" not in why


def test_waiting_clears_capital_the_venue_deleted():
    """`capital` is resting offers. A book that no longer exists has none, and
    the stale figure reported money committed to orders that were gone."""
    st = _St()
    st.spec["_live"] = {"ts": NOW - 600.0, "capital": 79.05, "income": 0.8,
                        "share": 0.0001, "quotes": [{"side": "UP"}]}
    fleet._stamp_waiting(st, NOW, "outcome decided -- awaiting resolution")
    live = st.spec["_live"]
    assert live["capital"] == 0
    assert live["quotes"] == []
    assert live["income"] == 0.0
    assert live["ts"] == NOW - 600.0, "`ts` dates the FIGURES, not the state"
