"""Record raw order books for the live BTC 5-min market to a standalone DB.

WHY A RECORDER AND NOT JUST INSTRUMENTING THE BOT
-------------------------------------------------
The open question is the real fill rate now that the phantom-fill bug is gone.
Measuring it by running the bot would answer it for exactly ONE quoting rule,
and every variant (rest at ask-1tick vs at the bid, chase the ask vs sit still,
price band on vs off) would need another hour of live data. Worse, the strategy
is about to change (price band + timing rule), so a bot-derived number would be
obsolete on arrival.

Raw books are strategy-independent. Record once, replay any rule offline,
compare rules on the SAME market data instead of on different hours. It also
means the fill-rate claim is reproducible: the input is on disk, not gone.

Writes to books.db, never to maker.db. Does not import strategy.store, so it
cannot touch the bot's data even by accident.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategy.markets import fetch_live_market          # noqa: E402
from strategy.net_config import load_net                # noqa: E402

log = logging.getLogger("recorder")

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,           -- wall clock of the poll
    condition_id TEXT NOT NULL,
    market_slug TEXT,
    start_ts REAL,              -- window open
    end_ts REAL,                -- window close
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,         -- UP | DOWN (which outcome this token is)
    bids TEXT NOT NULL,         -- json {price: size}
    asks TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_s_cond ON snapshots(condition_id, ts);
CREATE INDEX IF NOT EXISTS idx_s_ts ON snapshots(ts);

-- One row per window we saw, so replay knows the full market list even for
-- windows where every poll happened to fail.
CREATE TABLE IF NOT EXISTS windows (
    condition_id TEXT PRIMARY KEY,
    market_slug TEXT,
    start_ts REAL,
    end_ts REAL,
    up_token TEXT,
    down_token TEXT,
    first_seen REAL,
    last_seen REAL,
    polls INTEGER DEFAULT 0
);
"""


def db(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(path))
    c.executescript(SCHEMA)
    return c


def book(clob_host: str, token_id: str) -> dict:
    r = requests.get(f"{clob_host}/book", params={"token_id": token_id}, timeout=10)
    r.raise_for_status()
    b = r.json()
    return {
        "bids": {round(float(x["price"]), 4): float(x["size"])
                 for x in (b.get("bids") or [])},
        "asks": {round(float(x["price"]), 4): float(x["size"])
                 for x in (b.get("asks") or [])},
    }


def run(out: Path, interval: float, minutes: float) -> None:
    net = load_net()
    conn = db(out)
    deadline = time.time() + minutes * 60 if minutes > 0 else float("inf")
    market = None
    last_market_fetch = 0.0
    polls = 0
    windows = set()

    log.info("recording to %s | interval %.1fs | for %s",
             out, interval, f"{minutes:.0f}m" if minutes > 0 else "ever")

    while time.time() < deadline:
        now = time.time()
        # Refresh the market when we have none or the current window expired.
        if market is None or now >= market.end_ts or (now - last_market_fetch) > 60:
            try:
                m = fetch_live_market(net.gamma_host, net.series_slug)
                last_market_fetch = now
                if m and (market is None or m.condition_id != market.condition_id):
                    log.info("window %s  t_remaining=%.0fs", m.market_slug,
                             m.end_ts - now)
                market = m
            except Exception as e:
                log.warning("market fetch failed: %s", e)
        if market is None:
            time.sleep(2.0)
            continue

        try:
            up = book(net.clob_host, market.up_token)
            dn = book(net.clob_host, market.down_token)
        except Exception as e:
            log.warning("book fetch failed: %s", e)
            time.sleep(interval)
            continue

        ts = time.time()
        rows = [
            (ts, market.condition_id, market.market_slug, market.start_ts,
             market.end_ts, market.up_token, "UP",
             json.dumps(up["bids"]), json.dumps(up["asks"])),
            (ts, market.condition_id, market.market_slug, market.start_ts,
             market.end_ts, market.down_token, "DOWN",
             json.dumps(dn["bids"]), json.dumps(dn["asks"])),
        ]
        conn.executemany(
            "INSERT INTO snapshots (ts, condition_id, market_slug, start_ts, "
            "end_ts, token_id, side, bids, asks) VALUES (?,?,?,?,?,?,?,?,?)", rows)
        conn.execute(
            "INSERT INTO windows (condition_id, market_slug, start_ts, end_ts, "
            "up_token, down_token, first_seen, last_seen, polls) "
            "VALUES (?,?,?,?,?,?,?,?,1) "
            "ON CONFLICT(condition_id) DO UPDATE SET last_seen=excluded.last_seen, "
            "polls = polls + 1",
            (market.condition_id, market.market_slug, market.start_ts,
             market.end_ts, market.up_token, market.down_token, ts, ts))
        conn.commit()

        polls += 1
        windows.add(market.condition_id)
        if polls % 60 == 0:
            log.info("%d polls | %d windows", polls, len(windows))

        time.sleep(max(0.0, interval - (time.time() - ts)))

    log.info("done: %d polls over %d windows", polls, len(windows))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(ROOT / "books.db"))
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--minutes", type=float, default=0.0, help="0 = run forever")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    try:
        run(Path(a.out), a.interval, a.minutes)
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
