"""Measure 5m/15m crypto oscillation for spread capture.

Polls live BTC/ETH/BNB/SOL/XRP 5m + 15m markets (10 series) every second,
records mid drift from 0.50, max_up / max_down, pair_cost at SPREAD=2 (offset 0.02),
and classifies monotonic vs oscillating.

Writes:
  run/oscillation_snapshots.jsonl  -- one line per poll per market
  run/oscillation_windows.jsonl    -- one line per closed window (summary)
  run/oscillation_summary.json     -- aggregated per series for dashboard

Usage:
  python -m scripts.measure_5m_oscillation          # continuous
  python -m scripts.measure_5m_oscillation --once   # single poll (for testing)
  python -m scripts.measure_5m_oscillation --windows 20  # run until 20 windows closed
"""
from __future__ import annotations
import argparse
import json
import time
import math
from pathlib import Path
from collections import defaultdict, deque

import requests

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "run"
RUN.mkdir(exist_ok=True)

SNAP_FILE = RUN / "oscillation_snapshots.jsonl"
WIN_FILE = RUN / "oscillation_windows.jsonl"
SUMMARY_FILE = RUN / "oscillation_summary.json"

GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

SERIES = [
    ("btc-up-or-down-5m", 300, "BTC 5m"),
    ("eth-up-or-down-5m", 300, "ETH 5m"),
    ("bnb-up-or-down-5m", 300, "BNB 5m"),
    ("sol-up-or-down-5m", 300, "SOL 5m"),
    ("xrp-up-or-down-5m", 300, "XRP 5m"),
    ("btc-up-or-down-15m", 900, "BTC 15m"),
    ("eth-up-or-down-15m", 900, "ETH 15m"),
    ("bnb-up-or-down-15m", 900, "BNB 15m"),
    ("sol-up-or-down-15m", 900, "SOL 15m"),
    ("xrp-up-or-down-15m", 900, "XRP 15m"),
]

# offset for SPREAD=2 -> 0.02 below mid, pair = 1.00 - 0.04 = 0.96
SPREAD_OFFSET = 0.02
# thresholds for oscillation classification
OSC_THRESH_CENTS = [2.0, 3.0]  # 0.02, 0.03

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})
SESSION.mount("https://", requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=0))

def iso_to_unix(s: str) -> float:
    from datetime import datetime
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).timestamp()

def fetch_live_for_series(series_slug: str):
    try:
        r = SESSION.get(f"{GAMMA_HOST}/events", params={"series_slug": series_slug, "closed": "false", "limit": 500}, timeout=(3.05,5))
        r.raise_for_status()
        events = r.json()
    except Exception as e:
        return None, f"gamma err {e}"
    now = time.time()
    candidates = []
    for ev in events:
        for m in ev.get("markets") or []:
            try:
                import json as j
                raw = m.get("clobTokenIds")
                tids = j.loads(raw) if isinstance(raw, str) else raw
                if not tids or len(tids) != 2:
                    continue
                st = iso_to_unix(m.get("eventStartTime"))
                et = iso_to_unix(m.get("endDate") or m.get("endDateIso"))
                if st <= now < et:
                    candidates.append((st, et, m))
            except: continue
    if not candidates:
        return None, "no live"
    candidates.sort(key=lambda x: x[0], reverse=True)
    st, et, m = candidates[0]
    import json as j
    raw = m.get("clobTokenIds")
    tids = j.loads(raw) if isinstance(raw, str) else raw
    return {
        "conditionId": m["conditionId"],
        "slug": m["slug"],
        "start_ts": st, "end_ts": et,
        "up_token": str(tids[0]), "down_token": str(tids[1]),
        "series": series_slug,
    }, None

def fetch_book(token_id: str):
    try:
        r = SESSION.get(f"{CLOB_HOST}/book", params={"token_id": token_id}, timeout=(3.05,5))
        r.raise_for_status()
        raw = r.json()
        bids = {}
        asks = {}
        for side, tgt in (("bids", bids), ("asks", asks)):
            for row in raw.get(side) or []:
                try:
                    p = round(float(row["price"]),4); s = float(row["size"])
                    tgt[p]=s
                except: continue
        best_bid = max(bids) if bids else None
        best_ask = min(asks) if asks else None
        return {"bids": bids, "asks": asks, "best_bid": best_bid, "best_ask": best_ask}
    except Exception as e:
        return {"bids": {}, "asks": {}, "best_bid": None, "best_ask": None, "err": str(e)}

# in-memory per-window collectors: cid -> {series, slug, start_ts, end_ts, mids: deque, pair_costs: [], queue_depths: []}
windows = {}

def classify_window(mids: list[float]):
    if not mids:
        return "no_data"
    base = 0.50
    max_up = max(mids) - base  # e.g. 0.53-0.50 = 0.03
    max_down = base - min(mids)  # 0.50-0.47 = 0.03
    # monotonic: only one direction exceeds 2c, other <1c
    # oscillating: both exceed 2c
    # flat: neither exceeds 2c
    up2 = max_up >= 0.02
    down2 = max_down >= 0.02
    if up2 and down2:
        return "oscillating"
    if up2 or down2:
        # check if it went one way then never came back >1c opposite
        return "monotonic"
    return "flat"

def update_summary():
    # aggregate from WIN_FILE
    per_series = defaultdict(list)
    if WIN_FILE.exists():
        for line in WIN_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip(): continue
            try:
                w = json.loads(line)
                per_series[w["series"]].append(w)
            except: continue
    summary = {}
    for series_slug, duration, label in SERIES:
        ws = per_series.get(series_slug, [])
        n = len(ws)
        if n==0:
            summary[series_slug] = {"label": label, "duration": duration, "windows": 0, "osc_2c": 0, "osc_3c": 0, "monotonic": 0, "flat":0, "pair_cost_median": None, "recent": []}
            continue
        osc2 = sum(1 for w in ws if w["max_up"]>=0.02 and w["max_down"]>=0.02 or w["max_up"]>=0.02 or w["max_down"]>=0.02)  # actually need >=2c either direction
        # correct: any direction >=2c
        any2 = sum(1 for w in ws if max(w["max_up"], w["max_down"])>=0.02)
        any3 = sum(1 for w in ws if max(w["max_up"], w["max_down"])>=0.03)
        mono = sum(1 for w in ws if w["class"]=="monotonic")
        flat = sum(1 for w in ws if w["class"]=="flat")
        osc = sum(1 for w in ws if w["class"]=="oscillating")
        # pair cost median (resting 0.96 constant, but actual touch pair)
        pcs = [w.get("touch_pair_median") for w in ws if w.get("touch_pair_median")]
        pcs_median = sorted(pcs)[len(pcs)//2] if pcs else None
        # also mid excursion median
        summary[series_slug] = {
            "label": label, "duration": duration, "windows": n,
            "any_2c": any2, "any_3c": any3,
            "oscillating": osc, "monotonic": mono, "flat": flat,
            "pair_cost_median": pcs_median,
            "recent": ws[-10:][::-1],  # last 10 windows newest first
        }
    SUMMARY_FILE.write_text(json.dumps({"ts": time.time(), "per_series": summary}, indent=2), encoding="utf-8")
    return summary

def poll_once():
    now = time.time()
    for series_slug, duration, label in SERIES:
        info, err = fetch_live_for_series(series_slug)
        if not info:
            continue
        cid = info["conditionId"]
        # init window collector if new
        if cid not in windows:
            windows[cid] = {"series": series_slug, "slug": info["slug"], "start_ts": info["start_ts"], "end_ts": info["end_ts"], "mids": [], "touch_pairs": [], "snap_count": 0, "label": label, "duration": duration}
        # fetch books
        ub = fetch_book(info["up_token"])
        db = fetch_book(info["down_token"])
        # compute mid_up from UP book
        mid = None
        if ub["best_bid"] is not None and ub["best_ask"] is not None:
            mid = (ub["best_bid"] + ub["best_ask"])/2.0
        elif ub["best_bid"] is not None:
            mid = ub["best_bid"] + 0.005
        elif ub["best_ask"] is not None:
            mid = ub["best_ask"] - 0.005
        # touch pair = up_ask + down_ask (cost to cross both)
        touch_pair = None
        if ub["best_ask"] is not None and db["best_ask"] is not None:
            touch_pair = ub["best_ask"] + db["best_ask"]
        # resting pair at SPREAD 2
        resting_pair = 1.0 - 2*SPREAD_OFFSET  # 0.96
        # queue ahead at resting price (approx: sum sizes at price >= resting_price for asks? but we rest at bid, so queue at bid level)
        # For UP side, resting bid = mid - 0.02. Queue = sum of bid sizes at price >= resting_bid (ahead of us if we are at back)
        queue_up = None
        if mid is not None:
            rest = round(mid - SPREAD_OFFSET,3)
            # count bids at >= rest
            queue_up = sum(s for p,s in ub["bids"].items() if p >= rest) if ub["bids"] else 0
            windows[cid]["mids"].append(mid)
            if touch_pair is not None:
                windows[cid]["touch_pairs"].append(touch_pair)
        snap = {
            "ts": now, "series": series_slug, "cid": cid, "slug": info["slug"],
            "mid": mid, "touch_pair": touch_pair, "resting_pair": resting_pair,
            "queue_up": queue_up,
            "up_bid": ub["best_bid"], "up_ask": ub["best_ask"],
            "down_bid": db["best_bid"], "down_ask": db["best_ask"],
            "t_rem": info["end_ts"] - now,
        }
        # append snapshot line
        with open(SNAP_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(snap)+"\n")
        windows[cid]["snap_count"] += 1
    # check for closed windows
    now = time.time()
    closed = []
    for cid, w in list(windows.items()):
        if w["end_ts"] < now:
            mids = w["mids"]
            if mids:
                max_up = max(mids) - 0.50
                max_down = 0.50 - min(mids)
                min_mid = min(mids); max_mid = max(mids)
                start_mid = mids[0]; close_mid = mids[-1]
                cls = classify_window(mids)
            else:
                max_up = max_down = 0; min_mid=max_mid=start_mid=close_mid=None; cls="no_data"
            tps = w["touch_pairs"]
            tp_med = sorted(tps)[len(tps)//2] if tps else None
            rec = {
                "series": w["series"], "label": w["label"], "duration": w["duration"],
                "cid": cid, "slug": w["slug"],
                "start_ts": w["start_ts"], "end_ts": w["end_ts"],
                "closed_ts": now,
                "snaps": w["snap_count"],
                "start_mid": round(start_mid,4) if start_mid is not None else None,
                "close_mid": round(close_mid,4) if close_mid is not None else None,
                "max_up": round(max_up,4) if mids else 0, "max_down": round(max_down,4) if mids else 0,
                "min_mid": round(min_mid,4) if min_mid is not None else None,
                "max_mid": round(max_mid,4) if max_mid is not None else None,
                "class": cls,
                "touch_pair_median": round(tp_med,4) if tp_med else None,
                "url": f"https://polymarket.com/market/{w['slug']}" if w["slug"] else "",
            }
            with open(WIN_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec)+"\n")
            closed.append(rec)
            del windows[cid]
    if closed:
        update_summary()
        for c in closed:
            print(f"closed {c['label']} {c['slug'][:30]} class={c['class']} max_up={c['max_up']*100:.1f}c max_down={c['max_down']*100:.1f}c snaps={c['snaps']}")
    else:
        # update summary periodically even without close (for live bars)
        if int(now) % 10 == 0:
            update_summary()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single poll then exit")
    ap.add_argument("--windows", type=int, default=0, help="run until N windows closed")
    args = ap.parse_args()
    print(f"series: {len(SERIES)} SPREAD_OFFSET={SPREAD_OFFSET} -> resting_pair={1-2*SPREAD_OFFSET:.2f}")
    print(f"snapshot -> {SNAP_FILE}  windows -> {WIN_FILE}")
    if args.once:
        poll_once()
        update_summary()
        print("once done")
        return
    closed_total = 0
    # count existing windows
    if WIN_FILE.exists():
        closed_total = sum(1 for _ in WIN_FILE.read_text(encoding="utf-8").splitlines() if _.strip())
    try:
        while True:
            poll_once()
            # count closed
            if args.windows:
                cur = sum(1 for _ in WIN_FILE.read_text(encoding="utf-8").splitlines() if _.strip()) if WIN_FILE.exists() else 0
                if cur >= args.windows:
                    print(f"reached {args.windows} windows, exiting")
                    break
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("interrupted")
        update_summary()

if __name__ == "__main__":
    main()
