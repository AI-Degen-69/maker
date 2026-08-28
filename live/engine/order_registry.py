"""live/engine/order_registry.py - Live order and fill registry backed by SQLite (run/live.db).

Stage 2 Architecture Constraints:
- Stored separately in run/live.db, never in run/fleet.db (simulator state).
- orders.id is a local uuid4 string written BEFORE submitting to the venue.
- orders.order_id is the venue order id, unique and nullable, attached after POST response.
- size_matched is strictly derived from SUM(fills.size), never stored as a mutable column in orders.
- PRAGMA foreign_keys=ON on every connection to enforce fills.order_uuid -> orders.id.
- PRAGMA journal_mode=WAL for non-blocking concurrent reads during poll writes.
- Timestamps are integer epoch milliseconds (UTC) for order/fill events; real seconds for store parity.
- Atomic fail-closed transaction boundaries.
- Every table carries a run_id for session tracing.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

LIVE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = LIVE_ROOT / "run" / "live.db"
BUSY_TIMEOUT_SEC = 5.0

# 30 seconds match window: covers HTTP roundtrip and CLOB ingestion skew
DEFAULT_ORPHAN_MATCH_WINDOW_MS: int = 30_000

# The deliberate re-read window on every trade query.
TRADE_OVERLAP_MS: int = 60_000

# Floor on how far back a trade query may reach.
MAX_TRADE_LOOKBACK_MS: int = 15 * 60 * 1000

# Sentinel for test doubles
_CREDS_UNCHECKED = object()

# Float epsilon for size comparisons
SIZE_EPS: float = 1e-9

# Every status a row may hold, enforced by a CHECK constraint in the schema.
ORDER_STATUSES = ("pending", "open", "partial", "filled", "cancelled", "unattributed")

RECONCILE_LOCK_STALE_MS: int = 300_000

_CURRENT_RUN_ID: Optional[str] = None


def _resolve_run_id() -> str:
    """Derive the session run_id so fleet/dash/exec processes share one ID.

    Priority:
    1. SH_RUN_ID env var (explicit override, set by start_bot)
    2. live/run/.current_run_id lock file, if it was written in the last 12h
    3. New UUID — generates and writes the lock file so other processes pick it up
    """
    env_id = os.environ.get("SH_RUN_ID")
    if env_id:
        return env_id

    lock_file = LIVE_ROOT / "run" / ".current_run_id"
    try:
        if lock_file.exists():
            mtime = lock_file.stat().st_mtime
            if time.time() - mtime < 43200:  # 12h
                content = lock_file.read_text().strip()
                if content:  # Validate non-empty before trusting
                    return content
    except Exception:
        pass

    new_id = f"run-{uuid.uuid4().hex[:12]}"
    try:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        # Atomic publish: write to temp, then os.replace. If another process wins,
        # retry-read its value instead of clobbering.
        import tempfile
        temp_fd, temp_path = tempfile.mkstemp(dir=lock_file.parent, prefix=".run_id_tmp_")
        try:
            os.write(temp_fd, new_id.encode())
            os.close(temp_fd)
            try:
                os.replace(temp_path, lock_file)
                # We won the race, return our ID
                return new_id
            except Exception:
                # Another process may have published first; re-read and use theirs
                if lock_file.exists():
                    content = lock_file.read_text().strip()
                    if content:
                        return content
                # Fall back to our ID if read failed
                return new_id
        finally:
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except Exception:
                pass
    except Exception:
        # If all file operations fail, return the generated ID anyway
        pass
    return new_id


_CURRENT_RUN_ID = _resolve_run_id()


def get_run_id() -> str:
    """Return the current process/session run_id."""
    global _CURRENT_RUN_ID
    return _CURRENT_RUN_ID


def set_run_id(run_id: str) -> None:
    """Set the session run_id."""
    global _CURRENT_RUN_ID
    _CURRENT_RUN_ID = run_id


class ReconcileInProgress(RuntimeError):
    """Raised when a reconcile pass is already in flight against this database."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    order_id TEXT UNIQUE,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    price REAL NOT NULL,
    original_size REAL NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'open', 'partial', 'filled', 'cancelled', 'unattributed')
    ),
    posted_ts INTEGER NOT NULL,
    last_polled_ts INTEGER NOT NULL,
    pair_id TEXT,
    max_pair_cost_at_post REAL,
    run_id TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    trade_id TEXT PRIMARY KEY,
    order_uuid TEXT NOT NULL,
    size REAL NOT NULL,
    price REAL NOT NULL,
    venue_ts INTEGER,
    recorded_ts INTEGER,
    run_id TEXT,
    FOREIGN KEY (order_uuid) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    market_slug TEXT,
    condition_id TEXT,
    token_id TEXT,
    side TEXT,
    price REAL,
    size REAL,
    queue_ahead REAL,
    mid REAL,
    edge_vs_mid REAL,
    t_remaining REAL,
    filled REAL DEFAULT 0,
    fill_ts REAL,
    cancelled INTEGER DEFAULT 0,
    order_id TEXT,
    local_id TEXT,
    latency_ms REAL,
    run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    market_slug TEXT,
    condition_id TEXT,
    kind TEXT NOT NULL,
    reason TEXT,
    reason_code TEXT DEFAULT 'OTHER',
    side TEXT,
    price REAL,
    size REAL,
    run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS markouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    condition_id TEXT,
    market_slug TEXT,
    side TEXT,
    token_id TEXT,
    fill_price REAL,
    size REAL,
    ref_mid REAL,
    ref_mid_source TEXT DEFAULT 'contaminated',
    mid_h0 REAL,
    mid_h1 REAL,
    mid_h2 REAL,
    mid_h3 REAL,
    done INTEGER DEFAULT 0,
    run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS closes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    condition_id TEXT,
    market_slug TEXT,
    method TEXT DEFAULT 'sell',
    gas REAL,
    shares REAL,
    up_price REAL,
    dn_price REAL,
    cost_basis REAL,
    proceeds REAL,
    fee REAL,
    realized_pnl REAL,
    forgone_vs_settlement REAL,
    up_cost_removed REAL,
    dn_cost_removed REAL,
    tx_hash TEXT,
    run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS float_marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    unrealized_usd REAL NOT NULL,
    committed_open_usd REAL NOT NULL,
    naked_usd REAL NOT NULL,
    run_id TEXT NOT NULL
);

-- What the venue says the account is worth, recorded by the account sweep.
-- Every column is nullable on purpose: a sweep that reached one endpoint and
-- not another records what it got and NULL for the rest. A 0.0 here would be a
-- claim the venue never made.
CREATE TABLE IF NOT EXISTS account_marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    collateral_usd REAL,
    positions_value_usd REAL,
    account_value_usd REAL,
    pnl_usd REAL,
    pnl_pct REAL,
    pnl_closed_usd REAL,
    pnl_series_usd REAL,
    unrealized_usd REAL,
    committed_usd REAL,
    open_positions_count INTEGER,
    closed_positions_count INTEGER,
    source TEXT NOT NULL,
    run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hedge_census (
    condition_id TEXT PRIMARY KEY,
    market_slug TEXT,
    up_ask REAL,
    down_ask REAL,
    pair_cost_at_touch REAL,
    fillable_sub_one REAL,
    observed_ts REAL,
    run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resolutions (
    condition_id TEXT PRIMARY KEY,
    winning_token TEXT,
    resolved_ts REAL,
    run_id TEXT
);

CREATE TABLE IF NOT EXISTS venue_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    condition_id TEXT,
    side TEXT,
    price REAL,
    size REAL,
    error_code TEXT,
    raw_error_msg TEXT,
    run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS divergence_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    condition_id TEXT,
    pair_id TEXT,
    registry_diff REAL,
    venue_diff REAL,
    chain_diff REAL,
    divergence_msg TEXT,
    run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconcile_lock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    holder TEXT NOT NULL,
    acquired_ts INTEGER NOT NULL
);

-- Per-cycle fleet decisions, one row per market visit, pruned to the last 200.
-- Written by engine.cycle_stream (decide event inserts, submit event updates
-- the submitted/cancelled counts). Telemetry only; the ring file is for
-- streaming, this table is for SQL queries.
CREATE TABLE IF NOT EXISTS cycle_intent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    cycle INTEGER NOT NULL,
    market_slug TEXT NOT NULL,
    condition_id TEXT,
    intent_count INTEGER NOT NULL DEFAULT 0,
    submitted INTEGER NOT NULL DEFAULT 0,
    cancelled INTEGER NOT NULL DEFAULT 0,
    top_skip_reason TEXT,
    top_pass_reason TEXT,
    latency_ms REAL,
    run_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders(order_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_pair_id ON orders(pair_id);
CREATE INDEX IF NOT EXISTS idx_fills_order_uuid ON fills(order_uuid);
CREATE INDEX IF NOT EXISTS idx_quotes_ts ON quotes(ts);
CREATE INDEX IF NOT EXISTS idx_market_events_cond_ts ON market_events(condition_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_markouts_done ON markouts(done, ts);
CREATE INDEX IF NOT EXISTS idx_closes_ts ON closes(ts);
CREATE INDEX IF NOT EXISTS idx_float_marks_ts ON float_marks(ts);
CREATE INDEX IF NOT EXISTS idx_account_marks_ts ON account_marks(ts);

CREATE VIEW IF NOT EXISTS order_summary AS
SELECT
    o.id,
    o.order_id,
    o.condition_id,
    o.token_id,
    o.side,
    o.price,
    o.original_size,
    o.status,
    o.posted_ts,
    o.last_polled_ts,
    o.pair_id,
    o.max_pair_cost_at_post,
    o.run_id,
    COALESCE(SUM(f.size), 0.0) AS size_matched
FROM orders o
LEFT JOIN fills f ON f.order_uuid = o.id
GROUP BY o.id;
"""

_schema_ready: dict[str, tuple[int, int, int]] = {}
_lock = threading.RLock()


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Safely apply schema migrations to existing database tables."""
    # Check columns in orders
    cur = conn.execute("PRAGMA table_info(orders)")
    cols = {row["name"] for row in cur.fetchall()}
    if "run_id" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN run_id TEXT")

    # Check columns in fills
    cur = conn.execute("PRAGMA table_info(fills)")
    cols = {row["name"] for row in cur.fetchall()}
    if "recorded_ts" not in cols:
        conn.execute("ALTER TABLE fills ADD COLUMN recorded_ts INTEGER")
    if "run_id" not in cols:
        conn.execute("ALTER TABLE fills ADD COLUMN run_id TEXT")

    # Check columns in markouts
    cur = conn.execute("PRAGMA table_info(markouts)")
    cols = {row["name"] for row in cur.fetchall()}
    if cols:
        if "mid_h3" not in cols:
            conn.execute("ALTER TABLE markouts ADD COLUMN mid_h3 REAL")
        if "run_id" not in cols:
            conn.execute("ALTER TABLE markouts ADD COLUMN run_id TEXT")
        if "token_id" not in cols:
            conn.execute("ALTER TABLE markouts ADD COLUMN token_id TEXT")

    # Check columns in closes
    cur = conn.execute("PRAGMA table_info(closes)")
    cols = {row["name"] for row in cur.fetchall()}
    if cols:
        if "tx_hash" not in cols:
            conn.execute("ALTER TABLE closes ADD COLUMN tx_hash TEXT")
        if "run_id" not in cols:
            conn.execute("ALTER TABLE closes ADD COLUMN run_id TEXT")

    # Check columns in account_marks
    cur = conn.execute("PRAGMA table_info(account_marks)")
    cols = {row["name"] for row in cur.fetchall()}
    if cols and "closed_positions_count" not in cols:
        conn.execute("ALTER TABLE account_marks ADD COLUMN closed_positions_count INTEGER")

    # Add UNIQUE constraint on closes (condition_id, tx_hash) to prevent duplicate venue_sync entries.
    # Check if index already exists by querying sqlite_master.
    idx_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_closes_cid_tx'"
    ).fetchone()
    if not idx_check:
        conn.execute(
            "CREATE UNIQUE INDEX idx_closes_cid_tx ON closes(condition_id, tx_hash)"
        )

    conn.commit()


def get_connection(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Return a configured sqlite3.Connection with WAL and foreign_keys=ON."""
    path = str(Path(db_path))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SEC)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        st = os.stat(path)
        file_id = (st.st_ino, st.st_ctime_ns, st.st_mtime_ns)
    except OSError:
        file_id = (0, 0, 0)

    if _schema_ready.get(path) != file_id:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        conn.executescript(SCHEMA)
        _apply_migrations(conn)
        _schema_ready[path] = file_id

    return conn


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Initialize schema for the database file."""
    with _lock:
        conn = get_connection(db_path)
        conn.close()


@dataclass(frozen=True)
class OrderRecord:
    id: str
    condition_id: str
    token_id: str
    side: str
    price: float
    original_size: float
    status: str
    posted_ts: int
    last_polled_ts: int
    order_id: Optional[str] = None
    pair_id: Optional[str] = None
    max_pair_cost_at_post: Optional[float] = None
    run_id: Optional[str] = None


@dataclass(frozen=True)
class FillRecord:
    trade_id: str
    order_uuid: str
    size: float
    price: float
    venue_ts: Optional[int] = None
    recorded_ts: Optional[int] = None
    run_id: Optional[str] = None


@dataclass(frozen=True)
class QuoteRecord:
    ts: float
    condition_id: str
    token_id: str
    side: str
    price: float
    size: float
    market_slug: Optional[str] = None
    queue_ahead: Optional[float] = None
    mid: Optional[float] = None
    edge_vs_mid: Optional[float] = None
    t_remaining: Optional[float] = None
    filled: float = 0.0
    fill_ts: Optional[float] = None
    cancelled: int = 0
    order_id: Optional[str] = None
    local_id: Optional[str] = None
    latency_ms: Optional[float] = None
    run_id: Optional[str] = None


@dataclass(frozen=True)
class MarketEventRecord:
    ts: float
    condition_id: str
    kind: str
    market_slug: Optional[str] = None
    reason: Optional[str] = None
    reason_code: str = "OTHER"
    side: Optional[str] = None
    price: Optional[float] = None
    size: Optional[float] = None
    run_id: Optional[str] = None


@dataclass(frozen=True)
class MarkoutRecord:
    ts: float
    condition_id: str
    side: str
    fill_price: float
    size: float
    # The venue token this fill was on. `side` is the order book's BUY/SELL, which
    # cannot say whether the fill was on the UP or the DOWN leg -- and the markout
    # sampler needs exactly that to pick a reference mid.
    token_id: Optional[str] = None
    market_slug: Optional[str] = None
    ref_mid: Optional[float] = None
    ref_mid_source: str = "contaminated"
    mid_h0: Optional[float] = None
    mid_h1: Optional[float] = None
    mid_h2: Optional[float] = None
    mid_h3: Optional[float] = None
    done: int = 0
    run_id: Optional[str] = None


@dataclass(frozen=True)
class CloseRecord:
    ts: float
    condition_id: str
    method: str = "sell"
    market_slug: Optional[str] = None
    gas: Optional[float] = None
    shares: Optional[float] = None
    up_price: Optional[float] = None
    dn_price: Optional[float] = None
    cost_basis: Optional[float] = None
    proceeds: Optional[float] = None
    fee: Optional[float] = None
    realized_pnl: Optional[float] = None
    forgone_vs_settlement: Optional[float] = None
    up_cost_removed: Optional[float] = None
    dn_cost_removed: Optional[float] = None
    tx_hash: Optional[str] = None
    run_id: Optional[str] = None


@dataclass(frozen=True)
class FloatMarkRecord:
    ts: float
    unrealized_usd: float
    committed_open_usd: float
    naked_usd: float
    run_id: Optional[str] = None


@dataclass(frozen=True)
class HedgeCensusRecord:
    condition_id: str
    up_ask: Optional[float]
    down_ask: Optional[float]
    pair_cost_at_touch: Optional[float]
    fillable_sub_one: Optional[float]
    observed_ts: float
    market_slug: Optional[str] = None
    run_id: Optional[str] = None


@dataclass(frozen=True)
class ResolutionRecord:
    condition_id: str
    winning_token: str
    resolved_ts: float
    run_id: Optional[str] = None


@dataclass(frozen=True)
class VenueErrorRecord:
    ts: float
    condition_id: str
    side: Optional[str]
    price: Optional[float]
    size: Optional[float]
    error_code: Optional[str]
    raw_error_msg: str
    run_id: Optional[str] = None


@dataclass(frozen=True)
class DivergenceEventRecord:
    ts: float
    condition_id: str
    pair_id: Optional[str]
    registry_diff: float
    venue_diff: float
    chain_diff: float
    divergence_msg: str
    run_id: Optional[str] = None


class OrderRegistry:
    """Thread-safe SQLite-backed registry for live orders, fills, and operational telemetry."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        init_db(self.db_path)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with _lock:
            conn = get_connection(self.db_path)
            try:
                yield conn
            except BaseException:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()

    def _write_reconcile_lock(self, holder: str, acquired_ts: int) -> None:
        """Force the lock row. Test and recovery seam."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR REPLACE INTO reconcile_lock (id, holder, acquired_ts) "
                "VALUES (1, ?, ?)",
                (holder, int(acquired_ts)),
            )
            conn.commit()

    @contextmanager
    def reconcile_lock(self, now_ms: int) -> Iterator[str]:
        """Hold the single reconcile slot for this database, or refuse."""
        holder = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT holder, acquired_ts FROM reconcile_lock WHERE id = 1"
            ).fetchone()
            if row is not None:
                age_ms = int(now_ms) - int(row["acquired_ts"])
                if age_ms < RECONCILE_LOCK_STALE_MS:
                    conn.rollback()
                    raise ReconcileInProgress(
                        f"A reconcile pass is already in flight against "
                        f"{self.db_path} (holder={row['holder']}, held for "
                        f"{age_ms} ms). Refusing rather than deciding "
                        f"transitions from reads the other pass is about to "
                        f"invalidate."
                    )
            conn.execute(
                "INSERT OR REPLACE INTO reconcile_lock (id, holder, acquired_ts) "
                "VALUES (1, ?, ?)",
                (holder, int(now_ms)),
            )
            conn.commit()

        try:
            yield holder
        finally:
            try:
                with self._conn() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "DELETE FROM reconcile_lock WHERE id = 1 AND holder = ?",
                        (holder,),
                    )
                    conn.commit()
            except sqlite3.Error:
                pass

    def create_order(self, order: OrderRecord) -> None:
        """Insert a new order row before sending to venue."""
        r_id = order.run_id or get_run_id()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO orders (
                    id, order_id, condition_id, token_id, side, price,
                    original_size, status, posted_ts, last_polled_ts,
                    pair_id, max_pair_cost_at_post, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.id,
                    order.order_id,
                    order.condition_id,
                    order.token_id,
                    order.side,
                    order.price,
                    order.original_size,
                    order.status,
                    order.posted_ts,
                    order.last_polled_ts,
                    order.pair_id,
                    order.max_pair_cost_at_post,
                    r_id,
                ),
            )
            conn.commit()

    def attach_venue_order_id(
        self,
        local_id: str,
        venue_order_id: str,
        status: str = "open",
        last_polled_ts: Optional[int] = None,
    ) -> None:
        """Attach venue order ID to a pending order upon receiving response."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if last_polled_ts is not None:
                cur = conn.execute(
                    """
                    UPDATE orders
                    SET order_id = ?, status = ?, last_polled_ts = ?
                    WHERE id = ?
                    """,
                    (venue_order_id, status, last_polled_ts, local_id),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE orders
                    SET order_id = ?, status = ?
                    WHERE id = ?
                    """,
                    (venue_order_id, status, local_id),
                )
            if cur.rowcount != 1:
                conn.rollback()
                raise KeyError(
                    f"attach_venue_order_id: no order row {local_id!r} for venue "
                    f"order {venue_order_id!r}; {cur.rowcount} rows matched"
                )
            conn.commit()

    def get_order(self, local_id: str) -> Optional[OrderRecord]:
        """Fetch order record by local uuid."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE id = ?",
                (local_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_order(row)

    def get_order_by_venue_id(self, venue_order_id: str) -> Optional[OrderRecord]:
        """Fetch order record by venue order_id."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE order_id = ?",
                (venue_order_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_order(row)

    def get_size_matched(self, order_uuid: str) -> float:
        """Derive size_matched from SUM(size) over fills."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(size), 0.0) AS total FROM fills WHERE order_uuid = ?",
                (order_uuid,),
            ).fetchone()
            return float(row["total"]) if row else 0.0

    def record_fill(self, fill: FillRecord) -> bool:
        """Record a fill idempotently. True if inserted, False if already present."""
        rec_ts = fill.recorded_ts if fill.recorded_ts is not None else int(time.time() * 1000)
        r_id = fill.run_id or get_run_id()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            already = conn.execute(
                "SELECT 1 FROM fills WHERE trade_id = ?",
                (fill.trade_id,),
            ).fetchone()
            if already is not None:
                conn.rollback()
                return False
            conn.execute(
                """
                INSERT INTO fills (trade_id, order_uuid, size, price, venue_ts, recorded_ts, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (fill.trade_id, fill.order_uuid, fill.size, fill.price, fill.venue_ts, rec_ts, r_id),
            )
            conn.commit()
            return True

    def update_order_status(
        self, local_id: str, status: str, last_polled_ts: int
    ) -> None:
        """Update order status and last_polled_ts."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE orders
                SET status = ?, last_polled_ts = ?
                WHERE id = ?
                """,
                (status, last_polled_ts, local_id),
            )
            if cur.rowcount != 1:
                conn.rollback()
                raise KeyError(
                    f"update_order_status: no order row {local_id!r} "
                    f"(status={status!r}); {cur.rowcount} rows matched"
                )
            conn.commit()

    def get_matched_notional(self, order_uuid: str) -> float:
        """SUM(size * price) over this order's fills."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(size * price), 0.0) AS notional "
                "FROM fills WHERE order_uuid = ?",
                (order_uuid,),
            ).fetchone()
        return float(row["notional"]) if row else 0.0

    def get_orders_by_pair(self, pair_id: str) -> list[OrderRecord]:
        """Every order carrying this pair_id, whatever its status."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE pair_id = ? ORDER BY posted_ts",
                (pair_id,),
            ).fetchall()
        return [self._row_to_order(r) for r in rows]

    def get_active_orders(self) -> list[OrderRecord]:
        """Return all orders in pending, open, or partial status."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE status IN ('pending', 'open', 'partial') ORDER BY posted_ts ASC"
            ).fetchall()
            return [self._row_to_order(r) for r in rows]

    # --- Telemetry Logging Methods -----------------------------------------

    def log_quote(self, quote: QuoteRecord) -> int:
        """Record a quote intent to quotes table."""
        r_id = quote.run_id or get_run_id()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                INSERT INTO quotes (
                    ts, market_slug, condition_id, token_id, side, price, size,
                    queue_ahead, mid, edge_vs_mid, t_remaining, filled, fill_ts,
                    cancelled, order_id, local_id, latency_ms, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    quote.ts,
                    quote.market_slug,
                    quote.condition_id,
                    quote.token_id,
                    quote.side,
                    quote.price,
                    quote.size,
                    quote.queue_ahead,
                    quote.mid,
                    quote.edge_vs_mid,
                    quote.t_remaining,
                    quote.filled,
                    quote.fill_ts,
                    quote.cancelled,
                    quote.order_id,
                    quote.local_id,
                    quote.latency_ms,
                    r_id,
                ),
            )
            conn.commit()
            return cur.lastrowid

    def update_quote_fill(
        self, local_id: str, filled_shares: float, fill_ts: float, cancelled: bool = False
    ) -> None:
        """Update quote record fill status."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE quotes
                SET filled = ?, fill_ts = ?, cancelled = ?
                WHERE local_id = ?
                """,
                (filled_shares, fill_ts, 1 if cancelled else 0, local_id),
            )
            conn.commit()

    def log_market_event(self, event: MarketEventRecord) -> None:
        """Record a market event (decision, skip reason, fill, state transition)."""
        r_id = event.run_id or get_run_id()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO market_events (
                    ts, market_slug, condition_id, kind, reason, reason_code,
                    side, price, size, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.ts,
                    event.market_slug,
                    event.condition_id,
                    event.kind,
                    event.reason,
                    event.reason_code,
                    event.side,
                    event.price,
                    event.size,
                    r_id,
                ),
            )
            conn.commit()

    def log_markout(self, markout: MarkoutRecord) -> int:
        """Record initial markout row on fill."""
        r_id = markout.run_id or get_run_id()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                INSERT INTO markouts (
                    ts, condition_id, market_slug, side, token_id, fill_price, size,
                    ref_mid, ref_mid_source, mid_h0, mid_h1, mid_h2, mid_h3,
                    done, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    markout.ts,
                    markout.condition_id,
                    markout.market_slug,
                    markout.side,
                    markout.token_id,
                    markout.fill_price,
                    markout.size,
                    markout.ref_mid,
                    markout.ref_mid_source,
                    markout.mid_h0,
                    markout.mid_h1,
                    markout.mid_h2,
                    markout.mid_h3,
                    markout.done,
                    r_id,
                ),
            )
            conn.commit()
            return cur.lastrowid

    def update_markout_horizon(
        self, markout_id: int, horizon_idx: int, mid: float, last: bool = False
    ) -> None:
        """Record mid at horizon_idx and set done flag if last horizon."""
        col = f"mid_h{horizon_idx}"
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if last:
                conn.execute(
                    f"UPDATE markouts SET {col} = ?, done = 1 WHERE id = ?",
                    (mid, markout_id),
                )
            else:
                conn.execute(
                    f"UPDATE markouts SET {col} = ? WHERE id = ?",
                    (mid, markout_id),
                )
            conn.commit()

    def get_pending_markouts(self, now_sec: float, horizons: tuple[float, ...]) -> list[dict]:
        """Fetch pending markouts due for sampling."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM markouts WHERE done = 0 ORDER BY ts ASC"
            ).fetchall()
            out = []
            for r in rows:
                row_dict = dict(r)
                age = now_sec - float(row_dict["ts"])
                for i, h in enumerate(horizons):
                    col = f"mid_h{i}"
                    if row_dict.get(col) is None and age >= h:
                        item = dict(row_dict)
                        item["_due"] = i
                        out.append(item)
                        break
            return out

    def log_close(self, close: CloseRecord) -> None:
        """Record an early exit or merge close."""
        r_id = close.run_id or get_run_id()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO closes (
                    ts, condition_id, market_slug, method, gas, shares,
                    up_price, dn_price, cost_basis, proceeds, fee, realized_pnl,
                    forgone_vs_settlement, up_cost_removed, dn_cost_removed,
                    tx_hash, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    close.ts,
                    close.condition_id,
                    close.market_slug,
                    close.method,
                    close.gas,
                    close.shares,
                    close.up_price,
                    close.dn_price,
                    close.cost_basis,
                    close.proceeds,
                    close.fee,
                    close.realized_pnl,
                    close.forgone_vs_settlement,
                    close.up_cost_removed,
                    close.dn_cost_removed,
                    close.tx_hash,
                    r_id,
                ),
            )
            conn.commit()

    def log_float_mark(
        self, unrealized_usd: float, committed_open_usd: float, naked_usd: float, ts: Optional[float] = None, run_id: Optional[str] = None
    ) -> None:
        """Record periodic portfolio marks."""
        now_ts = ts if ts is not None else time.time()
        r_id = run_id or get_run_id()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO float_marks (ts, unrealized_usd, committed_open_usd, naked_usd, run_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (now_ts, unrealized_usd, committed_open_usd, naked_usd, r_id),
            )
            conn.commit()

    def log_account_mark(
        self, mark: dict, ts: Optional[float] = None, run_id: Optional[str] = None
    ) -> None:
        """Record what the venue said the account was worth.

        `mark` is the dict returned by engine.account.compose_account_mark.
        Absent keys are stored as NULL rather than 0.0: the dashboard reads
        these rows and must be able to tell "not measured" from "measured flat".
        """
        now_ts = ts if ts is not None else time.time()
        r_id = run_id or get_run_id()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO account_marks (
                    ts, collateral_usd, positions_value_usd, account_value_usd,
                    pnl_usd, pnl_pct, pnl_closed_usd, pnl_series_usd,
                    unrealized_usd, committed_usd, open_positions_count,
                    closed_positions_count, source, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_ts,
                    mark.get("collateral_usd"),
                    mark.get("positions_value_usd"),
                    mark.get("account_value_usd"),
                    mark.get("pnl_usd"),
                    mark.get("pnl_pct"),
                    mark.get("pnl_closed_usd"),
                    mark.get("pnl_series_usd"),
                    mark.get("unrealized_usd"),
                    mark.get("committed_usd"),
                    mark.get("open_positions_count"),
                    mark.get("closed_positions_count"),
                    mark.get("source") or "venue",
                    r_id,
                ),
            )
            conn.commit()

    def log_hedge_census(self, census: HedgeCensusRecord) -> None:
        """Record market hedge census evaluation."""
        r_id = census.run_id or get_run_id()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR REPLACE INTO hedge_census (
                    condition_id, market_slug, up_ask, down_ask,
                    pair_cost_at_touch, fillable_sub_one, observed_ts, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    census.condition_id,
                    census.market_slug,
                    census.up_ask,
                    census.down_ask,
                    census.pair_cost_at_touch,
                    census.fillable_sub_one,
                    census.observed_ts,
                    r_id,
                ),
            )
            conn.commit()

    def log_resolution(self, res: ResolutionRecord) -> None:
        """Record market resolution."""
        r_id = res.run_id or get_run_id()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR REPLACE INTO resolutions (condition_id, winning_token, resolved_ts, run_id)
                VALUES (?, ?, ?, ?)
                """,
                (res.condition_id, res.winning_token, res.resolved_ts, r_id),
            )
            conn.commit()

    def log_venue_error(self, err: VenueErrorRecord) -> None:
        """Record venue error or rejection."""
        r_id = err.run_id or get_run_id()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO venue_errors (
                    ts, condition_id, side, price, size, error_code, raw_error_msg, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    err.ts,
                    err.condition_id,
                    err.side,
                    err.price,
                    err.size,
                    err.error_code,
                    err.raw_error_msg,
                    r_id,
                ),
            )
            conn.commit()

    def log_divergence_event(self, div: DivergenceEventRecord) -> None:
        """Record 3-way divergence incident."""
        r_id = div.run_id or get_run_id()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO divergence_events (
                    ts, condition_id, pair_id, registry_diff, venue_diff, chain_diff,
                    divergence_msg, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    div.ts,
                    div.condition_id,
                    div.pair_id,
                    div.registry_diff,
                    div.venue_diff,
                    div.chain_diff,
                    div.divergence_msg,
                    r_id,
                ),
            )
            conn.commit()

    # --- Query helpers for reporting ---------------------------------------

    def get_all_quotes(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM quotes ORDER BY ts ASC").fetchall()
            return [dict(r) for r in rows]

    def get_all_fills(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT f.trade_id, f.order_uuid, f.size, f.price, f.venue_ts, f.recorded_ts, f.run_id,
                       o.condition_id, o.token_id, o.side, o.pair_id, o.posted_ts
                FROM fills f
                JOIN orders o ON f.order_uuid = o.id
                ORDER BY f.recorded_ts ASC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_market_events(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM market_events ORDER BY ts ASC").fetchall()
            return [dict(r) for r in rows]

    def get_all_markouts(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM markouts ORDER BY ts ASC").fetchall()
            return [dict(r) for r in rows]

    def get_all_closes(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM closes ORDER BY ts ASC").fetchall()
            return [dict(r) for r in rows]

    def get_all_float_marks(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM float_marks ORDER BY ts ASC").fetchall()
            return [dict(r) for r in rows]

    def get_all_account_marks(self) -> list[dict]:
        """
        Retrieve all recorded account marks in ascending timestamp order.
        
        Returns:
            list[dict]: Account mark records ordered by timestamp.
        """
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM account_marks ORDER BY ts ASC").fetchall()
            return [dict(r) for r in rows]

    def get_all_orders(self) -> list[dict]:
        """Return all orders ordered by their posting timestamp.
        
        Returns:
        	list[dict]: Order records represented as dictionaries.
        """
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM orders ORDER BY posted_ts ASC").fetchall()
            return [dict(r) for r in rows]

    def get_all_hedge_census(self) -> list[dict]:
        """
        Return all recorded hedge census entries ordered by observation time.
        
        Returns:
        	list[dict]: Hedge census records in ascending order of observed timestamp.
        """
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM hedge_census ORDER BY observed_ts ASC").fetchall()
            return [dict(r) for r in rows]

    def get_all_resolutions(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM resolutions").fetchall()
            return [dict(r) for r in rows]

    def get_all_venue_errors(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM venue_errors ORDER BY ts ASC").fetchall()
            return [dict(r) for r in rows]

    def get_all_divergence_events(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM divergence_events ORDER BY ts ASC").fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def _row_to_order(row: sqlite3.Row) -> OrderRecord:
        return OrderRecord(
            id=row["id"],
            order_id=row["order_id"],
            condition_id=row["condition_id"],
            token_id=row["token_id"],
            side=row["side"],
            price=float(row["price"]),
            original_size=float(row["original_size"]),
            status=row["status"],
            posted_ts=int(row["posted_ts"]),
            last_polled_ts=int(row["last_polled_ts"]),
            pair_id=row["pair_id"],
            max_pair_cost_at_post=(
                float(row["max_pair_cost_at_post"])
                if row["max_pair_cost_at_post"] is not None
                else None
            ),
            run_id=row["run_id"] if "run_id" in row.keys() else None,
        )


@dataclass
class ReconcileSummary:
    polled_ts: int = 0
    open_orders_count: int = 0
    trades_polled: int = 0
    fills_recorded: int = 0
    duplicates_ignored: int = 0
    orders_filled: int = 0
    orders_partially_filled: int = 0
    orders_cancelled: int = 0
    orphans_adopted: int = 0
    unattributed_recorded: int = 0
    unmatched_trades: int = 0
    transitions: list[str] = field(default_factory=list)


def compute_backoff_delay(
    err_count: int, base_sec: float = 2.0, max_sec: float = 60.0
) -> float:
    """Compute exponential backoff delay capped at max_sec."""
    if err_count <= 0:
        return 0.0
    delay = base_sec * (2 ** (err_count - 1))
    return min(delay, max_sec)


def reconcile_orders(
    client,
    registry: OrderRegistry,
    maker_address: Optional[str] = None,
    current_ts_ms: Optional[int] = None,
    orphan_window_ms: int = DEFAULT_ORPHAN_MATCH_WINDOW_MS,
    lookback_ms: Optional[int] = None,
) -> ReconcileSummary:
    """Reconcile registry state against venue open orders and trades."""
    now_ms = current_ts_ms if current_ts_ms is not None else int(time.time() * 1000)
    with registry.reconcile_lock(now_ms):
        return _reconcile_pass(
            client,
            registry,
            maker_address=maker_address,
            now_ms=now_ms,
            orphan_window_ms=orphan_window_ms,
            lookback_ms=lookback_ms,
        )


def _reconcile_pass(
    client,
    registry: OrderRegistry,
    maker_address: Optional[str],
    now_ms: int,
    orphan_window_ms: int,
    lookback_ms: Optional[int] = None,
) -> ReconcileSummary:
    """One reconcile pass. Callers must already hold the reconcile lock."""
    summary = ReconcileSummary(polled_ts=now_ms)

    creds = getattr(client, "creds", _CREDS_UNCHECKED)
    if creds is None:
        raise PermissionError(
            "reconcile_orders: client has no L2 API credentials. Refusing to "
            "reconcile -- an unauthenticated client cannot distinguish 'no "
            "open orders' from 'never asked'."
        )

    # 1. Fetch open orders from venue.
    venue_open_orders_raw = client.get_open_orders()
    summary.open_orders_count = len(venue_open_orders_raw) if venue_open_orders_raw else 0
    venue_order_map: dict[str, dict] = {}
    if venue_open_orders_raw:
        for item in venue_open_orders_raw:
            v_id = str(item.get("id") or item.get("order_id") or "")
            if v_id:
                venue_order_map[v_id] = item

    # 2. Orphan adoption
    active_orders = registry.get_active_orders()
    pending_pool = [o for o in active_orders if o.status == "pending"]
    for v_id, item in venue_order_map.items():
        existing = registry.get_order_by_venue_id(v_id)
        if existing is None:
            v_token = str(item.get("asset_id") or item.get("token_id") or "")
            v_price = float(item.get("price", 0.0))
            v_size = float(item.get("size") or item.get("original_size") or 0.0)
            v_ts_raw = item.get("match_time") or item.get("timestamp") or item.get("created_at") or item.get("posted_ts")
            v_ts = int(v_ts_raw) if v_ts_raw is not None else None
            if v_ts is not None and v_ts < 10_000_000_000:
                v_ts *= 1000

            matched_pending = None
            for pending in pending_pool:
                token_match = (not v_token) or (pending.token_id == v_token)
                price_match = abs(pending.price - v_price) <= 1e-6
                size_match = abs(pending.original_size - v_size) <= SIZE_EPS
                ts_match = True
                if v_ts is not None:
                    ts_match = abs(v_ts - pending.posted_ts) <= orphan_window_ms
                if token_match and price_match and size_match and ts_match:
                    matched_pending = pending
                    break

            if matched_pending is not None:
                try:
                    registry.attach_venue_order_id(
                        matched_pending.id, v_id, status="open", last_polled_ts=now_ms
                    )
                    pending_pool.remove(matched_pending)
                    summary.orphans_adopted += 1
                    summary.transitions.append(f"ADOPT {matched_pending.id[:8]} -> {v_id}")
                except KeyError:
                    unattr_order = OrderRecord(
                        id=str(uuid.uuid4()),
                        order_id=v_id,
                        condition_id=str(item.get("market") or item.get("condition_id") or "unknown"),
                        token_id=v_token or "unknown",
                        side=str(item.get("side", "BUY")).upper(),
                        price=v_price,
                        original_size=v_size,
                        status="unattributed",
                        posted_ts=now_ms,
                        last_polled_ts=now_ms,
                        run_id=get_run_id(),
                    )
                    registry.create_order(unattr_order)
                    summary.unattributed_recorded += 1
                    summary.transitions.append(f"UNATTRIBUTED {v_id}")
            else:
                unattr_order = OrderRecord(
                    id=str(uuid.uuid4()),
                    order_id=v_id,
                    condition_id=str(item.get("market") or item.get("condition_id") or "unknown"),
                    token_id=v_token or "unknown",
                    side=str(item.get("side", "BUY")).upper(),
                    price=v_price,
                    original_size=v_size,
                    status="unattributed",
                    posted_ts=now_ms,
                    last_polled_ts=now_ms,
                    run_id=get_run_id(),
                )
                registry.create_order(unattr_order)
                summary.unattributed_recorded += 1
                summary.transitions.append(f"UNATTRIBUTED {v_id}")

    # 3. Poll trades with the 60s overlap.
    earliest_polled_ts = now_ms
    current_active = registry.get_active_orders()
    if current_active:
        earliest_polled_ts = min(o.last_polled_ts for o in current_active)
    else:
        with registry._conn() as conn:
            row = conn.execute("SELECT MIN(posted_ts) AS min_ts FROM orders").fetchone()
            if row and row["min_ts"]:
                earliest_polled_ts = int(row["min_ts"])

    max_lookback = lookback_ms if lookback_ms is not None else MAX_TRADE_LOOKBACK_MS
    earliest_polled_ts = max(earliest_polled_ts, now_ms - max_lookback)
    after_sec = max(0, int((earliest_polled_ts - TRADE_OVERLAP_MS) / 1000))

    from py_clob_client_v2.clob_types import TradeParams

    p = TradeParams(maker_address=maker_address, after=after_sec)
    trades_raw = client.get_trades(params=p)

    summary.trades_polled = len(trades_raw) if trades_raw else 0
    affected_order_ids: set[str] = set()
    if trades_raw:
        for t in trades_raw:
            t_id = str(t.get("id") or t.get("trade_id") or "")
            if not t_id:
                summary.unmatched_trades += 1
                summary.transitions.append(
                    f"UNMATCHED_TRADE <no id> size={t.get('size')} @ {t.get('price')}"
                )
                continue

            t_order_id = str(
                t.get("taker_order_id")
                or t.get("order_id")
                or t.get("maker_order_id")
                or ""
            )
            t_size = float(t.get("size", 0.0))
            t_price = float(t.get("price", 0.0))

            # Venue match time: match_time, timestamp, or created_at
            t_ts_raw = t.get("match_time") or t.get("timestamp") or t.get("venue_ts") or t.get("created_at")
            t_ts = None
            if t_ts_raw is not None:
                try:
                    t_ts = int(t_ts_raw)
                    if t_ts < 10_000_000_000:
                        t_ts *= 1000
                except (ValueError, TypeError):
                    t_ts = None

            # Check if this trade contains aggregate maker_orders
            maker_entries = t.get("maker_orders")
            matched_any_maker = False
            if isinstance(maker_entries, list) and maker_entries:
                for m_entry in maker_entries:
                    m_oid = str(m_entry.get("order_id") or "")
                    if not m_oid:
                        continue
                    m_order = registry.get_order_by_venue_id(m_oid) or registry.get_order(m_oid)
                    if m_order is not None:
                        matched_any_maker = True
                        affected_order_ids.add(m_order.id)
                        m_size = float(m_entry.get("matched_amount") or m_entry.get("size") or t_size)
                        m_price = float(m_entry.get("price") or t_price)
                        fill_rec = FillRecord(
                            trade_id=f"{t_id}_{m_oid[:10]}",
                            order_uuid=m_order.id,
                            size=m_size,
                            price=m_price,
                            venue_ts=t_ts,
                            recorded_ts=now_ms,
                            run_id=get_run_id(),
                        )
                        if registry.record_fill(fill_rec):
                            summary.fills_recorded += 1
                            summary.transitions.append(f"FILL {m_order.id[:8]} ({m_order.order_id}): +{m_size} @ {m_price}")
                            # Record markout entry
                            fill_sec = (t_ts / 1000.0) if t_ts else (now_ms / 1000.0)
                            registry.log_markout(
                                MarkoutRecord(
                                    ts=fill_sec,
                                    condition_id=m_order.condition_id,
                                    side=m_order.side,
                                    token_id=m_order.token_id,
                                    fill_price=m_price,
                                    size=m_size,
                                    ref_mid=m_price,  # initial ref mid fallback
                                    ref_mid_source="contaminated",
                                    run_id=get_run_id(),
                                )
                            )
                        else:
                            summary.duplicates_ignored += 1

            if matched_any_maker:
                continue

            order = None
            for candidate in (
                t_order_id,
                str(t.get("maker_order_id") or ""),
                str(t.get("order_id") or ""),
            ):
                if not candidate:
                    continue
                order = registry.get_order_by_venue_id(candidate)
                if order is None:
                    order = registry.get_order(candidate)
                if order is not None:
                    break

            if order is None:
                summary.unmatched_trades += 1
                summary.transitions.append(
                    f"UNMATCHED_TRADE {t_id} order_id={t_order_id or 'none'} "
                    f"size={t_size} @ {t_price}"
                )
            else:
                affected_order_ids.add(order.id)
                fill_rec = FillRecord(
                    trade_id=t_id,
                    order_uuid=order.id,
                    size=t_size,
                    price=t_price,
                    venue_ts=t_ts,
                    recorded_ts=now_ms,
                    run_id=get_run_id(),
                )
                if registry.record_fill(fill_rec):
                    summary.fills_recorded += 1
                    summary.transitions.append(f"FILL {order.id[:8]} ({order.order_id}): +{t_size} @ {t_price}")
                    fill_sec = (t_ts / 1000.0) if t_ts else (now_ms / 1000.0)
                    registry.log_markout(
                        MarkoutRecord(
                            ts=fill_sec,
                            condition_id=order.condition_id,
                            side=order.side,
                            token_id=order.token_id,
                            fill_price=t_price,
                            size=t_size,
                            ref_mid=t_price,
                            ref_mid_source="contaminated",
                            run_id=get_run_id(),
                        )
                    )
                else:
                    summary.duplicates_ignored += 1

    # 4. Update order statuses for all active orders plus any order that received fills in this pass
    orders_to_check: dict[str, OrderRecord] = {o.id: o for o in registry.get_active_orders()}
    for o_id in affected_order_ids:
        if o_id not in orders_to_check:
            o = registry.get_order(o_id)
            if o is not None:
                orders_to_check[o.id] = o

    for order in orders_to_check.values():
        size_matched = registry.get_size_matched(order.id)
        is_resting = bool(order.order_id and order.order_id in venue_order_map)
        is_full = (size_matched >= order.original_size - SIZE_EPS)

        if is_full:
            new_status = "filled"
            summary.orders_filled += 1
        elif is_resting:
            new_status = "partial" if size_matched > SIZE_EPS else "open"
            if new_status == "partial" and order.status != "partial":
                summary.orders_partially_filled += 1
        else:
            if size_matched > SIZE_EPS:
                new_status = "partial"
                if order.status != "partial":
                    summary.orders_partially_filled += 1
            elif order.status != "pending":
                new_status = "cancelled"
                summary.orders_cancelled += 1
            else:
                new_status = order.status

        if new_status != order.status:
            registry.update_order_status(order.id, new_status, now_ms)
            summary.transitions.append(f"STATUS {order.id[:8]} ({order.order_id or 'local'}): {order.status} -> {new_status}")
        else:
            registry.update_order_status(order.id, order.status, now_ms)

    return summary


def inventory_from_registry(
    condition_id: str,
    up_token: str,
    down_token: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> "Inventory":
    """Rebuild a market's share inventory from fills and closes in the SQLite registry."""
    from engine.quotes import Inventory

    inv = Inventory()
    db = Path(db_path)
    if not db.is_file():
        return inv

    with get_connection(db) as conn:
        # A `venue_sync` close (the dashboard's Sync) means the venue reports
        # this condition's position as CLOSED -- the account no longer holds
        # it. Its rows carry no leg encoding (both legs write `up_price`), so
        # a per-leg subtraction would guess. Instead, retire every fill that
        # predates the latest such close; fills after it are new exposure and
        # survive. Same timestamp rule as the auto-pairs pass's skip: a close
        # covers the fills that precede it.
        venue_sync_cutoff_s: Optional[float] = None
        for cr in conn.execute(
            """SELECT ts FROM closes
                WHERE condition_id = ? AND method = 'venue_sync'""",
            (condition_id,),
        ).fetchall():
            t = cr["ts"]
            if t:
                venue_sync_cutoff_s = max(venue_sync_cutoff_s or 0.0, float(t))

        rows = conn.execute(
            """
            SELECT o.token_id, o.side, f.size, f.price, f.venue_ts
            FROM fills f
            JOIN orders o ON f.order_uuid = o.id
            WHERE o.condition_id = ?
            ORDER BY f.venue_ts ASC
            """,
            (condition_id,),
        ).fetchall()
        for r in rows:
            tok = str(r["token_id"])
            side = str(r["side"]).upper()
            size = float(r["size"] or 0.0)
            price = float(r["price"] or 0.0)
            vts = float(r["venue_ts"] or 0.0) / 1000.0 if r["venue_ts"] else None

            if vts and venue_sync_cutoff_s is not None and vts <= venue_sync_cutoff_s:
                # Retired by the venue sync: the account no longer holds these
                # shares, and the close row cannot say which leg they were.
                continue

            if tok == up_token:
                if side == "BUY":
                    inv.up_shares += size
                    inv.up_cost += size * price
                elif side == "SELL":
                    inv.up_shares -= size
                    inv.up_cost -= size * price
            elif tok == down_token:
                if side == "BUY":
                    inv.down_shares += size
                    inv.down_cost += size * price
                elif side == "SELL":
                    inv.down_shares -= size
                    inv.down_cost -= size * price
            inv.fills += 1
            if vts:
                inv.last_fill_ts = vts if inv.last_fill_ts is None else max(inv.last_fill_ts, vts)

        # Subtract executed closes. `naked_exit` is U35: ONE leg was sold, and
        # which one is encoded by which price field is set -- the same
        # encoding the sim's stats.inventory_from_db uses and the settled-
        # positions reader expects. The OTHER leg is untouched, so it must
        # not be decremented.
        close_rows = conn.execute(
            """
            SELECT method, shares, up_cost_removed, dn_cost_removed,
                   cost_basis, up_price, dn_price
            FROM closes
            WHERE condition_id = ?
            """,
            (condition_id,),
        ).fetchall()
        for cr in close_rows:
            method = cr["method"]
            sh = float(cr["shares"] or 0.0)
            up_c = cr["up_cost_removed"]
            dn_c = cr["dn_cost_removed"]
            if method == "naked_exit":
                if cr["up_price"] is not None:
                    inv.up_shares = max(0.0, inv.up_shares - sh)
                    if up_c is not None:
                        inv.up_cost = max(0.0, inv.up_cost - float(up_c))
                else:
                    inv.down_shares = max(0.0, inv.down_shares - sh)
                    if dn_c is not None:
                        inv.down_cost = max(0.0, inv.down_cost - float(dn_c))
                continue
            if method == "merge":
                inv.up_shares = max(0.0, inv.up_shares - sh)
                inv.down_shares = max(0.0, inv.down_shares - sh)
                if up_c is not None and dn_c is not None:
                    inv.up_cost = max(0.0, inv.up_cost - float(up_c))
                    inv.down_cost = max(0.0, inv.down_cost - float(dn_c))
    return inv


# --- fleet-wide aggregates (moved from engine.live_exec) ----------------
# `registry_naked_usd` and `registry_committed_usd` are pure registry
# reads -- the fleet's risk caps and the poll loop both need them, so they
# live next to the registry instead of in live_exec's CLI grab-bag.


def registry_naked_usd(registry) -> float:
    """Dollars at risk on open, one-sided live exposure, from the registry.

    Pairs whose condition already has a close are skipped: a merged pair has
    no open exposure, and counting its fills would bill risk the venue no
    longer carries. A partially-exited pair is skipped whole rather than
    over-stated, which is the safe direction for a risk figure.
    """
    from engine.live_pairs import load_pair, PairExitRefused

    with registry._conn() as conn:
        closed_cids = {
            r["condition_id"]
            for r in conn.execute(
                "SELECT DISTINCT condition_id FROM closes WHERE condition_id IS NOT NULL"
            ).fetchall()
        }
        rows = conn.execute(
            "SELECT DISTINCT condition_id, pair_id FROM orders "
            "WHERE pair_id IS NOT NULL"
        ).fetchall()

    total = 0.0
    for r in rows:
        if r["condition_id"] in closed_cids:
            continue
        try:
            pair = load_pair(registry, r["pair_id"])
        except PairExitRefused:
            continue
        naked_sh = pair.get("naked") or 0.0
        if naked_sh > 0:
            total += naked_sh * (pair.get("fill_cost") or 0.0)
    return total


def registry_committed_usd(registry) -> float:
    """Dollars committed fleet-wide, from the registry.

    Inventory cost (every filled share's cost basis whose pair is still open)
    plus the notional resting in unfilled offers. Both are spoken for right
    now: the venue holds collateral against an open bid, and a fill converts
    that promise into inventory without asking -- the same two terms the
    simulation's `fleet_committed_cost` sums, and the same gap that let $767
    of naked exposure hide behind $9,588 of committed capital in 2026-07-30.

    Pairs whose condition already has a close are skipped whole: merged or
    exited inventory is no longer committed. A filled SELL reduces cost basis
    (money came back), so it subtracts; a resting SELL commits no new capital
    and is excluded.
    """
    with registry._conn() as conn:
        closed_cids = {
            r["condition_id"]
            for r in conn.execute(
                "SELECT DISTINCT condition_id FROM closes WHERE condition_id IS NOT NULL"
            ).fetchall()
        }
        inventory = conn.execute(
            """
            SELECT COALESCE(SUM(
                CASE WHEN o.side = 'BUY' THEN f.size * f.price
                     ELSE -f.size * f.price END
            ), 0.0)
            FROM fills f
            JOIN orders o ON f.order_uuid = o.id
            WHERE o.condition_id NOT IN (
                SELECT DISTINCT condition_id FROM closes
                WHERE condition_id IS NOT NULL
            )
            """
        ).fetchone()[0]

    resting = 0.0
    for o in registry.get_active_orders():
        if o.side != "BUY" or o.condition_id in closed_cids:
            continue
        resting += o.price * max(0.0, o.original_size - registry.get_size_matched(o.id))

    return float(inventory) + resting
