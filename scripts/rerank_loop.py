"""Re-run the ranker on a fixed interval, forever.

`strategy.fleet` adopts run/markets.json every `rerank_interval_sec`, but
nothing regenerates that file -- and the U6 universe is short-dated by
construction, so every market in it resolves within a day. Left alone, the
fleet re-reads the same file until its whole universe has settled and then
quotes nothing while looking perfectly healthy.

A separate process rather than a thread inside the fleet: re-ranking scores
hundreds of books over the network, and a stall there must not stall the
trading loop. That is the same argument `fleet.py` already makes for keeping
the scoring out of the sweep.

Failures are logged and slept through, never raised. A ranker that fails at
03:00 because the venue returned a 502 must not leave the fleet with no
refresh for the rest of the night.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "rerank.log"

INTERVAL_SEC = 3600.0
TOP = 20


def main() -> None:
    LOG.parent.mkdir(exist_ok=True)
    while True:
        time.sleep(INTERVAL_SEC)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            r = subprocess.run(
                [sys.executable, "-m", "scripts.rank_markets", "--top", str(TOP)],
                cwd=str(ROOT), capture_output=True, text=True, timeout=600)
            out = r.stdout or ""
            err = "" if r.returncode == 0 else f"\nEXIT {r.returncode}\n{r.stderr}"
        except Exception as e:                      # network, timeout, anything
            out, err = "", f"\nFAILED: {type(e).__name__}: {e}"
        with LOG.open("a", encoding="utf-8", errors="replace") as f:
            f.write(f"\n===== {stamp} =====\n{out}{err}")


if __name__ == "__main__":
    main()
