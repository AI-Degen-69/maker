"""SQLite store for the maker sim. Entirely separate DB from the taker bot.

Schema is maker-shaped: we record every QUOTE we post (not just fills), because
for a maker the quotes that DIDN'T fill are half the information -- fill rate,
queue depth and time-to-fill are the metrics that decide whether the strategy is
viable at all.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from strategy.config import load as load_cfg

_cfg = load_cfg()
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    market_slug TEXT,
    condition_id TEXT,
    token_id TEXT,
    side TEXT,                 -- UP | DOWN
    price REAL,
    size REAL,                 -- shares posted
    queue_ahead REAL,          -- shares resting ahead of us when we joined
    mid REAL,                  -- market mid at post time
    edge_vs_mid REAL,          -- mid - price (our theoretical spread capture)
    t_remaining REAL,
    filled REAL DEFAULT 0,     -- shares eventually filled
    fill_ts REAL,              -- when the last fill landed
    cancelled INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    quote_id INTEGER,
    market_slug TEXT,
    condition_id TEXT,
    token_id TEXT,
    side TEXT,
    price REAL,
    size REAL,
    mid_at_post REAL,
    edge_vs_mid REAL,          -- captured spread per share
    queue_waited REAL,
    seconds_to_fill REAL,
    crossed INTEGER DEFAULT 0,
    -- How the fill model decided this filled: 'tape' (volume confirmed on the
    -- trade tape at our price), 'queue' (book-only, level shrank past us),
    -- 'sweep' (book-only, level emptied -- indistinguishable from a mass
    -- cancel) or 'cross' (we took liquidity). A fill rate carried by 'sweep'
    -- is not the same claim as one carried by 'tape', so the dashboard has to
    -- show which it is rather than presenting a single undifferentiated number.
    reason TEXT DEFAULT 'queue'
);

CREATE TABLE IF NOT EXISTS resolutions (
    condition_id TEXT PRIMARY KEY,
    winning_token TEXT,
    resolved_ts REAL
);

-- Why we quoted (or didn't) each cycle. Same idea as the taker's decision log:
-- the reasons we DIDN'T act are what you tune the strategy on. Consecutive
-- identical decisions collapse into one row with a count.
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    market_slug TEXT,
    condition_id TEXT,
    action TEXT,               -- QUOTE | SKIP_*
    side TEXT,
    price REAL,
    mid REAL,
    edge_vs_mid REAL,
    t_remaining REAL,
    balance REAL,
    pair_cost REAL,
    reason TEXT,
    count INTEGER DEFAULT 1
);

-- Single-row snapshot of what the bot is looking at right now, so the
-- dashboard (a separate process) can render the live market without doing its
-- own market/book polling.
CREATE TABLE IF NOT EXISTS live_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ts REAL,
    payload TEXT
);

CREATE INDEX IF NOT EXISTS idx_q_ts ON quotes(ts);
CREATE INDEX IF NOT EXISTS idx_f_ts ON fills(ts);
CREATE INDEX IF NOT EXISTS idx_f_cond ON fills(condition_id);
CREATE INDEX IF NOT EXISTS idx_d_ts ON decisions(ts);

-- Decisive experiment census: for each DISTINCT live market we poll, record
-- whether a fillable sub-$1.00 hedged pair existed at ask-1tick. This is the
-- single number that decides saveable-vs-dead -- the run is contaminated by
-- the pair-cost bug until this is measured on clean data.
CREATE TABLE IF NOT EXISTS hedge_census (
    condition_id TEXT PRIMARY KEY,
    market_slug TEXT,
    up_ask REAL,
    down_ask REAL,
    pair_cost_at_touch REAL,        -- up_ask + down_ask - 0.02
    fillable_sub_one REAL,           -- 1 if pair < max_pair_cost else 0
    observed_ts REAL
);

-- Liquidity-reward accrual, sampled every quoting cycle. The reward is paid on
-- RESTING size, so the product is score-share over time, not fills -- and the
-- previous 60-market run measured fills only, which is why it read as flat
-- while the actual payoff went unrecorded. our_share is what the pool pays us:
--   payout ~= our_share * 0.20 * taker_fees_in_this_market
CREATE TABLE IF NOT EXISTS reward_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    market_slug TEXT,
    condition_id TEXT,
    our_score REAL,            -- sum over our resting orders of ((v-s)/v)^2*size
    market_score REAL,         -- same, over every qualifying level in the book
    our_share REAL,            -- our_score / market_score
    offset_c REAL,             -- how far under mid we quoted, in cents
    n_sides INTEGER            -- 2 = two-sided (scores full rate), 1 = halved
);
CREATE INDEX IF NOT EXISTS idx_rs_ts ON reward_samples(ts);

-- One row per fill, columns filled in as each horizon matures. This is the
-- only measurement of what being filled COSTS: settlement P&L cannot answer it
-- before 2027, and rent says nothing about it at all.
CREATE TABLE IF NOT EXISTS markouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    condition_id TEXT,
    market_slug TEXT,
    side TEXT,
    fill_price REAL,
    size REAL,
    ref_mid REAL,              -- mid at fill time, our own resting size excluded
    -- 'venue_clean' | 'contaminated'. A live run that cannot subtract our own
    -- size must write 'contaminated', so the aggregate drops the row instead
    -- of quietly reporting our own footprint back to us as edge.
    ref_mid_source TEXT,
    mid_h0 REAL, mid_h1 REAL, mid_h2 REAL,
    done INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mk_done ON markouts(done, ts);

-- One row per profit-taking close. Kept separate from `fills` because a close
-- is the only row in this database that books REALIZED money -- everything
-- else is an estimate or an open position. Blending the two is how a
-- projection turns into a reported profit.
CREATE TABLE IF NOT EXISTS closes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    condition_id TEXT,
    market_slug TEXT,
    shares REAL,               -- pairs closed
    -- Size-weighted AVERAGE price actually achieved selling this leg, not the
    -- top-of-book tick: a close can walk past the best bid into worse levels,
    -- and `proceeds` below is computed from that same walk. Logging the top
    -- tick here instead would silently contradict `proceeds` on any close
    -- that consumed more than the best level -- one row in a table whose
    -- comment claims it books REALIZED money must not disagree with itself.
    up_price REAL,
    dn_price REAL,
    cost_basis REAL,
    proceeds REAL,
    fee REAL,
    realized_pnl REAL,
    -- What holding these shares to settlement would have netted (1.00 minus
    -- cost, times shares) minus what closing actually netted. A close almost
    -- always forgoes some of this: two bids essentially always sum to under
    -- $1.00. The trade is sound (capital freed ~1.5 years early earns daily
    -- rent that dwarfs this), but that claim is only checkable if the cost
    -- side is on the record too -- see strategy/profit_take.py.
    forgone_vs_settlement REAL,
    -- The combined cost_basis above cannot be split back across the two legs
    -- after the fact: a close removes cost at each leg's OWN average price,
    -- and only by coincidence is that the same proportion as the share counts.
    -- Recording each leg's removed cost is what lets a restart rebuild the
    -- exact inventory the live process held.
    up_cost_removed REAL,
    dn_cost_removed REAL
);
CREATE INDEX IF NOT EXISTS idx_cl_ts ON closes(ts);

-- The quality gate's verdict per market, so a restart cannot forget it.
-- Everything else the fleet holds in memory is either re-derivable from the
-- ledger (inventory, from `fills` + `closes`) or genuinely stale on restart
-- (open orders -- the venue would not have them either). The gate is neither:
-- it is a JUDGEMENT built from `markout_min_sample` fills of evidence, and
-- rebuilding it costs another sample of fills in a market already known to be
-- toxic. A process restart is not new information about the market.
CREATE TABLE IF NOT EXISTS market_gate (
    condition_id TEXT PRIMARY KEY,
    gate_state TEXT,           -- NORMAL | WIDENED | EXITED
    updated_ts REAL
);
"""


# Columns added after the first DBs were created. Declared once here rather
# than repaired inside each writer -- log_fill used to carry a duplicated
# INSERT in an except branch to add `crossed`, which meant the migration only
# ran if a write happened to fail first.
_MIGRATIONS = {
    "fills": {"crossed": "INTEGER DEFAULT 0", "reason": "TEXT DEFAULT 'queue'"},
    "closes": {"up_cost_removed": "REAL", "dn_cost_removed": "REAL",
               "forgone_vs_settlement": "REAL"},
}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_cfg.db_path()))
    c.executescript(SCHEMA)
    for table, cols in _MIGRATIONS.items():
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols.items():
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    return c


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    with _lock:
        c = _conn()
        try:
            yield c
            c.commit()
        finally:
            c.close()


def log_quote(**kw) -> int:
    with db() as c:
        cur = c.execute(
            "INSERT INTO quotes (ts, market_slug, condition_id, token_id, side, "
            "price, size, queue_ahead, mid, edge_vs_mid, t_remaining) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), kw["market_slug"], kw["condition_id"], kw["token_id"],
             kw["side"], kw["price"], kw["size"], kw["queue_ahead"], kw["mid"],
             kw["edge_vs_mid"], kw["t_remaining"]),
        )
        return cur.lastrowid


def log_fill(**kw) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO fills (ts, quote_id, market_slug, condition_id, token_id, "
            "side, price, size, mid_at_post, edge_vs_mid, queue_waited, "
            "seconds_to_fill, crossed, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), kw.get("quote_id"), kw["market_slug"], kw["condition_id"],
             kw["token_id"], kw["side"], kw["price"], kw["size"],
             kw.get("mid_at_post"), kw.get("edge_vs_mid"), kw.get("queue_waited"),
             kw.get("seconds_to_fill"), int(bool(kw.get("crossed"))),
             kw.get("reason") or "queue"))
        c.execute("UPDATE quotes SET filled = filled + ?, fill_ts = ? WHERE id = ?",
                  (kw["size"], time.time(), kw.get("quote_id")))


def log_markout_open(**kw) -> int:
    """Open a markout row at fill time. The horizons are filled in later."""
    with db() as c:
        cur = c.execute(
            "INSERT INTO markouts (ts, condition_id, market_slug, side, "
            "fill_price, size, ref_mid, ref_mid_source) VALUES (?,?,?,?,?,?,?,?)",
            (kw["ts"], kw["condition_id"], kw["market_slug"], kw["side"],
             kw["fill_price"], kw["size"], kw["ref_mid"],
             kw.get("ref_mid_source") or "venue_clean"))
        return cur.lastrowid


def log_close(**kw) -> None:
    """A profit-taking close. Realized money -- never blended into estimates."""
    with db() as c:
        c.execute(
            "INSERT INTO closes (ts, condition_id, market_slug, shares, "
            "up_price, dn_price, cost_basis, proceeds, fee, realized_pnl, "
            "forgone_vs_settlement, up_cost_removed, dn_cost_removed) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), kw["condition_id"], kw["market_slug"], kw["shares"],
             kw["up_price"], kw["dn_price"], kw["cost_basis"], kw["proceeds"],
             kw["fee"], kw["realized_pnl"], kw.get("forgone_vs_settlement"),
             kw["up_cost_removed"], kw["dn_cost_removed"]))


def save_gate_state(condition_id: str, state: str) -> None:
    """Persist a market's gate verdict.

    Called on the transition INTO EXITED, which is the only transition whose
    loss is asymmetric. Forgetting NORMAL or WIDENED costs at most a slightly
    wrong offset for one sample; forgetting EXITED puts us back in a market
    that has already been measured taking money off us, and the only way back
    out is to pay for the evidence a second time.

    Upsert, not insert: a market can re-enter this table on a later run.
    """
    with db() as c:
        c.execute("INSERT OR REPLACE INTO market_gate "
                  "(condition_id, gate_state, updated_ts) VALUES (?,?,?)",
                  (condition_id, state, time.time()))


def get_gate_state(condition_id: str) -> Optional[str]:
    """The last persisted verdict, or None if this market has never had one."""
    with db() as c:
        r = c.execute("SELECT gate_state FROM market_gate WHERE condition_id=?",
                      (condition_id,)).fetchone()
    return r[0] if r else None


def pending_markouts(now: float, horizons) -> list[dict]:
    """Rows with at least one horizon matured and not yet recorded.

    Returns the FIRST unrecorded matured horizon per row (as `_due`) rather
    than all of them, so a row that has been waiting a long time still gets
    its earlier horizons written in order instead of skipping to the last.
    """
    out = []
    with db() as c:
        cur = c.execute("SELECT * FROM markouts WHERE done = 0")
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            row = dict(zip(cols, r))
            for i, h in enumerate(horizons):
                if row.get(f"mid_h{i}") is None and now - row["ts"] >= h:
                    row["_due"] = i
                    out.append(row)
                    break
    return out


def close_markout(rowid: int, horizon_idx: int, mid_later: float,
                  last: bool = False) -> None:
    with db() as c:
        c.execute(f"UPDATE markouts SET mid_h{horizon_idx} = ?, done = ? "
                  "WHERE id = ?", (mid_later, 1 if last else 0, rowid))


def markout_rows() -> list[dict]:
    with db() as c:
        cur = c.execute("SELECT * FROM markouts")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def mark_cancelled(quote_ids: list[int]) -> None:
    if not quote_ids:
        return
    with db() as c:
        c.executemany("UPDATE quotes SET cancelled=1 WHERE id=? AND filled=0",
                      [(q,) for q in quote_ids])


def record_resolution(condition_id: str, winning_token: str) -> None:
    with db() as c:
        c.execute("INSERT OR REPLACE INTO resolutions VALUES (?,?,?)",
                  (condition_id, winning_token, time.time()))


def unresolved() -> list[tuple[str, str]]:
    with db() as c:
        return [(r[0], r[1]) for r in c.execute(
            "SELECT DISTINCT f.condition_id, f.market_slug FROM fills f "
            "LEFT JOIN resolutions r ON r.condition_id=f.condition_id "
            "WHERE r.condition_id IS NULL"
        ).fetchall()]


def open_markets() -> int:
    return len(unresolved())


def record_hedge_census(condition_id: str, market_slug: str, up_ask: float,
                         down_ask: float, pair_cost: float, fillable: bool,
                         ts: float) -> None:
    """One row per distinct market: was a fillable sub-$1.00 pair available?"""
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO hedge_census VALUES (?,?,?,?,?,?,?)",
            (condition_id, market_slug, up_ask, down_ask, pair_cost,
             1 if fillable else 0, ts),
        )


def log_reward_sample(ts: float, market_slug: str, condition_id: str,
                       our_score: float, market_score: float, offset_c: float,
                       n_sides: int) -> None:
    """One row per quoting cycle: what fraction of the reward score we hold.

    Written even when our_score is 0 -- a cycle spent out of the book is the
    thing being measured, not an absence of data. The old run's 69% skip rate
    was invisible for exactly this reason.
    """
    # our / (ours + theirs), NOT our / theirs. `market_score` is measured from
    # the public book, which in simulation does NOT contain our orders -- we
    # post nothing real. Live, our size would sit in that book and count toward
    # the total, so the pool splits over ours PLUS everyone else's. Dividing by
    # theirs alone overstates the share, and overstates it most in exactly the
    # thin markets we deliberately picked.
    denom = our_score + market_score
    share = (our_score / denom) if denom > 0 else 0.0
    with db() as c:
        c.execute(
            "INSERT INTO reward_samples "
            "(ts,market_slug,condition_id,our_score,market_score,our_share,"
            " offset_c,n_sides) VALUES (?,?,?,?,?,?,?,?)",
            (ts, market_slug, condition_id, our_score, market_score, share,
             offset_c, n_sides),
        )


# --- decision log (run-collapsed, same approach as the taker bot) -----------
#
# The dedup key deliberately EXCLUDES `reason`. Reason strings embed live values
# ("t_remaining 4s < 15s", "rest 1 tick under ask 0.53") that change on nearly
# every cycle, so keying on them collapses almost nothing -- measured 2.0x here
# versus ~15x on the taker, 17,490 rows/day. Same mistake was made and fixed on
# the taker side; keying on (market, action, side) is what actually works. The
# latest reason/price is kept as the row's value, and the `quotes` table still
# holds the exact per-quote record, so no detail is lost.
# 30s, not the taker's 10s: the maker re-decides every 2s (vs 0.25s), so a
# 10s window caps a run at only 5 evaluations and compression stalls ~2.8x.
# 30s allows ~15/row. The live-market panel gives real-time visibility, so a
# decision log that lags up to 30s costs nothing.
_RUN_MAX_SEC = 30.0
_run: dict = {"key": None, "row": None, "count": 0, "started": 0.0}


def log_decision(**kw) -> None:
    """Collapse consecutive identical decisions into one row with a count."""
    global _run
    now = time.time()
    key = (kw.get("condition_id"), kw.get("action"), kw.get("side"))
    row = (now, kw.get("market_slug"), kw.get("condition_id"), kw.get("action"),
           kw.get("side"), kw.get("price"), kw.get("mid"), kw.get("edge_vs_mid"),
           kw.get("t_remaining"), kw.get("balance"), kw.get("pair_cost"), kw.get("reason"))
    if _run["key"] == key and (now - _run["started"]) < _RUN_MAX_SEC:
        _run["count"] += 1
        _run["row"] = row          # keep the freshest values
        return
    flush_decision(force=True)
    _run = {"key": key, "row": row, "count": 1, "started": now}


def flush_decision(force: bool = False) -> None:
    """Write the open run. Without `force`, only once it exceeds _RUN_MAX_SEC,
    so a persistent state still reaches the DB without spawning a row per tick."""
    global _run
    if _run["key"] is None or _run["row"] is None:
        return
    if not force and (time.time() - _run["started"]) < _RUN_MAX_SEC:
        return
    with db() as c:
        c.execute(
            "INSERT INTO decisions (ts, market_slug, condition_id, action, side, "
            "price, mid, edge_vs_mid, t_remaining, balance, pair_cost, reason, count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            _run["row"] + (_run["count"],),
        )
    _run = {"key": None, "row": None, "count": 0, "started": 0.0}


def set_live_state(payload: dict) -> None:
    import json
    with db() as c:
        c.execute("INSERT OR REPLACE INTO live_state (id, ts, payload) VALUES (1,?,?)",
                  (time.time(), json.dumps(payload)))


def get_live_state() -> dict:
    import json
    with db() as c:
        r = c.execute("SELECT ts, payload FROM live_state WHERE id=1").fetchone()
    if not r:
        return {}
    try:
        d = json.loads(r[1])
        d["_age"] = time.time() - (r[0] or 0)
        return d
    except Exception:
        return {}
