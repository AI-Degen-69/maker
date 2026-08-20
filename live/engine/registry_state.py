"""The read side of the live registry (live/run/live.db).

Mirrors the root tree's State-reader split: strategy/store.py owns writes and
schema, strategy/stats.py owns every read that produces dashboard numbers. Here
OrderRegistry owns writes and reconcile, and this module owns the position
summary the live dashboard renders — orders, fills, capital commitment, poll
staleness, hedge-state classification (RESTING / BALANCED / NAKED / SETTLED /
REFUSED), and the naked-leg exposure math.

Extracted verbatim from live/dash/live_dash.py (query_db_state + _market_identity)
so the dashboard becomes a thin consumer. The payload shape is unchanged: this is
relocation for locality, not a redesign.

Read-only contract: the registry is opened with a strict `mode=ro` URI
connection on every call. The dashboard must never write — the engine's
reconcile loop is the only writer, and a read-write connection from the page
would create WAL side files and lock contention. A poll is allowed to fail and
report "stale"; it is never allowed to corrupt.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

# live/, one level up from live/engine/. Same tree-boundary rule as live_exec:
# `engine` must resolve inside live/ and nowhere else.
LIVE_ROOT = Path(__file__).resolve().parent.parent
# The ranker writes run/markets.json at the repo root; this module reads it as
# data, never as code.
REPO_ROOT = LIVE_ROOT.parent

# A cycle older than this is reported as a stale poll (seconds).
STALE_THRESHOLD_SEC = 30.0


def _market_identity(condition_id: str, closes_by_cid: dict) -> dict:
    """Who is this market, in words a human recognises."""
    out = {"condition_id": condition_id, "title": None, "slug": None,
           "url": None, "days_to_resolve": None, "min_size": None,
           "volume_24h": None, "source": None}
    if not condition_id:
        return out
    try:
        feed = json.loads((REPO_ROOT / "run" / "markets.json").read_text(encoding="utf-8"))
    except Exception:
        feed = []
    for row in feed if isinstance(feed, list) else []:
        if (row.get("cid") or "").lower() == condition_id.lower():
            out.update({
                "title": row.get("title") or row.get("event_title"),
                "slug": row.get("slug"),
                "days_to_resolve": row.get("days_to_resolve"),
                "min_size": row.get("min_size"),
                "volume_24h": row.get("volume_24h"),
                "source": row.get("source"),
            })
            break
    if not out["slug"]:
        closed = closes_by_cid.get(condition_id) or {}
        out["slug"] = closed.get("market_slug")
    if not out["title"] and out["slug"]:
        out["title"] = out["slug"].replace("-", " ").title()
    elif not out["title"]:
        out["title"] = f"Market {condition_id[:10]}...{condition_id[-6:]}" if len(condition_id) > 16 else condition_id
    if out["slug"]:
        out["url"] = f"https://polymarket.com/market/{out['slug']}"
    return out


def summarize_state(db_path: Path | str, now: float | None = None) -> dict[str, Any]:
    """Read the live registry database in read-only URI mode and summarize state.

    `now` is a test seam: pass a fixed timestamp for deterministic staleness
    assertions. Production callers omit it and get the wall clock.
    """
    path = Path(db_path)
    now_ts = now if now is not None else time.time()
    now_ms = int(now_ts * 1000)

    empty_payload = {
        "empty": True,
        "db_path": str(path),
        "server_time_ms": now_ms,
        "message": "Database not initialized or no live orders found.",
        "pairs": [],
        "orders": [],
        "fills": [],
        "capital": {
            "resting_committed": 0.0,
            "filled_committed": 0.0,
            "total_committed": 0.0,
        },
        "last_polled_ts": None,
        "seconds_since_poll": None,
        "stale": False,
        "idle": True,
        "at_stake": False,
        "reconcile_lock": {
            "held": False,
            "holder": None,
            "acquired_ts": None,
            "age_sec": None,
        },
    }

    if not path.exists():
        empty_payload["message"] = f"Database file not found at {path}"
        return empty_payload

    # Strictly read-only connection
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
        con.row_factory = sqlite3.Row
    except Exception as e:
        empty_payload["message"] = f"Failed to connect in read-only mode: {e}"
        return empty_payload

    try:
        cur = con.cursor()
        tables = {
            row[0]
            for row in cur.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }

        if "orders" not in tables:
            con.close()
            empty_payload["message"] = "Orders table does not exist in database."
            return empty_payload

        # Query orders summary
        if "order_summary" in tables:
            orders_rows = cur.execute("""
                SELECT id, order_id, condition_id, token_id, side, price, original_size,
                       status, posted_ts, last_polled_ts, pair_id, max_pair_cost_at_post,
                       size_matched
                FROM order_summary
                ORDER BY posted_ts DESC
            """).fetchall()
        else:
            orders_rows = cur.execute("""
                SELECT o.id, o.order_id, o.condition_id, o.token_id, o.side, o.price, o.original_size,
                       o.status, o.posted_ts, o.last_polled_ts, o.pair_id, o.max_pair_cost_at_post,
                       COALESCE(SUM(f.size), 0.0) AS size_matched
                FROM orders o
                LEFT JOIN fills f ON f.order_uuid = o.id
                GROUP BY o.id
                ORDER BY o.posted_ts DESC
            """).fetchall()

        # Settlement closes
        closes_by_cid: dict[str, dict[str, Any]] = {}
        if "closes" in tables:
            for c_row in cur.execute("""
                SELECT condition_id, method, market_slug, SUM(shares) AS shares,
                       SUM(COALESCE(realized_pnl, 0.0)) AS pnl,
                       MAX(ts) AS last_ts
                FROM closes
                WHERE condition_id IS NOT NULL
                GROUP BY condition_id, method
            """).fetchall():
                slot = closes_by_cid.setdefault(
                    c_row["condition_id"],
                    {"shares": 0.0, "pnl": 0.0, "last_ts": 0.0, "methods": [],
                     "market_slug": c_row["market_slug"]},
                )
                slot["shares"] += float(c_row["shares"] or 0.0)
                slot["pnl"] += float(c_row["pnl"] or 0.0)
                slot["last_ts"] = max(slot["last_ts"], float(c_row["last_ts"] or 0.0))
                slot["methods"].append(c_row["method"] or "?")

        # Query fills
        fills_rows = []
        if "fills" in tables:
            fills_rows = cur.execute("""
                            SELECT f.trade_id, f.order_uuid, f.size, f.price, f.venue_ts,
                                   o.side, o.pair_id, o.token_id, o.condition_id
                            FROM fills f
                            LEFT JOIN orders o ON o.id = f.order_uuid
                            ORDER BY f.venue_ts DESC
                        """).fetchall()

        # Check reconcile lock
        rec_lock_info = {
            "held": False,
            "holder": None,
            "acquired_ts": None,
            "age_sec": None,
        }
        if "reconcile_lock" in tables:
            lock_row = cur.execute("SELECT holder, acquired_ts FROM reconcile_lock WHERE id = 1").fetchone()
            if lock_row and lock_row["acquired_ts"] is not None:
                acq = float(lock_row["acquired_ts"])
                # If acquired within 5 minutes, consider it active
                if now_ms - acq < 300000:
                    rec_lock_info["held"] = True
                    rec_lock_info["holder"] = lock_row["holder"]
                    rec_lock_info["acquired_ts"] = acq
                    rec_lock_info["age_sec"] = max(0.0, round((now_ms - acq) / 1000.0, 1))

        con.close()
    except Exception as e:
        empty_payload["message"] = f"Error reading database: {e}"
        return empty_payload

    if not orders_rows:
        empty_payload["reconcile_lock"] = rec_lock_info
        return empty_payload

    max_poll_ms = 0
    resting_committed = 0.0
    filled_committed = 0.0

    orders_list = []
    for r in orders_rows:
        o = dict(r)
        lp = o.get("last_polled_ts") or 0
        if lp > max_poll_ms:
            max_poll_ms = lp

        o["age_sec"] = max(0.0, round((now_ms - o["posted_ts"]) / 1000.0, 1))
        o["size_remaining"] = max(0.0, float(o["original_size"]) - float(o["size_matched"]))
        o["is_unattributed"] = (o.get("status") == "unattributed")

        # Committed math
        st = o["status"]
        sz_rem = o["size_remaining"]
        px = float(o["price"])
        sz_mat = float(o["size_matched"])

        if st in ("open", "pending", "partial"):
            resting_committed += sz_rem * px
        if sz_mat > 0:
            # Fallback only. The order's limit price is what we asked to pay; a
            # maker fill often lands better, so the fills table overrides this
            # below. $0.625 asked vs $0.620 paid is a 0.5c lie per share.
            filled_committed += sz_mat * px

        orders_list.append(o)

    # Poll staleness
    seconds_since_poll = round((now_ms - max_poll_ms) / 1000.0, 1) if max_poll_ms > 0 else None
    stale = (seconds_since_poll is not None and seconds_since_poll > STALE_THRESHOLD_SEC)

    # Idle check
    has_resting = any(o["status"] in ("open", "pending", "partial") for o in orders_list)
    fills_list = []
    for f in fills_rows:
        f_dict = dict(f)
        f_dict["age_sec"] = max(0.0, round((now_ms - f_dict["venue_ts"]) / 1000.0, 1))
        try:
            dt = datetime.datetime.fromtimestamp(f_dict["venue_ts"] / 1000.0)
            f_dict["venue_time_str"] = dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            f_dict["venue_time_str"] = str(f_dict["venue_ts"])
        fills_list.append(f_dict)

    # Price the filled leg at what it actually cost, not at what we bid.
    # A backfilled order can carry a NULL avg_fill_price, so rebuild it from the
    # trades themselves rather than falling back to the limit price.
    _fill_agg: dict[str, list[float]] = {}
    for f in fills_list:
        sz = float(f.get("size") or 0.0)
        agg = _fill_agg.setdefault(f["order_uuid"], [0.0, 0.0])
        agg[0] += sz
        agg[1] += sz * float(f.get("price") or 0.0)
    avg_fill_by_order = {oid: (v[1] / v[0]) for oid, v in _fill_agg.items() if v[0] > 0}

    fills_cost = sum(float(f.get("size") or 0.0) * float(f.get("price") or 0.0) for f in fills_list)
    if fills_cost > 0:
        filled_committed = fills_cost
    total_committed = resting_committed + filled_committed

    # Group into pairs
    pairs_map: dict[str, dict[str, Any]] = {}
    for o in orders_list:
        pid = o["pair_id"] or f"unpaired_{o['id']}"
        if pid not in pairs_map:
            pairs_map[pid] = {
                "pair_id": pid,
                "condition_id": o["condition_id"],
                "max_pair_cost_at_post": o["max_pair_cost_at_post"],
                "orders": [],
            }
        pairs_map[pid]["orders"].append(o)

    pairs_list = []
    for pdata in pairs_map.values():
        legs = pdata["orders"]
        _open_by_token: dict[str, float] = {}
        for leg in sorted(legs, key=lambda x: (x["side"] != "BUY", x["posted_ts"])):
            _open_by_token.setdefault(
                leg["token_id"],
                float(
                    leg.get("avg_fill_price")
                    or avg_fill_by_order.get(leg["id"])
                    or leg["price"]
                ),
            )
        combined_price = sum(_open_by_token.values())
        pdata["combined_price"] = round(combined_price, 4)
        pdata["combined_price_is_paid"] = any(
            leg.get("avg_fill_price") or avg_fill_by_order.get(leg["id"]) for leg in legs
        )

        order_ids_in_pair = {leg["id"] for leg in legs}
        pair_fills = [f for f in fills_list if f["order_uuid"] in order_ids_in_pair]
        pair_fills.sort(key=lambda x: x["venue_ts"])

        tokens: dict[str, dict[str, Any]] = {}
        for o in legs:
            tok = tokens.setdefault(o["token_id"], {
                "token_id": o["token_id"],
                "net_matched": 0.0,
                "notional": 0.0,
                "orders": [],
            })
            matched = float(o["size_matched"])
            signed = -matched if o["side"] == "SELL" else matched
            tok["net_matched"] += signed
            tok["notional"] += signed * float(o["price"])
            tok["orders"].append(o)

        for t in tokens.values():
            t["net_matched"] = round(t["net_matched"], 6)
            t["avg_price"] = (
                abs(t["notional"] / t["net_matched"]) if abs(t["net_matched"]) > 1e-9
                else (float(t["orders"][0]["price"]) if t["orders"] else 0.0)
            )

        pdata["tokens"] = list(tokens.values())

        def _naked_from(tok: dict[str, Any], shares: float,
                        _toks: dict[str, dict[str, Any]] = tokens) -> dict[str, Any]:
            """Build the naked payload for `shares` unhedged on `tok`."""
            tok_order_ids = {o["id"] for o in tok["orders"]}
            tok_fills = [f for f in pair_fills if f["order_uuid"] in tok_order_ids]
            since = (tok_fills[0]["venue_ts"] if tok_fills
                     else min(o["posted_ts"] for o in tok["orders"]))
            price = tok["avg_price"]
            nets = [t["net_matched"] for t in _toks.values()]
            return {
                "unhedged_shares": round(abs(shares), 4),
                "unhedged_side": "LONG" if shares > 0 else "SHORT",
                "unhedged_token_id": tok["token_id"],
                "unhedged_price": round(price, 4),
                "unhedged_dollars": round(abs(shares) * price, 2),
                "long_leg_matched": round(max(nets), 4),
                "short_leg_matched": round(min(nets), 4),
                "naked_since_ts": since,
                "seconds_naked": max(0.0, round((now_ms - since) / 1000.0, 1)),
            }

        naked_info = None
        if len(tokens) > 2:
            hedge_state = "REFUSED"
            pdata["refused_reason"] = (
                f"pair spans {len(tokens)} token ids; a pair is two legs. "
                f"Position cannot be classified from a reduced view."
            )
        else:
            nets = [t["net_matched"] for t in tokens.values()]
            if all(abs(n) <= 1e-6 for n in nets):
                working = {"open", "pending", "partial"}
                hedge_state = ("RESTING"
                               if any(leg["status"] in working for leg in legs)
                               else "CLOSED")
            elif len(tokens) == 2:
                a, b = list(tokens.values())
                diff = round(a["net_matched"] - b["net_matched"], 6)
                if abs(diff) <= 1e-6:
                    held = min(a["net_matched"], b["net_matched"])
                    closed = closes_by_cid.get(pdata.get("condition_id") or "")
                    if closed and closed["shares"] + 1e-6 >= held > 0:
                        hedge_state = "SETTLED"
                        pdata["settlement"] = {
                            "shares": closed["shares"],
                            "pnl": closed["pnl"],
                            "methods": sorted(set(closed["methods"])),
                            "ts": closed["last_ts"],
                        }
                    else:
                        hedge_state = "BALANCED"
                else:
                    hedge_state = "NAKED"
                    heavy = a if a["net_matched"] > b["net_matched"] else b
                    naked_info = _naked_from(heavy, abs(diff))
            else:
                only = next(iter(tokens.values()))
                hedge_state = "NAKED"
                naked_info = _naked_from(only, only["net_matched"])

        pdata["hedge_state"] = hedge_state
        pdata["naked_info"] = naked_info
        pdata["market"] = _market_identity(pdata.get("condition_id"), closes_by_cid)
        pairs_list.append(pdata)

    has_naked = any(p["hedge_state"] == "NAKED" for p in pairs_list)
    has_balanced = any(p["hedge_state"] == "BALANCED" for p in pairs_list)
    idle = not has_resting and not has_naked and not has_balanced
    at_stake = has_resting or has_naked

    return {
        "empty": False,
        "db_path": str(path),
        "server_time_ms": now_ms,
        "pairs": pairs_list,
        "orders": orders_list,
        "fills": fills_list,
        "capital": {
            "resting_committed": round(resting_committed, 2),
            "filled_committed": round(filled_committed, 2),
            "total_committed": round(total_committed, 2),
        },
        "last_polled_ts": max_poll_ms if max_poll_ms > 0 else None,
        "seconds_since_poll": seconds_since_poll,
        "stale": stale,
        "idle": idle,
        "at_stake": at_stake,
        "reconcile_lock": rec_lock_info,
    }
