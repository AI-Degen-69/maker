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
                 "per_share_cents": None}


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
    """A dashboard that cannot read one metric must still render the rest."""
    assert _maker_rebate(tmp_path / "absent.db") == {
        "earned": 0.0, "shares": 0.0, "fills": 0, "per_share_cents": None}


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
