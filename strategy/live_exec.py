"""LIVE order execution against Polymarket CLOB. Real money.

Everything else in this repo is a simulator. This file is the one place that
can lose actual funds, so it is deliberately small, deliberately manual, and
refuses to do anything by default.

CREDENTIALS NEVER APPEAR HERE. They are read from the environment and handed
straight to the client. Nothing in this module prints, logs, or writes a key,
and nothing that does should ever be added to it.

    # in .env, which must be in .gitignore BEFORE the key goes in
    POLY_PRIVATE_KEY=0x...      # signing key
    POLY_FUNDER=0x...           # address actually holding the USDC
    POLY_SIG_TYPE=1             # 0 EOA | 1 email-magic proxy | 2 browser proxy

    python -m strategy.live_exec status
    python -m strategy.live_exec quote <condition_id> --price 0.22 --size 20
    python -m strategy.live_exec quote <condition_id> --price 0.22 --size 20 --live
    python -m strategy.live_exec cancel-all --live

SAFETY RAILS, all on by default:
  * --live is required for anything that reaches the venue. Without it every
    command prints what it WOULD send and exits.
  * MAX_ORDER_USD caps one order; MAX_TOTAL_USD caps everything open at once.
  * Each leg is written to run/live_orders.json as it is sent, so a crash
    mid-flight still leaves a record of what went out.
  * cancel-all is its own command, because the thing you want at 3am is a way
    to pull every quote without reading code first.
  * Nothing here is imported by fleet.py. The automated bot cannot reach this
    module, so it cannot place a real order by accident.

SIGNATURE TYPE IS THE USUAL FOOTGUN. An account funded through the Polymarket
website is a PROXY: signature_type 1 or 2, with POLY_FUNDER set to the proxy
address rather than the address the private key derives to. Get it wrong and
orders are rejected -- or signed against an account with no balance. Run
`status` first and confirm the address it prints is the one holding your money.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "run"

# Hard ceilings. Not configuration -- this is the difference between a POC and
# an unbounded loss, so they live in code where a stray env var cannot raise
# them. Edit deliberately, never to "just get this order through".
MAX_ORDER_USD = 25.0
MAX_TOTAL_USD = 100.0


def _client():
    """Build a CLOB client from the environment. Raises if anything is absent.

    The key is read and passed on in a single expression: never bound to a
    module global, never returned, never in a log line.
    """
    from py_clob_client.client import ClobClient

    key = os.environ.get("POLY_PRIVATE_KEY")
    if not key:
        raise SystemExit(
            "POLY_PRIVATE_KEY not set. Put it in .env -- and confirm .env is "
            "in .gitignore before you paste anything into it.")
    funder = os.environ.get("POLY_FUNDER")
    sig_type = int(os.environ.get("POLY_SIG_TYPE", "1"))
    host = os.environ.get("CLOB_HOST", "https://clob.polymarket.com")

    c = ClobClient(host, key=key, chain_id=137,
                   signature_type=sig_type, funder=funder)
    # L2 API creds are derived from the key by the client; we never store them.
    c.set_api_creds(c.create_or_derive_api_creds())
    return c


def _log_order(rec: dict) -> None:
    RUN.mkdir(exist_ok=True)
    f = RUN / "live_orders.json"
    hist = []
    if f.exists():
        try:
            hist = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            hist = []
    hist.append(rec)
    f.write_text(json.dumps(hist, indent=2), encoding="utf-8")


def _open_notional(c) -> float:
    try:
        return sum(float(o.get("price", 0) or 0)
                   * float(o.get("original_size", 0) or 0)
                   for o in (c.get_orders() or []))
    except Exception:
        return 0.0


def status() -> None:
    """Who are we, and what is already resting. Read-only, safe anytime."""
    c = _client()
    print(f"address        {c.get_address()}")
    print(f"funder         {os.environ.get('POLY_FUNDER') or '(same as address)'}")
    print(f"signature type {os.environ.get('POLY_SIG_TYPE', '1')}")
    try:
        orders = c.get_orders() or []
        print(f"open orders    {len(orders)} "
              f"(${_open_notional(c):.2f} notional)")
        for o in orders[:10]:
            print(f"  {str(o.get('side')):4} {o.get('original_size')} @ "
                  f"{o.get('price')}  id={str(o.get('id'))[:16]}")
    except Exception as e:
        print(f"open orders    ERROR {type(e).__name__}: {e}")
    print("\nConfirm the address above is the account holding your USDC "
          "BEFORE sending anything.")


def quote(condition_id: str, price: float, size: float, live: bool) -> None:
    """Rest a two-sided pair: buy UP at `price`, buy DOWN at 1-price.

    Two-sided on purpose. A single leg is a naked directional bet -- exactly
    the failure the simulator spent all day demonstrating, where 16 markets
    accumulated $1,630 of unhedged exposure against a $62 edge.
    """
    from py_clob_client.clob_types import OrderArgs
    from py_clob_client.order_builder.constants import BUY

    from strategy.markets import fetch_pinned_market

    m = fetch_pinned_market(condition_id)
    if m is None:
        raise SystemExit(f"market {condition_id[:12]} not found")

    dn_price = round(1.0 - price, 4)
    cost = price * size + dn_price * size
    if cost > MAX_ORDER_USD:
        raise SystemExit(
            f"${cost:.2f} exceeds MAX_ORDER_USD ${MAX_ORDER_USD:.2f}")

    legs = [(m.up_token, price, "UP"), (m.down_token, dn_price, "DOWN")]
    print(f"market   {m.market_slug[:60]}")
    print(f"tick     {m.tick_size}   neg_risk {m.neg_risk}")
    for tok, p, label in legs:
        print(f"  BUY {size:.0f} {label:4} @ {p:.3f} = ${p * size:6.2f}  "
              f"token {str(tok)[:14]}...")
    print(f"total committed ${cost:.2f}")

    if not live:
        print("\nDRY RUN -- nothing sent. Re-run with --live to place.")
        return

    c = _client()
    already = _open_notional(c)
    if already + cost > MAX_TOTAL_USD:
        raise SystemExit(f"open ${already:.2f} + ${cost:.2f} exceeds "
                         f"MAX_TOTAL_USD ${MAX_TOTAL_USD:.2f}")

    for tok, p, label in legs:
        signed = c.create_order(
            OrderArgs(price=p, size=size, side=BUY, token_id=tok))
        resp = c.post_order(signed)
        _log_order({"ts": time.time(), "condition_id": condition_id,
                    "side": label, "token_id": str(tok), "price": p,
                    "size": size, "response": str(resp)[:400]})
        print(f"  SENT {label}: {resp}")
    print(f"\nlogged to {RUN / 'live_orders.json'}")


def cancel_all(live: bool) -> None:
    if not live:
        print("DRY RUN -- would cancel ALL open orders. Re-run with --live.")
        return
    print(_client().cancel_all())


def main() -> None:
    ap = argparse.ArgumentParser(description="LIVE Polymarket execution.")
    ap.add_argument("--live", action="store_true",
                    help="actually send. Without it, everything is a dry run.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    q = sub.add_parser("quote")
    q.add_argument("condition_id")
    q.add_argument("--price", type=float, required=True)
    q.add_argument("--size", type=float, required=True)
    sub.add_parser("cancel-all")
    a = ap.parse_args()

    if a.cmd == "status":
        status()
    elif a.cmd == "quote":
        quote(a.condition_id, a.price, a.size, a.live)
    else:
        cancel_all(a.live)


if __name__ == "__main__":
    main()
