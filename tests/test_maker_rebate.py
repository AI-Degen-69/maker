"""The Maker Rebates Program pays on MATCHED volume, never on waiting.

Written after a plan proposed reporting rebates as `score_share x pot x
uptime` -- the resting-size formula. That is the LIQUIDITY REWARDS program,
which is a different product and reads $0 here because every market the fleet
currently holds publishes clobRewards: 0. Conflating the two would have
labelled a spread-capture projection as a venue rebate and added it to the
headline a second time, on top of the booked P&L those same fills already
produced.

The rebate is a share of the taker fee paid on volume we made:

    rebate = rebate_rate * (shares * fee_rate * p * (1 - p))

so an unfilled resting order earns exactly zero no matter how long it rests,
and a crossed fill earns zero because we were the taker on it -- crediting our
own aggressive leg with a MAKER rebate would pay us for the side we are also
being charged a fee on.
"""
import itertools
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import fleet_dash  # noqa: E402
from server.fleet_dash import _maker_rebate  # noqa: E402
from strategy.config import load as load_config  # noqa: E402

CFG = load_config()

# pytest truncates the tmp_path basename to 30 characters, so two of these
# test names resolve to the SAME directory -- and a second CREATE TABLE in it
# fails with "table fills already exists". Counting the files keeps each
# fixture distinct whether the collision is across tests or inside one.
_seq = itertools.count()


def _db(tmp_path: Path, rows: list[tuple[float, float, int]]) -> Path:
    """A fills table holding (price, size, crossed) and nothing else.

    The dashboard opens the fleet DB read-only, so the fixture builds the one
    table under test rather than importing the full schema -- a rebate reader
    that needs the other fourteen tables to exist is coupled to things it does
    not read.
    """
    p = tmp_path / f"fleet{next(_seq)}.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE fills (price REAL, size REAL, crossed INTEGER)")
    c.executemany("INSERT INTO fills VALUES (?,?,?)", rows)
    c.commit()
    c.close()
    return p


def _expect(price: float, size: float) -> float:
    """The rebate one fill should pay.

    Compared with `approx` at every call site, because the reader multiplies
    these same five factors in its own order and float multiplication is not
    associative -- the groupings land 1e-16 apart. Exact equality would pin
    the ORDER of the arithmetic, which is not the contract; the rate is.
    """
    return CFG.rebate_rate * size * CFG.fee_rate * price * (1.0 - price)


def test_maker_fill_earns_its_share_of_the_taker_fee(tmp_path):
    r = _maker_rebate(_db(tmp_path, [(0.50, 100.0, 0)]))
    assert r["earned"] == pytest.approx(_expect(0.50, 100.0))
    assert r["shares"] == 100.0
    assert r["fills"] == 1


def test_crossed_fill_earns_nothing(tmp_path):
    """We were the taker on it. It pays a fee, it does not collect one."""
    r = _maker_rebate(_db(tmp_path, [(0.50, 100.0, 1)]))
    assert r["earned"] == 0.0
    assert r["shares"] == 0.0
    assert r["fills"] == 0


def test_only_maker_shares_count_when_the_book_holds_both(tmp_path):
    r = _maker_rebate(_db(tmp_path, [(0.50, 100.0, 0), (0.50, 40.0, 1),
                                     (0.20, 50.0, 0)]))
    assert r["earned"] == pytest.approx(_expect(0.50, 100.0)
                                        + _expect(0.20, 50.0))
    assert r["shares"] == 150.0
    assert r["fills"] == 2


def test_resting_size_alone_earns_zero(tmp_path):
    """The whole point. No fills means no rebate, however long we quoted."""
    r = _maker_rebate(_db(tmp_path, []))
    assert r == {"earned": 0.0, "shares": 0.0, "fills": 0,
                 "per_share_cents": None, "err": ""}


def test_rebate_scales_with_price_toward_the_fee_peak(tmp_path):
    """fee = p(1-p) peaks at 0.50, so the same size pays less out at the tails.

    Guards the shape of the formula, not just its value: a rebate that did not
    move with price would mean the taker-fee curve had been dropped for a flat
    per-share rate.
    """
    mid = _maker_rebate(_db(tmp_path, [(0.50, 100.0, 0)]))["earned"]
    tail = _maker_rebate(_db(tmp_path, [(0.05, 100.0, 0)]))["earned"]
    assert mid > tail > 0.0


def test_per_share_cents_reports_the_rate_actually_achieved(tmp_path):
    r = _maker_rebate(_db(tmp_path, [(0.50, 100.0, 0)]))
    assert r["per_share_cents"] == 100.0 * r["earned"] / 100.0


def test_missing_database_reads_as_zero_not_an_error(tmp_path):
    """No DB yet is not a failure -- $0.00 earned is the honest answer."""
    assert _maker_rebate(tmp_path / "absent.db") == {
        "earned": 0.0, "shares": 0.0, "fills": 0, "per_share_cents": None,
        "err": ""}


def _append(p: Path, rows: list[tuple[float, float, int]]) -> None:
    c = sqlite3.connect(p)
    c.executemany("INSERT INTO fills VALUES (?,?,?)", rows)
    c.commit()
    c.close()


def test_new_fills_accumulate_onto_the_running_total(tmp_path):
    """The read is incremental, so it must still equal a full scan.

    `fills` is append-only, so only rows past the last rowid are read. That is
    an optimisation the caller must not be able to observe: polling twice with
    new fills in between has to give the same answer as scanning everything.
    """
    p = _db(tmp_path, [(0.50, 100.0, 0)])
    first = _maker_rebate(p)
    assert first["fills"] == 1

    _append(p, [(0.20, 50.0, 0), (0.50, 40.0, 1)])   # one maker, one crossed
    second = _maker_rebate(p)

    assert second["fills"] == 2, "the crossed fill must still be excluded"
    assert second["shares"] == 150.0
    assert second["earned"] == pytest.approx(_expect(0.50, 100.0)
                                             + _expect(0.20, 50.0))


def test_polling_without_new_fills_does_not_double_count(tmp_path):
    """The failure mode of a running total: adding the same rows twice."""
    p = _db(tmp_path, [(0.50, 100.0, 0), (0.20, 50.0, 0)])
    once = _maker_rebate(p)
    for _ in range(5):
        again = _maker_rebate(p)
    assert again == once


def test_a_fill_landing_mid_read_is_not_counted_twice(tmp_path):
    """THE CHECKPOINT MUST COVER EXACTLY WHAT WAS COUNTED.

    MAX(rowid) and the row SELECT are separate statements, and the fleet writes
    continuously while the dashboard polls every 4s. A fill committed between
    them used to be included in the rows but not covered by the stored
    checkpoint, so the next poll added its rebate a second time -- on a money
    figure, silently.

    Simulated deterministically by inserting from a sqlite3 hook that fires
    between the two statements, rather than hoping to lose a real race.
    """
    p = _db(tmp_path, [(0.50, 100.0, 0)])

    inserted = []

    def racer():
        if not inserted:
            inserted.append(True)
            c = sqlite3.connect(p)
            c.execute("INSERT INTO fills VALUES (0.20, 50.0, 0)")
            c.commit()
            c.close()

    real_connect = sqlite3.connect

    def connect(*a, **kw):
        conn = real_connect(*a, **kw)
        # Fire on the ROW SELECT, not on MAX(rowid). The callback runs just
        # before its statement executes, so hooking MAX would insert too early
        # -- the checkpoint would already cover the new row and no race occurs.
        # Hooking the SELECT places the insert exactly between the two.
        conn.set_trace_callback(
            lambda stmt: racer() if "rowid >" in stmt else None)
        return conn

    fleet_dash.sqlite3.connect = connect
    try:
        first = _maker_rebate(p)
    finally:
        fleet_dash.sqlite3.connect = real_connect

    second = _maker_rebate(p)

    # Whatever the first poll saw, the second must not re-add anything.
    full = _expect(0.50, 100.0) + _expect(0.20, 50.0)
    assert second["fills"] == 2, "both fills counted exactly once"
    assert second["shares"] == 150.0
    assert second["earned"] == pytest.approx(full), (
        f"double-counted: {second['earned']} vs {full} (first poll {first['earned']})")


def test_a_replaced_database_restarts_the_total(tmp_path):
    """Archiving the DB restarts rowids, so the carried total is not ours.

    Without the guard the running total would survive into a database that
    never earned it, and new fills would stack on top of a stranger's history.
    """
    p = _db(tmp_path, [(0.50, 100.0, 0), (0.50, 100.0, 0)])
    assert _maker_rebate(p)["fills"] == 2

    # Recreate at the SAME path, as archiving then restarting does.
    p.unlink()
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE fills (price REAL, size REAL, crossed INTEGER)")
    c.execute("INSERT INTO fills VALUES (0.20, 10.0, 0)")
    c.commit()
    c.close()

    after = _maker_rebate(p)
    assert after["fills"] == 1, "the new DB has one fill, not three"
    assert after["shares"] == 10.0
    assert after["earned"] == pytest.approx(_expect(0.20, 10.0))


def test_unreadable_table_reports_an_error_rather_than_a_silent_zero(tmp_path):
    """THE DISTINCTION THAT MATTERS ON A MONEY FIGURE.

    A DB that exists but has no `fills` table is a broken read, not an empty
    one. Both produce $0.00, and the caller must be able to tell them apart --
    otherwise the page states "we earned nothing" when the truth is "we could
    not find out". The read still returns rather than raising, because one
    unreadable metric must not blank the whole dashboard.
    """
    p = tmp_path / f"fleet{next(_seq)}.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE unrelated (x INTEGER)")
    c.commit()
    c.close()
    r = _maker_rebate(p)
    assert r["earned"] == 0.0
    assert r["err"], "a failed read must say so"
    assert "fills" in r["err"], f"error should name the missing table: {r['err']}"


def test_a_healthy_read_leaves_err_empty(tmp_path):
    """The happy path must not set `err`, or the UI would show a dash."""
    assert _maker_rebate(_db(tmp_path, [(0.50, 100.0, 0)]))["err"] == ""


def test_connection_is_closed_even_when_the_query_fails(tmp_path):
    """The handle used to leak: close() sat after the statement that raises.

    Proven by deleting the file afterwards -- Windows refuses to unlink a file
    an open handle still holds, so a successful delete is the evidence.
    """
    p = tmp_path / f"fleet{next(_seq)}.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE unrelated (x INTEGER)")
    c.commit()
    c.close()
    assert _maker_rebate(p)["err"]
    p.unlink()  # raises PermissionError on Windows if the handle leaked
    assert not p.exists()


def test_null_columns_do_not_crash_the_reader(tmp_path):
    """SQLite will hand back NULL for an unwritten column; treat it as zero."""
    p = tmp_path / f"fleet{next(_seq)}.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE fills (price REAL, size REAL, crossed INTEGER)")
    c.execute("INSERT INTO fills VALUES (NULL, NULL, NULL)")
    c.commit()
    c.close()
    r = _maker_rebate(p)
    assert r["earned"] == 0.0
    assert r["fills"] == 1
