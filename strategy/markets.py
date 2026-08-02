"""Discover the currently-live BTC 5-min market via gamma-api events endpoint."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional

import requests


@dataclass(frozen=True)
class LiveMarket:
    condition_id: str
    market_slug: str
    up_token: str
    down_token: str
    start_ts: float  # unix seconds, market opens
    end_ts: float    # unix seconds, market closes
    tick_size: float
    neg_risk: bool

    def t_remaining(self, now: Optional[float] = None) -> float:
        return self.end_ts - (now if now is not None else time.time())


def _parse_market(market: dict) -> Optional[LiveMarket]:
    token_ids_raw = market.get("clobTokenIds")
    if not token_ids_raw:
        return None
    token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
    if len(token_ids) != 2:
        return None

    # eventStartTime is the actual trading-window open (UTC :00/:05/:10 boundary).
    # startDate is when the market was *listed*, often hours earlier.
    start_iso = market.get("eventStartTime")
    end_iso = market.get("endDate") or market.get("endDateIso")
    if not start_iso or not end_iso:
        return None

    start_ts = _iso_to_unix(start_iso)
    end_ts = _iso_to_unix(end_iso)
    return LiveMarket(
        condition_id=market["conditionId"],
        market_slug=market.get("slug", ""),
        up_token=str(token_ids[0]),
        down_token=str(token_ids[1]),
        start_ts=start_ts,
        end_ts=end_ts,
        tick_size=float(market.get("orderPriceMinTickSize") or 0.01),
        neg_risk=bool(market.get("negRisk", False)),
    )


def _iso_to_unix(s: str) -> float:
    # tolerate "Z" suffix
    from datetime import datetime
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).timestamp()


def fetch_live_market(gamma_host: str, series_slug: str) -> Optional[LiveMarket]:
    """Return the single 5-min BTC market that's currently live, or None."""
    url = f"{gamma_host}/events"
    params = {"series_slug": series_slug, "closed": "false", "limit": 500}
    r = requests.get(url, params=params, timeout=5)
    r.raise_for_status()
    events = r.json()

    now = time.time()
    candidates: list[LiveMarket] = []
    for ev in events:
        markets = ev.get("markets") or []
        for m in markets:
            lm = _parse_market(m)
            if lm and lm.start_ts <= now < lm.end_ts:
                candidates.append(lm)
    if not candidates:
        return None
    candidates.sort(key=lambda m: m.start_ts, reverse=True)
    return candidates[0]


def fetch_pinned_market(condition_id: str,
                        require_rewards: bool = True) -> Optional[LiveMarket]:
    """One specific long-dated market, pinned by condition_id.

    The 5-min BTC series pays nothing for resting (rewards.rates = null); these
    markets do. They also do not roll every five minutes, so there is no window
    to discover -- we quote the same book all day. `end_ts` is the real
    resolution date (months out), which makes t_remaining effectively infinite
    and disables every 5-min-specific timing rule by construction.

    `require_rewards` refuses a market that is not actually funded. A market
    can carry min_size and max_spread while `rates` is null, which looks
    configured and pays zero -- that exact trap cost us a whole run, and it is
    still the right default for a bot whose only income is rent.

    The fleet passes False, because "pays no rewards" stopped being
    disqualifying when spread capture landed: those are the markets that
    actually trade, and refusing them here made them unloadable, unsampled and
    therefore unfundable however well the allocator sized them. Whether a
    market is worth funding is the allocator's decision and it is made from
    `run/markets.json`; this function's job is only to say whether the market
    can be quoted at all.
    """
    r = requests.get(f"https://clob.polymarket.com/markets/{condition_id}", timeout=15)
    r.raise_for_status()
    m = r.json()

    rewards = m.get("rewards") or {}
    rates = rewards.get("rates") or []
    daily = sum(x.get("rewards_daily_rate", 0) or 0 for x in rates)
    if require_rewards and daily <= 0:
        return None
    if m.get("closed") or not m.get("accepting_orders"):
        return None

    toks = [t.get("token_id") for t in (m.get("tokens") or [])]
    if len(toks) != 2:
        return None

    end_iso = m.get("end_date_iso")
    end_ts = _iso_to_unix(end_iso) if end_iso else (time.time() + 365 * 86400)
    return LiveMarket(
        condition_id=condition_id,
        market_slug=m.get("market_slug") or condition_id[:10],
        up_token=str(toks[0]),
        down_token=str(toks[1]),
        start_ts=time.time() - 1.0,
        end_ts=end_ts,
        tick_size=float(m.get("minimum_tick_size") or 0.01),
        neg_risk=bool(m.get("neg_risk", False)),
    )


def market_meta(condition_id: str) -> dict:
    """Question text, link and funded daily rate, for the dashboard header."""
    try:
        m = requests.get(f"https://clob.polymarket.com/markets/{condition_id}",
                         timeout=15).json()
    except Exception:
        return {}
    rw = m.get("rewards") or {}
    slug = m.get("market_slug") or ""
    return {
        "question": m.get("question") or condition_id[:12],
        "slug": slug,
        "url": f"https://polymarket.com/market/{slug}" if slug else "",
        "daily_rate": sum(x.get("rewards_daily_rate", 0) or 0
                          for x in (rw.get("rates") or [])),
        "max_spread": rw.get("max_spread"),
        "min_size": rw.get("min_size"),
        "tick": m.get("minimum_tick_size"),
    }


if __name__ == "__main__":
    from strategy.config import load

    cfg = load()
    m = fetch_live_market(cfg.gamma_host, cfg.series_slug)
    if not m:
        print("no live market right now")
    else:
        rem = m.t_remaining()
        print(f"live: {m.market_slug}  t_remaining={rem:.1f}s")
        print(f"  cond={m.condition_id}")
        print(f"  up_token={m.up_token[:18]}...")
        print(f"  down_token={m.down_token[:18]}...")
        print(f"  tick={m.tick_size}  neg_risk={m.neg_risk}")
