"""Maker dashboard. Separate app, separate port (8788), separate DB.

Self-contained HTML -- no build step, no shared components with the taker UI, so
the two can never drift into each other.

Laid out as a KANBAN PIPELINE: every market flows left to right through the
stages it actually passes through --
    DECIDE -> REST (quote on book) -> FILL -> HOLD (position) -> SETTLE
New cards animate in, so you can watch work move down the pipeline rather than
reading five disconnected tables.
"""
from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from strategy import kpi, store
from strategy.config import load as load_cfg

cfg = load_cfg()
app = FastAPI(title="maker sim")

_cache = {"ts": 0.0, "data": None}


@app.get("/api/health")
def health():
    return {"ok": True, "ts": time.time()}


@app.get("/api/state")
def state():
    now = time.time()
    if _cache["data"] is None or now - _cache["ts"] > 4:
        try:
            _cache["data"] = kpi.report()
            _cache["ts"] = now
        except Exception as e:
            return {"error": str(e)}
    d = dict(_cache["data"])
    d["now"] = now
    d["live"] = store.get_live_state()
    d["decisions"] = kpi.recent_decisions(40)
    d["recent_fills"] = kpi.recent_fills(30)
    d["config"] = {
        "market_title": cfg.market_title,
        "market_url": cfg.market_url,
        "market_daily_rate": cfg.market_daily_rate,
        "reward_offset": cfg.reward_offset,
        "max_spread_from_mid": cfg.max_spread_from_mid,
        "quote_shares": cfg.quote_shares,
        "target_balance": cfg.target_balance,
        "max_pair_cost": cfg.max_pair_cost,
        "max_cost_per_market": cfg.max_cost_per_market,
        "hedge_fillable_min_rate": cfg.hedge_fillable_min_rate,
    }
    return d





from server.kanban import PAGE


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE
