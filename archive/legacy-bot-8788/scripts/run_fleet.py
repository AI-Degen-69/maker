"""Launch one bot + one dashboard per funded reward market.

Four bots, identical settings, different markets. If results differ, the
MARKET is the variable -- nothing else changes between them.

Every market is re-verified at launch and REFUSED if it is not actually
funded. That check exists because the previous run quoted btc-updown-5m for
hours on the belief that it paid for resting liquidity: it carries
rewards.min_size and rewards.max_spread, which look like a configured reward
market, while rewards.rates is null and the true payout for resting is $0.
Config that looks funded is not funded. Only a positive rewards_daily_rate is.

    python -m scripts.run_fleet          # verify + launch
    python -m scripts.run_fleet --check  # verify only, launch nothing
    python -m scripts.run_fleet --stop   # stop everything
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RUN = ROOT / "run"
LOGS = ROOT / "logs"

# Chosen by expected income, not by headline rate. A $300/day market with 10x
# our depth already resting in it pays less than a $50/day market with a thin
# book, because the pool splits by score share.
MARKETS = [
    "0x0d9d760ff17a0e64ff9b67f48893c0b1ae4874cd462ce6c2d38c82e2b9171fda",
    "0x76c1a69f2b0a7fcfa97b56ddbeaad1981a8cb3d8f24c1adce62ffa2d010ebbe0",
    "0x0d62a8571af67005063a9182c72ede090f29f6e82d8680d61060d9756b49140f",
    "0x55a5a4b50b7947482e95af8f638fa0b8d9a46740c5cd04c6799443e83565176a",
]
BASE_PORT = 8801


def verify(cid: str) -> dict | None:
    """Return launch parameters, or None if this market does not pay."""
    try:
        m = requests.get(f"https://clob.polymarket.com/markets/{cid}", timeout=20).json()
    except Exception as e:
        print(f"  !! fetch failed: {e}")
        return None

    rw = m.get("rewards") or {}
    rates = rw.get("rates") or []
    daily = sum(x.get("rewards_daily_rate", 0) or 0 for x in rates)

    checks = {
        "funded (rates != null)": daily > 0,
        "accepting orders": bool(m.get("accepting_orders")),
        "not closed": not m.get("closed"),
        "two tokens": len(m.get("tokens") or []) == 2,
    }
    # A live two-sided book is required: with no ask there is no midpoint, and
    # the reward score is defined as distance FROM the midpoint.
    book_ok = False
    toks = [t.get("token_id") for t in (m.get("tokens") or [])]
    if toks:
        try:
            b = requests.get("https://clob.polymarket.com/book",
                             params={"token_id": toks[0]}, timeout=20).json()
            book_ok = bool(b.get("bids")) and bool(b.get("asks"))
        except Exception:
            book_ok = False
    checks["live two-sided book"] = book_ok

    for name, ok in checks.items():
        print(f"     {'PASS' if ok else 'FAIL'}  {name}")
    if not all(checks.values()):
        return None

    return {
        "cid": cid,
        "title": m.get("question") or cid[:12],
        "slug": m.get("market_slug") or "",
        "daily": daily,
        "min_size": rw.get("min_size") or 50,
        "max_spread": rw.get("max_spread") or 3.5,
        "tick": m.get("minimum_tick_size") or 0.01,
    }


def launch(i: int, p: dict) -> None:
    RUN.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    port = BASE_PORT + i
    db = RUN / f"bot{i}.db"
    env = dict(os.environ)
    env.update({
        "MAKER_DB": str(db),
        "MAKER_MARKET": p["cid"],
        "MAKER_TITLE": p["title"],
        "MAKER_URL": f"https://polymarket.com/market/{p['slug']}" if p["slug"] else "",
        "MAKER_DAILY_RATE": str(p["daily"]),
        "MAKER_MIN_SIZE": str(p["min_size"]),
        "MAKER_MAX_SPREAD": str(p["max_spread"]),
        "MAKER_TICK": str(p["tick"]),
        # Each bot needs its own single-instance lock, or bot 2 exits on
        # finding bot 1's pidfile.
        "MAKER_PID": str(RUN / f"bot{i}.pid"),
    })
    bot_log = open(LOGS / f"bot{i}.out", "w")
    dash_log = open(LOGS / f"dash{i}.out", "w")
    b = subprocess.Popen([sys.executable, "-m", "strategy.main"],
                         cwd=str(ROOT), env=env, stdout=bot_log, stderr=bot_log)
    d = subprocess.Popen([sys.executable, "-m", "uvicorn", "server.dashboard:app",
                          "--host", "127.0.0.1", "--port", str(port)],
                         cwd=str(ROOT), env=env, stdout=dash_log, stderr=dash_log)
    (RUN / f"fleet{i}.pids").write_text(f"{b.pid}\n{d.pid}\n")
    print(f"  bot pid {b.pid} · dashboard http://127.0.0.1:{port}")


def stop() -> None:
    for f in RUN.glob("fleet*.pids"):
        for line in f.read_text().split():
            try:
                os.kill(int(line), signal.SIGTERM)
                print("  stopped", line)
            except Exception:
                pass
        f.unlink()


def main() -> None:
    if "--stop" in sys.argv:
        stop()
        return
    check_only = "--check" in sys.argv

    ok = []
    for i, cid in enumerate(MARKETS):
        print(f"\n[{i}] {cid[:14]}...")
        p = verify(cid)
        if not p:
            print("     REFUSED -- not launching a bot on a market that does not pay")
            continue
        print(f"     ${p['daily']}/day · min_size {p['min_size']} · "
              f"max_spread {p['max_spread']}c · tick {p['tick']}")
        print(f"     {p['title'][:70]}")
        ok.append((i, p))

    print(f"\n{len(ok)}/{len(MARKETS)} markets verified as paying.")
    if check_only or not ok:
        return

    stop()
    time.sleep(1)
    print("\nlaunching:")
    for i, p in ok:
        launch(i, p)
    print("\nall up. stop with:  python -m scripts.run_fleet --stop")


if __name__ == "__main__":
    main()
