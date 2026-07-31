#!/usr/bin/env python3
"""Container entrypoint: supervise the maker sim and serve its dashboard.

Preflight fails loudly on the two failures that otherwise stay silent while the
healthcheck goes green:
  1. a US region -- Binance returns 451, so the bot sees no market at all
  2. missing persistent storage -- the DB vanishes on every redeploy

Both have bitten this project before, which is why they are checked here rather
than trusted.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = int(os.environ.get("PORT", "8788"))
RESTART_BACKOFF = 5.0


def preflight() -> None:
    problems: list[str] = []

    # Opt-in destructive reset: when MAKER_DB_RESET=1, wipe the DB at the
    # mount path so a code change starts a clean sample (AGENTS.md: changing
    # strategy params invalidates the run). Only triggers on explicit opt-in,
    # never by default, so routine redeploys keep their data.
    if os.environ.get("MAKER_DB_RESET") == "1":
        db = os.environ.get("MAKER_DB", "")
        if db and Path(db).exists():
            Path(db).unlink()
            print(f"[preflight] MAKER_DB_RESET=1 -> wiped {db}", flush=True)

    db = os.environ.get("MAKER_DB")
    if not db:
        print("[preflight] WARNING: MAKER_DB unset -- using a container-local "
              "maker.db, which is LOST on redeploy unless a volume is mounted.",
              flush=True)
    else:
        try:
            from strategy import store
            with store.db() as c:
                c.execute("CREATE TABLE IF NOT EXISTS _pf (x INTEGER)")
                c.execute("DELETE FROM _pf")
            print(f"[preflight] volume OK: {db} is writable", flush=True)
        except Exception as e:
            problems.append(
                f"MAKER_DB={db} is not writable ({e}). Mount a Railway volume "
                f"at that directory (Settings -> Volumes -> /data)."
            )

    import requests
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price",
                         params={"symbol": "BTCUSDT"}, timeout=15)
        if r.status_code == 200:
            print(f"[preflight] binance OK: BTC={r.json().get('price')}", flush=True)
        else:
            problems.append(
                f"Binance returned HTTP {r.status_code} (451/403 = geo-blocked "
                f"region, e.g. US). Redeploy in a non-US region."
            )
    except Exception as e:
        problems.append(f"Binance unreachable: {e}")

    try:
        from strategy.markets import fetch_live_market
        from strategy.net_config import load_net
        n = load_net()
        m = fetch_live_market(n.gamma_host, n.series_slug)
        print(f"[preflight] polymarket OK: live market={m.market_slug if m else None}",
              flush=True)
    except Exception as e:
        problems.append(f"Polymarket unreachable: {type(e).__name__}: {e}")

    if problems:
        print("\n[preflight] FAILED:", flush=True)
        for p in problems:
            print(f"  - {p}", flush=True)
        if "--preflight" not in sys.argv:
            # The host restarts on failure; without this the container respawns
            # ~1/sec and hammers the upstreams with a probe burst each time.
            print("[preflight] sleeping 30s before exit to avoid a hot restart loop",
                  flush=True)
            time.sleep(30)
        sys.exit(1)
    print("[preflight] all checks passed", flush=True)


def run_bot() -> None:
    """Run the maker sim, restarting it if it exits."""
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    while True:
        print("[bot] starting (maker sim)", flush=True)
        proc = subprocess.Popen([sys.executable, "-m", "strategy.main"],
                                cwd=str(ROOT), env=env)
        code = proc.wait()
        print(f"[bot] exited code={code}; restarting in {RESTART_BACKOFF}s", flush=True)
        time.sleep(RESTART_BACKOFF)


def main() -> None:
    if "--preflight" in sys.argv:
        preflight()
        return
    preflight()
    threading.Thread(target=run_bot, name="bot", daemon=True).start()

    import uvicorn
    print(f"[dashboard] serving on 0.0.0.0:{PORT}", flush=True)
    uvicorn.run("server.dashboard:app", host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
