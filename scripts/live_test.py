"""Pre-register a live-test prediction, then reconcile it against reality.

The one thing the simulator has never verified is that rent actually arrives.
Every income figure on the dashboard is our own arithmetic on published rates
multiplied by a share we computed ourselves. No payout has ever landed.

This turns one small real order into an experiment rather than an anecdote.
The prediction is written to disk BEFORE the payout, together with the book
state it was derived from, so it cannot be quietly adjusted afterwards to match
whatever happened. That is the entire point: a forecast you can edit after the
result is not evidence.

    python -m scripts.live_test predict <slug> --price 0.50 --size 20
    python -m scripts.live_test reconcile <slug> --received 0.83 --hours 18

It places no orders and holds no keys. Placing the order is a human step, done
on Polymarket's own interface.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy import rewards                          # noqa: E402
from strategy.main import full_book                   # noqa: E402
from strategy.markets import fetch_pinned_market      # noqa: E402
from strategy.net_config import load_net as load_bot_cfg  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "run"


def _spec(slug: str) -> dict:
    """Find the market in the fleet's own list, so the test inherits exactly
    the daily rate, min_size and max_spread the simulator has been using."""
    markets = json.loads((RUN / "markets.json").read_text(encoding="utf-8"))
    for m in markets:
        if slug == m.get("slug") or slug in (m.get("slug") or ""):
            return m
    raise SystemExit(f"slug {slug!r} not found in run/markets.json")


def predict(slug: str, price: float, size: float,
            dn_price: float | None = None) -> None:
    spec = _spec(slug)
    bot = load_bot_cfg()
    m = fetch_pinned_market(spec["cid"])
    if m is None:
        raise SystemExit(f"could not fetch market {spec['cid'][:12]}")
    up = full_book(bot.clob_host, m.up_token)
    dn = full_book(bot.clob_host, m.down_token)

    # 1-price is wrong on most real books: for a binary
    # dn_ask == 1 - up_bid, so quoting DOWN at exactly 1-price crosses.
    if dn_price is None:
        dn_price = round(1.0 - price, 4)

    max_spread = spec["max_spread"] / 100.0
    min_size = float(spec["min_size"])

    # Competitors' qualifying score, from the same function the simulator
    # scores itself with. A different formula here would make the comparison
    # meaningless.
    bq1, bq2 = rewards.book_scores(up, dn, max_spread, min_size)
    theirs = rewards.q_min(bq1, bq2)

    mid_up = ((up.get("best_bid") or 0) + (up.get("best_ask") or 0)) / 2
    mid_dn = ((dn.get("best_bid") or 0) + (dn.get("best_ask") or 0)) / 2
    # Our score if we rest `size` at `price` on both sides. Modelled, not
    # placed: this is the number being put on trial.
    q_up = rewards.order_score(max_spread, abs(mid_up - price), size, min_size)
    q_dn = rewards.order_score(max_spread, abs(mid_dn - dn_price),
                               size, min_size)
    ours = rewards.q_min(q_up, q_dn)

    # Safety readout. Bidding ABOVE the book's best bid makes us the most
    # exposed order in the book -- first to be hit and marked down the moment
    # we are. A reward window that sits empty is usually empty for that reason,
    # not because free money was left lying there.
    up_bid, dn_bid = up.get("best_bid"), dn.get("best_bid")
    warn = []
    if price + dn_price >= 1.0:
        warn.append(f"pair costs ${price + dn_price:.3f} >= $1.00 -- a filled "
                    f"pair LOSES on a payout that is exactly $1.00")
    if up_bid and price > up_bid:
        warn.append(f"UP {price:.3f} is above best bid {up_bid:.3f} -- most "
                    f"exposed order in the book")
    if dn_bid and dn_price > dn_bid:
        warn.append(f"DOWN {dn_price:.3f} is above best bid {dn_bid:.3f} -- "
                    f"most exposed order in the book")
    if theirs <= 0:
        warn.append("NO competition inside the reward band. A 100% share is "
                    "payment for risk nobody else will take, not free rent")

    share = ours / (ours + theirs) if (ours + theirs) > 0 else 0.0
    predicted = share * spec["daily"]

    out = {
        "ts": time.time(),
        "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "slug": spec.get("slug"),
        "condition_id": spec["cid"],
        "title": spec["title"],
        "order": {
            "price_up": price,
            "price_down": dn_price,
            "size_each_side": size,
            "capital_committed": round(price * size + dn_price * size, 2),
            "pair_cost": round(price + dn_price, 4),
        },
        "book_at_prediction": {
            "up_bid": up.get("best_bid"), "up_ask": up.get("best_ask"),
            "dn_bid": dn.get("best_bid"), "dn_ask": dn.get("best_ask"),
            "mid_up": mid_up, "mid_down": mid_dn,
        },
        "model": {
            "daily_pot_usd": spec["daily"],
            "their_score": theirs,
            "our_score": ours,
            "our_share": share,
            "predicted_usd_per_day": predicted,
        },
        "payout_expected_at": "00:00 UTC, once, in USDC",
        "assumptions_that_will_probably_break": [
            "Competing score stays as observed. It will not -- other makers "
            "react to new size, and our share is the figure most likely to "
            "shrink on contact.",
            "The order rests inside the reward band the whole time. Any "
            "minute outside it earns zero for that minute.",
            "Rent is sampled per minute, so a partial day earns pro rata; "
            "pass --hours when reconciling or the comparison is unfair.",
        ],
    }
    f = RUN / f"live_test_{spec['cid'][:10]}.json"
    f.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print(f"market      {spec['title'][:56]}")
    print(f"pot         ${spec['daily']:.0f}/day")
    print(f"order       {size:.0f} shares @ {price:.3f} UP "
          f"+ {size:.0f} @ {dn_price:.3f} DOWN")
    print(f"capital     ${out['order']['capital_committed']:.2f}"
          f"   pair cost ${price + dn_price:.3f}")
    for w in warn:
        print(f"  WARNING: {w}")
    print(f"their score {theirs:.1f}   our score {ours:.1f}")
    print(f"our share   {100 * share:.2f}%")
    print(f"PREDICTED   ${predicted:.4f} per full day")
    print(f"\nwrote {f}")
    print("Place the order yourself on Polymarket, then run reconcile.")


def reconcile(slug: str, received: float, hours: float) -> None:
    spec = _spec(slug)
    f = RUN / f"live_test_{spec['cid'][:10]}.json"
    if not f.exists():
        raise SystemExit(f"no prediction at {f} -- run predict first")
    p = json.loads(f.read_text(encoding="utf-8"))

    full_day = p["model"]["predicted_usd_per_day"]
    # Pro rata: comparing a 6-hour test against a 24-hour forecast would
    # manufacture a 4x "miss" that says nothing about the model.
    expected = full_day * min(hours / 24.0, 1.0)
    err = received - expected
    ratio = (received / expected) if expected else None

    verdict = ("NO PAYOUT" if received == 0 else
               "MODEL HOLDS" if ratio and 0.5 <= ratio <= 2.0 else
               "MODEL WRONG" if ratio else "NO PREDICTION")

    print(f"market            {p['title'][:50]}")
    print(f"predicted /day    ${full_day:.4f}")
    print(f"rested {hours:.1f}h -> expected ${expected:.4f}")
    print(f"actually received ${received:.4f}")
    print(f"error             ${err:+.4f}"
          + (f"   ({ratio:.2f}x predicted)" if ratio else ""))
    print(f"VERDICT           {verdict}")
    if received == 0:
        print("  Zero is the most informative result available: the rent "
              "thesis is wrong, not merely mis-sized. Nothing else in the "
              "strategy pays enough to matter without it.")
    elif ratio and ratio < 0.5:
        print("  Overestimated. Most likely competitors added size once ours "
              "appeared, or the order spent time outside the reward band.")
    elif ratio and ratio > 2.0:
        print("  Underestimated -- check the pot figure and whether another "
              "maker withdrew.")

    p["result"] = {
        "received_usd": received, "hours_rested": hours,
        "expected_usd": expected, "error_usd": err, "ratio": ratio,
        "verdict": verdict,
        "reconciled_utc": datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
    }
    f.write_text(json.dumps(p, indent=2), encoding="utf-8")
    print(f"\nupdated {f}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Pre-register and reconcile a live rent test.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("predict", help="write the prediction BEFORE ordering")
    a.add_argument("slug")
    a.add_argument("--price", type=float, required=True,
                   help="UP price; DOWN is quoted at 1-price")
    a.add_argument("--dn-price", dest="dn_price", type=float,
                   default=None, help="DOWN price; default 1-price, usually wrong")
    a.add_argument("--size", type=float, required=True,
                   help="shares per side; must be >= the market min_size")
    b = sub.add_parser("reconcile", help="compare against what actually landed")
    b.add_argument("slug")
    b.add_argument("--received", type=float, required=True,
                   help="USDC actually paid, 0 if nothing arrived")
    b.add_argument("--hours", type=float, default=24.0,
                   help="hours the order actually rested")
    args = ap.parse_args()
    if args.cmd == "predict":
        predict(args.slug, args.price, args.size, args.dn_price)
    else:
        reconcile(args.slug, args.received, args.hours)


if __name__ == "__main__":
    main()
