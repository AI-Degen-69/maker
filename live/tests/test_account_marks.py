"""Venue-sourced account value and P&L.

The defect these tests pin: the dashboard's headline tile read
`config.bankroll_usd + realized_pnl` = $100.30 for an account the venue valued
at $101.88. `bankroll_usd` is a simulation parameter nobody deposited.

The measured figures below are the real account on 2026-08-19, read from the
Data API: collateral $101.88, no open positions, two closed positions returning
+$0.90 and -$0.60, and a user-pnl series whose newest point is +$0.30.
"""

import json
import urllib.error
from pathlib import Path

import pytest

from engine import account as acct
from engine import venue
from engine.order_registry import OrderRegistry, registry_naked_usd


# ---------------------------------------------------------------------------
# compose_account_mark -- pure, no network
# ---------------------------------------------------------------------------

def test_account_value_is_collateral_plus_positions():
    m = acct.compose_account_mark(
        collateral_usd=101.88,
        positions_value_usd=0.0,
        open_positions=[],
        closed_positions=[],
        user_pnl_usd=0.30,
    )
    assert m["account_value_usd"] == pytest.approx(101.88)


def test_account_value_reproduces_the_real_account():
    """The exact figures the venue reported for the funded wallet."""
    m = acct.compose_account_mark(
        collateral_usd=101.88,
        positions_value_usd=0.0,
        open_positions=[],
        closed_positions=[
            {"realizedPnl": 0.9, "conditionId": "0x70de"},
            {"realizedPnl": -0.6, "conditionId": "0x70de"},
        ],
        user_pnl_usd=0.3,
    )
    assert m["account_value_usd"] == pytest.approx(101.88)
    assert m["pnl_usd"] == pytest.approx(0.30)
    assert m["pnl_closed_usd"] == pytest.approx(0.30)
    # Polymarket displayed +0.30% past day; the basis is net deposits $101.58.
    assert m["pnl_pct"] == pytest.approx(0.2953, abs=1e-3)
    assert round(m["pnl_pct"], 2) == 0.30
    assert m["pnl_source_gap"] == pytest.approx(0.0)


def test_account_value_is_none_when_collateral_unavailable():
    """Positions-only is not the account total, and must not be reported as it."""
    m = acct.compose_account_mark(
        collateral_usd=None,
        positions_value_usd=12.0,
        open_positions=[],
        closed_positions=[],
        user_pnl_usd=None,
    )
    assert m["account_value_usd"] is None
    assert m["pnl_pct"] is None


def test_account_value_is_none_when_positions_value_unavailable():
    m = acct.compose_account_mark(
        collateral_usd=101.88,
        positions_value_usd=None,
        open_positions=None,
        closed_positions=None,
        user_pnl_usd=None,
    )
    assert m["account_value_usd"] is None


def test_unreached_endpoint_is_null_not_zero():
    """None rows mean 'not reached'; an empty list means 'reached, holds nothing'."""
    unreached = acct.compose_account_mark(
        collateral_usd=100.0, positions_value_usd=0.0,
        open_positions=None, closed_positions=None, user_pnl_usd=None,
    )
    assert unreached["unrealized_usd"] is None
    assert unreached["committed_usd"] is None
    assert unreached["pnl_closed_usd"] is None
    assert unreached["open_positions_count"] is None

    reached = acct.compose_account_mark(
        collateral_usd=100.0, positions_value_usd=0.0,
        open_positions=[], closed_positions=[], user_pnl_usd=None,
    )
    assert reached["unrealized_usd"] == 0.0
    assert reached["committed_usd"] == 0.0
    assert reached["pnl_closed_usd"] == 0.0
    assert reached["open_positions_count"] == 0
    assert unreached["closed_positions_count"] is None
    assert reached["closed_positions_count"] == 0


def test_open_positions_drive_unrealized_and_committed():
    m = acct.compose_account_mark(
        collateral_usd=50.0,
        positions_value_usd=61.50,
        open_positions=[
            {"cashPnl": 8.79, "initialValue": 52.71},
            {"cashPnl": -1.29, "initialValue": 10.00},
        ],
        closed_positions=[],
        user_pnl_usd=None,
    )
    assert m["unrealized_usd"] == pytest.approx(7.50)
    assert m["committed_usd"] == pytest.approx(62.71)
    assert m["open_positions_count"] == 2


def test_pnl_falls_back_to_closed_positions_when_series_missing():
    m = acct.compose_account_mark(
        collateral_usd=101.88, positions_value_usd=0.0,
        open_positions=[],
        closed_positions=[{"realizedPnl": 0.9}, {"realizedPnl": -0.6}],
        user_pnl_usd=None,
    )
    assert m["pnl_usd"] == pytest.approx(0.30)
    assert m["pnl_source_gap"] is None


def test_disagreeing_pnl_sources_are_both_recorded():
    """A gap between the two venue sources is information, not something to hide."""
    m = acct.compose_account_mark(
        collateral_usd=101.88, positions_value_usd=0.0,
        open_positions=[],
        closed_positions=[{"realizedPnl": 0.10}],
        user_pnl_usd=0.30,
    )
    assert m["pnl_series_usd"] == pytest.approx(0.30)
    assert m["pnl_closed_usd"] == pytest.approx(0.10)
    assert m["pnl_source_gap"] == pytest.approx(0.20)
    assert m["pnl_usd"] == pytest.approx(0.30)


def test_pnl_pct_is_none_when_basis_is_zero():
    """A fresh account has no denominator; 0.00% would read as 'flat'."""
    m = acct.compose_account_mark(
        collateral_usd=0.0, positions_value_usd=0.0,
        open_positions=[], closed_positions=[], user_pnl_usd=0.0,
    )
    assert m["pnl_pct"] is None


def test_non_numeric_field_does_not_crash_the_sum():
    m = acct.compose_account_mark(
        collateral_usd=10.0, positions_value_usd=0.0,
        open_positions=[{"cashPnl": "nonsense"}, {"cashPnl": 1.5}],
        closed_positions=[], user_pnl_usd=None,
    )
    assert m["unrealized_usd"] == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# Venue reads -- transport stubbed, never a real request
# ---------------------------------------------------------------------------

def test_value_endpoint_is_read_as_a_list(monkeypatch):
    """GET /value returns [{user, value}]. Reading it as a dict yields None,
    which would value every open book at zero."""
    monkeypatch.setattr(
        acct, "_get_json",
        lambda base, path, params, timeout: [{"user": "0xee", "value": 61.5}],
    )
    assert acct.fetch_positions_value("0xee") == pytest.approx(61.5)


def test_value_endpoint_empty_list_is_none(monkeypatch):
    """No row is 'the venue said nothing', not 'the positions are worth zero'."""
    monkeypatch.setattr(acct, "_get_json", lambda base, path, params, timeout: [])
    assert acct.fetch_positions_value("0xee") is None


def test_positions_request_sets_size_threshold_zero(monkeypatch):
    """The endpoint defaults to sizeThreshold=1 and would drop sub-share dust."""
    seen = {}

    def fake(base, path, params, timeout):
        seen.update(params)
        return []

    monkeypatch.setattr(acct, "_get_json", fake)
    acct.fetch_open_positions("0xee")
    assert seen["sizeThreshold"] == 0


def test_positions_paginate_past_the_first_page(monkeypatch):
    page = [{"asset": str(i), "cashPnl": 1.0} for i in range(acct.POSITIONS_PAGE_SIZE)]
    calls = {"n": 0}

    def fake(base, path, params, timeout):
        calls["n"] += 1
        return page if calls["n"] == 1 else [{"asset": "last", "cashPnl": 1.0}]

    monkeypatch.setattr(acct, "_get_json", fake)
    rows = acct.fetch_open_positions("0xee")
    assert len(rows) == acct.POSITIONS_PAGE_SIZE + 1


def test_network_failure_returns_none_not_empty(monkeypatch):
    """An unreachable venue must not look like an empty portfolio."""
    def boom(base, path, params, timeout):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(acct, "_get_json", boom)
    assert acct.fetch_positions_value("0xee") is None
    assert acct.fetch_open_positions("0xee") is None
    assert acct.fetch_closed_positions("0xee") is None
    assert acct.fetch_user_pnl("0xee") is None


def test_user_pnl_takes_the_newest_point(monkeypatch):
    monkeypatch.setattr(
        acct, "_get_json",
        lambda base, path, params, timeout: [
            {"t": 1787112000, "p": 0.1},
            {"t": 1787126400, "p": 0.3},
            {"t": 1787119200, "p": 0.2},
        ],
    )
    assert acct.fetch_user_pnl("0xee") == pytest.approx(0.3)


def test_user_pnl_empty_series_is_none(monkeypatch):
    monkeypatch.setattr(acct, "_get_json", lambda base, path, params, timeout: [])
    assert acct.fetch_user_pnl("0xee") is None


# ---------------------------------------------------------------------------
# Registry round trip
# ---------------------------------------------------------------------------

def test_account_mark_round_trips_through_the_registry(tmp_path):
    reg = OrderRegistry(db_path=tmp_path / "live.db")
    mark = acct.compose_account_mark(
        collateral_usd=101.88, positions_value_usd=0.0,
        open_positions=[],
        closed_positions=[{"realizedPnl": 0.9}, {"realizedPnl": -0.6}],
        user_pnl_usd=0.3,
    )
    reg.log_account_mark(mark, ts=1000.0, run_id="run-a")

    rows = reg.get_all_account_marks()
    assert len(rows) == 1
    assert rows[0]["account_value_usd"] == pytest.approx(101.88)
    assert rows[0]["pnl_usd"] == pytest.approx(0.30)
    assert rows[0]["source"] == "venue"
    assert rows[0]["run_id"] == "run-a"


def test_registry_stores_unavailable_fields_as_null(tmp_path):
    reg = OrderRegistry(db_path=tmp_path / "live.db")
    reg.log_account_mark(
        acct.compose_account_mark(None, None, None, None, None),
        ts=1000.0, run_id="run-a",
    )
    row = reg.get_all_account_marks()[0]
    assert row["account_value_usd"] is None
    assert row["pnl_usd"] is None
    assert row["unrealized_usd"] is None
    assert row["open_positions_count"] is None


def test_sweep_records_what_the_venue_reported(tmp_path, monkeypatch):
    """account-sweep is read-only at the venue and writes one row locally."""
    from engine import live_exec

    monkeypatch.setenv("POLY_FUNDER", "0xee3b")
    monkeypatch.setattr(live_exec, "fetch_live_balance", lambda who: 101.88)
    monkeypatch.setattr(acct, "fetch_positions_value", lambda f, t=15.0: 0.0)
    monkeypatch.setattr(acct, "fetch_open_positions", lambda f, t=15.0: [])
    monkeypatch.setattr(acct, "fetch_closed_positions",
                        lambda f, t=15.0: [{"realizedPnl": 0.9}, {"realizedPnl": -0.6}])
    monkeypatch.setattr(acct, "fetch_user_pnl",
                        lambda f, interval="all", fidelity="1d", timeout=15.0: 0.3)

    db = tmp_path / "live.db"
    mark = live_exec.account_sweep(db_path=str(db), quiet=True)
    assert mark["account_value_usd"] == pytest.approx(101.88)

    rows = OrderRegistry(db_path=db).get_all_account_marks()
    assert len(rows) == 1
    assert rows[0]["account_value_usd"] == pytest.approx(101.88)
    assert rows[0]["pnl_usd"] == pytest.approx(0.30)


def test_sweep_refuses_without_a_funder(tmp_path, monkeypatch):
    from engine import live_exec

    monkeypatch.delenv("POLY_FUNDER", raising=False)
    with pytest.raises(SystemExit):
        live_exec.account_sweep(db_path=str(tmp_path / "live.db"), quiet=True)


def test_sweep_records_a_partial_read_rather_than_inventing_a_total(tmp_path, monkeypatch):
    """No credentials means no collateral, which means no account value -- and
    the row says so instead of reporting positions-only as the total."""
    from engine import live_exec

    monkeypatch.setenv("POLY_FUNDER", "0xee3b")
    monkeypatch.setattr(live_exec, "fetch_live_balance", lambda who: None)
    monkeypatch.setattr(acct, "fetch_positions_value", lambda f, t=15.0: 61.5)
    monkeypatch.setattr(acct, "fetch_open_positions", lambda f, t=15.0: [])
    monkeypatch.setattr(acct, "fetch_closed_positions", lambda f, t=15.0: [])
    monkeypatch.setattr(acct, "fetch_user_pnl",
                        lambda f, interval="all", fidelity="1d", timeout=15.0: None)

    db = tmp_path / "live.db"
    live_exec.account_sweep(db_path=str(db), quiet=True)
    row = OrderRegistry(db_path=db).get_all_account_marks()[0]
    assert row["account_value_usd"] is None
    assert row["positions_value_usd"] == pytest.approx(61.5)


def test_marks_come_back_in_timestamp_order(tmp_path):
    reg = OrderRegistry(db_path=tmp_path / "live.db")
    for ts, val in ((3000.0, 103.0), (1000.0, 101.0), (2000.0, 102.0)):
        reg.log_account_mark(
            acct.compose_account_mark(val, 0.0, [], [], 0.0), ts=ts, run_id="r")
    assert [r["ts"] for r in reg.get_all_account_marks()] == [1000.0, 2000.0, 3000.0]


def test_closed_positions_count_round_trips(tmp_path):
    reg = OrderRegistry(db_path=tmp_path / "live.db")
    reg.log_account_mark(
        acct.compose_account_mark(
            101.88, 0.0, [], [{"realizedPnl": 0.9}, {"realizedPnl": -0.6}], 0.3),
        ts=1000.0, run_id="r")
    row = reg.get_all_account_marks()[0]
    assert row["closed_positions_count"] == 2
    assert row["pnl_closed_usd"] == pytest.approx(0.30)


def test_client_is_built_once_per_process(monkeypatch):
    """Every client() call used to POST /auth/api-key then GET
    /auth/derive-api-key. Derivation is the most rate-limit-sensitive call in
    the API, and repeated derivations preceded a venue-side stall on this
    account: signed requests hung past 30s while unsigned ones returned in 0.1s.
    """
    from engine import live_exec

    venue._CLIENT_CACHE.clear()
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.setenv("POLY_FUNDER", "0xee3b")

    built = {"n": 0}

    class FakeClient:
        def __init__(self, *a, **kw):
            built["n"] += 1

        def create_or_derive_api_key(self):
            return {"key": "k"}

        def set_api_creds(self, creds):
            pass

    import py_clob_client_v2.client as clob_mod
    monkeypatch.setattr(clob_mod, "ClobClient", FakeClient)

    first = live_exec.client()
    second = live_exec.client()
    assert first is second
    assert built["n"] == 1
    venue._CLIENT_CACHE.clear()


def test_a_different_funder_gets_its_own_client(monkeypatch):
    """`balance --funder` checks a candidate address; it must not reuse a
    client authenticated for a different one."""
    from engine import live_exec

    venue._CLIENT_CACHE.clear()
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0x" + "11" * 32)

    class FakeClient:
        def __init__(self, *a, **kw):
            self.funder = kw.get("funder")

        def create_or_derive_api_key(self):
            return {"key": "k"}

        def set_api_creds(self, creds):
            pass

    import py_clob_client_v2.client as clob_mod
    monkeypatch.setattr(clob_mod, "ClobClient", FakeClient)

    a = live_exec.client(funder="0xaaa")
    b = live_exec.client(funder="0xbbb")
    assert a is not b
    assert a.funder == "0xaaa" and b.funder == "0xbbb"
    venue._CLIENT_CACHE.clear()


def test_stored_credentials_skip_derivation_entirely(monkeypatch):
    """Derivation is the venue's most rate-limit-sensitive endpoint. With the
    three L2 values in the environment, no command may call it: `balance`
    succeeded and `account-sweep` timed out on the same credentials twenty
    seconds later, purely because each derived its own key."""
    from engine import live_exec

    venue._CLIENT_CACHE.clear()
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.setenv("POLY_FUNDER", "0xee3b")
    monkeypatch.setenv("POLY_API_KEY", "k")
    monkeypatch.setenv("POLY_API_SECRET", "s")
    monkeypatch.setenv("POLY_API_PASSPHRASE", "p")

    derived = {"n": 0}

    class FakeClient:
        def __init__(self, *a, **kw):
            self.creds = None

        def create_or_derive_api_key(self):
            derived["n"] += 1
            raise AssertionError("derivation must not be reached")

        def set_api_creds(self, creds):
            self.creds = creds

    import py_clob_client_v2.client as clob_mod
    monkeypatch.setattr(clob_mod, "ClobClient", FakeClient)

    c = live_exec.client()
    assert derived["n"] == 0
    assert c.creds.api_key == "k"
    venue._CLIENT_CACHE.clear()


@pytest.mark.parametrize("missing", ["POLY_API_KEY", "POLY_API_SECRET",
                                     "POLY_API_PASSPHRASE"])
def test_a_partial_credential_set_falls_back_to_derivation(monkeypatch, missing):
    """A half-configured .env would otherwise build a client that fails every
    signed request with an error indistinguishable from a venue outage.

    Asserted through `client()`, not just `api_creds_from_env()`: the point is
    that derivation still happens, and a test that only checks the helper
    returns None would pass even if `client()` stopped deriving on None.
    """
    from engine import live_exec

    venue._CLIENT_CACHE.clear()
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.setenv("POLY_FUNDER", "0xee3b")
    monkeypatch.setenv("POLY_API_KEY", "k")
    monkeypatch.setenv("POLY_API_SECRET", "s")
    monkeypatch.setenv("POLY_API_PASSPHRASE", "p")
    monkeypatch.delenv(missing, raising=False)

    derived = {"n": 0}

    class FakeClient:
        def __init__(self, *a, **kw):
            self.creds = None

        def create_or_derive_api_key(self):
            derived["n"] += 1
            return "derived-creds"

        def set_api_creds(self, creds):
            self.creds = creds

    import py_clob_client_v2.client as clob_mod
    monkeypatch.setattr(clob_mod, "ClobClient", FakeClient)

    assert live_exec.api_creds_from_env() is None
    c = live_exec.client()
    assert derived["n"] == 1
    assert c.creds == "derived-creds"
    venue._CLIENT_CACHE.clear()


def test_credentials_are_read_from_the_environment(monkeypatch):
    from engine import live_exec

    monkeypatch.setenv("POLY_API_KEY", "abc")
    monkeypatch.setenv("POLY_API_SECRET", "def")
    monkeypatch.setenv("POLY_API_PASSPHRASE", "ghi")
    creds = live_exec.api_creds_from_env()
    assert (creds.api_key, creds.api_secret, creds.api_passphrase) == ("abc", "def", "ghi")


def test_env_write_is_atomic_and_never_truncates_on_failure(tmp_path, monkeypatch):
    """.env holds POLY_PRIVATE_KEY. A plain write truncates first, so a crash
    between truncate and flush would destroy the wallet's signing key -- and it
    is not recoverable from anywhere in this repo."""
    from engine import live_exec

    env = tmp_path / ".env"
    original = "POLY_PRIVATE_KEY=0xdeadbeef\n"
    env.write_text(original, encoding="utf-8")

    # A write that fails mid-flight must leave the original byte-for-byte.
    real_open = open

    def exploding_open(path, *args, **kwargs):
        if ".tmp." in str(path):
            raise OSError("disk full")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", exploding_open)
    assert live_exec._atomic_write_text(env, "REPLACED\n") is False
    monkeypatch.undo()
    assert env.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("*.tmp.*")) == []

    # A successful write replaces the content and leaves no temp file behind.
    assert live_exec._atomic_write_text(env, "REPLACED\n") is True
    assert env.read_text(encoding="utf-8") == "REPLACED\n"
    assert list(tmp_path.glob("*.tmp.*")) == []


def test_registry_naked_usd_counts_open_pairs_only(tmp_path):
    """Naked dollars come from open pairs; a merged pair contributes nothing."""
    import time

    from engine import live_exec
    from engine.order_registry import (
        OrderRegistry, OrderRecord, FillRecord, CloseRecord,
    )

    reg = OrderRegistry(db_path=tmp_path / "live.db")
    now = int(time.time() * 1000)

    # Open pair: 5 filled on one leg, 2 on the other -> 3 naked shares at $0.62.
    for oid, tok, price in (("o1", "tok-a", 0.62), ("o2", "tok-b", 0.32)):
        reg.create_order(OrderRecord(
            id=oid, condition_id="cond-open", token_id=tok, side="BUY",
            price=price, original_size=5.0, status="partial",
            posted_ts=now, last_polled_ts=now, pair_id="pair-open",
        ))
    reg.record_fill(FillRecord(trade_id="t1", order_uuid="o1", size=5.0,
                               price=0.62, venue_ts=now, recorded_ts=now, run_id="run-a"))
    reg.record_fill(FillRecord(trade_id="t2", order_uuid="o2", size=2.0,
                               price=0.32, venue_ts=now, recorded_ts=now, run_id="run-a"))

    # Closed pair: fully filled then merged -> no open exposure remains.
    for oid, tok, price in (("o3", "tok-c", 0.60), ("o4", "tok-d", 0.40)):
        reg.create_order(OrderRecord(
            id=oid, condition_id="cond-closed", token_id=tok, side="BUY",
            price=price, original_size=5.0, status="filled",
            posted_ts=now, last_polled_ts=now, pair_id="pair-closed",
        ))
    reg.record_fill(FillRecord(trade_id="t3", order_uuid="o3", size=5.0,
                               price=0.60, venue_ts=now, recorded_ts=now, run_id="run-a"))
    reg.record_fill(FillRecord(trade_id="t4", order_uuid="o4", size=5.0,
                               price=0.40, venue_ts=now, recorded_ts=now, run_id="run-a"))
    reg.log_close(CloseRecord(ts=time.time(), condition_id="cond-closed",
                              method="merge", shares=5.0, cost_basis=5.0,
                              proceeds=5.0, realized_pnl=0.0, run_id="run-a"))

    assert registry_naked_usd(reg) == pytest.approx(3.0 * 0.62)


def test_float_mark_logs_only_when_the_venue_measured(tmp_path):
    """A partial venue read must not fabricate a 0.0 float mark."""
    from engine import live_exec
    from engine.order_registry import OrderRegistry, registry_naked_usd

    reg = OrderRegistry(db_path=tmp_path / "live.db")

    live_exec.log_float_mark_if_measured(
        reg, {"unrealized_usd": None, "committed_usd": 4.7}
    )
    assert reg.get_all_float_marks() == []

    live_exec.log_float_mark_if_measured(
        reg, {"unrealized_usd": 0.30, "committed_usd": 4.7}
    )
    rows = reg.get_all_float_marks()
    assert len(rows) == 1
    assert rows[0]["unrealized_usd"] == pytest.approx(0.30)
    assert rows[0]["committed_open_usd"] == pytest.approx(4.7)
    assert rows[0]["naked_usd"] == 0.0
