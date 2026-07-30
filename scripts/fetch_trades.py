"""Backfill the trade tape for every window recorded in books.db.

WHY
---
The fill model reads bid-side book deltas. A level that shrinks might have been
TRADED (we move up the queue, and past it we get filled) or CANCELLED (we move
up the queue and get nothing). From the book alone the two are identical, and
the engine resolves the ambiguity optimistically -- it credits the whole
remainder whenever a level empties out. Measured on the first recorded windows,
100% of simulated fills came from that branch, so the entire fill rate rested on
the one assumption that cannot be checked from the book.

The tape checks it. It reports each participant's own side, so the AGGRESSOR is
not recoverable (this is why fills.py stopped keying off `side`) -- but we do not
need the aggressor. We need to know whether any volume traded at our price while
our level was draining. That is directly observable.

Backfill rather than live capture: /trades is queryable per market after the
fact, so every window already in books.db gets a tape too.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger("trades")
API = "https://data-api.polymarket.com/trades"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,     -- 'asset' in the API payload
    ts REAL NOT NULL,           -- unix seconds (API gives 1s resolution)
    price REAL NOT NULL,
    size REAL NOT NULL,
    tx TEXT,
    PRIMARY KEY (condition_id, token_id, ts, price, size, tx)
);
CREATE INDEX IF NOT EXISTS idx_t_cond ON trades(condition_id, token_id, ts);

CREATE TABLE IF NOT EXISTS trade_fetch (
    condition_id TEXT PRIMARY KEY,
    fetched_ts REAL,
    n INTEGER
);
"""


def fetch_market(cond: str, page: int = 500, max_pages: int = 40) -> list[tuple]:
    out, offset = [], 0
    for _ in range(max_pages):
        r = requests.get(API, params={"market": cond, "limit": page,
                                      "offset": offset}, timeout=20)
        r.raise_for_status()
        d = r.json() or []
        for t in d:
            out.append((
                cond, str(t.get("asset")), float(t.get("timestamp") or 0),
                float(t.get("price") or 0), float(t.get("size") or 0),
                str(t.get("transactionHash") or t.get("proxyWallet") or ""),
            ))
        if len(d) < page:
            break
        offset += page
    return out


def run(books: Path, refetch: bool) -> None:
    c = sqlite3.connect(str(books))
    c.executescript(SCHEMA)
    conds = [r[0] for r in c.execute(
        "SELECT condition_id FROM windows ORDER BY start_ts").fetchall()]
    done = set() if refetch else {
        r[0] for r in c.execute("SELECT condition_id FROM trade_fetch")}
    todo = [x for x in conds if x not in done]
    log.info("%d windows, %d already fetched, %d to do",
             len(conds), len(done), len(todo))

    for i, cond in enumerate(todo, 1):
        try:
            rows = fetch_market(cond)
        except Exception as e:
            log.warning("fetch failed %s: %s", cond[:12], e)
            continue
        c.executemany(
            "INSERT OR IGNORE INTO trades (condition_id, token_id, ts, price, "
            "size, tx) VALUES (?,?,?,?,?,?)", rows)
        c.execute("INSERT OR REPLACE INTO trade_fetch VALUES (?,?,?)",
                  (cond, time.time(), len(rows)))
        c.commit()
        log.info("[%d/%d] %s  %d trades", i, len(todo), cond[:12], len(rows))
        time.sleep(0.25)

    n, = c.execute("SELECT count(*) FROM trades").fetchone()
    log.info("tape holds %d trades over %d windows", n, len(conds))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--books", default=str(ROOT / "books.db"))
    p.add_argument("--refetch", action="store_true",
                   help="re-pull windows already fetched (they may have grown)")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")
    run(Path(a.books), a.refetch)


if __name__ == "__main__":
    main()
