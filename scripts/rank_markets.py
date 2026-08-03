"""Rank funded reward markets by RETURN, and write the winners to run/markets.json.

Headline daily rate is the wrong sort key. A $300/day market with thousands of
shares already resting inside the reward window pays less than a $50/day market
with a thin book, because the pool splits by score share:

    income = daily_rate * ours / (ours + theirs)

So the metric is income per dollar of capital committed, computed against the
live book. Measured across 211 funded markets, the spread between best and
worst on that metric is roughly 7x at identical risk.

    python -m scripts.rank_markets            # top 20
    python -m scripts.rank_markets --top 40
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategy.allocate import spread_capture_daily   # noqa: E402
from strategy.config import load as _load_cfg   # noqa: E402

RUN = ROOT / "run"
OFFSET = 0.020          # where we intend to quote, in price units
C = 3.0                 # venue's one-sided penalty

# Polymarket: "The minimum reward payout is $1; amounts below this will not be
# paid." A market projecting under a dollar a day does not pay a fraction of a
# dollar, it pays nothing -- so a sub-floor market is not a small position, it
# is capital committed for zero income. Measured 2026-07-30, 16 of 20 fleet
# markets were in exactly that state.
MIN_PAYOUT = 1.0
FLOOR_MULTIPLE = 1.5    # headroom: projections are noisy and rivals arrive

# TRADABILITY AND HORIZON (U6). Sourced from config so the ranker and the
# fleet cannot drift, exactly as the payout floor is.
_CFG = _load_cfg()
MIN_VOLUME_24H = _CFG.select_min_volume_24h_usd
MAX_DAYS_TO_RESOLVE = _CFG.select_max_days_to_resolve

GAMMA = "https://gamma-api.polymarket.com/markets"


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, ValueError):
        return False


def q_min(a: float, b: float) -> float:
    return max(min(a, b), max(a / C, b / C))


def days_to_resolve(end_iso: Optional[str],
                    now_iso: Optional[str] = None) -> Optional[float]:
    """Days from now until the venue's stated end date, or None if unstated.

    `now_iso` exists so the horizon arithmetic is testable without freezing
    the clock. Negative means the end date has already passed.
    """
    if not end_iso:
        return None
    try:
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    now = (datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
           if now_iso else datetime.now(timezone.utc))
    # An unqualified venue timestamp carries no offset, and subtracting an
    # aware datetime from a naive one raises TypeError -- which the
    # `except ValueError` above does not catch. This runs inside a
    # ThreadPoolExecutor worker, so a single such endDate aborted the entire
    # ranking run. Venue times are UTC by convention; assuming that keeps the
    # arithmetic aware-vs-aware instead of raising.
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (end - now).total_seconds() / 86400.0


def tradable(volume_24h: Optional[float],
             days: Optional[float]) -> tuple[bool, str]:
    """Can this market produce the two observations the run needs?

    A fill needs someone to trade at our price; a settled P&L needs the market
    to resolve inside the run. Reward yield answers neither question, and
    ranking on it alone chose 20 markets that between them printed 48 trades in
    11.6 hours and resolved no sooner than September 2026.

    Unknown is refused on both axes rather than assumed favourable. The
    universe that produced zero fills and zero resolutions was long-dated and
    thin, so a missing field is far more likely to be another one of those than
    a liquid market with a gap in its metadata.
    """
    if volume_24h is None:
        return False, "volume unknown"
    if volume_24h < MIN_VOLUME_24H:
        return False, f"24h volume ${volume_24h:,.0f} < ${MIN_VOLUME_24H:,.0f}"
    if days is None:
        return False, "horizon unknown"
    if days < 0:
        return False, "horizon passed"
    if days > MAX_DAYS_TO_RESOLVE:
        return False, f"horizon {days:.1f}d > {MAX_DAYS_TO_RESOLVE:.0f}d"
    return True, ""


def gamma_volume(session: requests.Session,
                 cids: list[str]) -> dict[str, float]:
    """24h traded volume per condition_id, from gamma.

    The CLOB's market payload carries no volume at all, which is why the
    ranker never had this filter: the number it needed was on a different
    host. Queried in chunks because the endpoint takes repeated
    `condition_ids` parameters and the candidate list is a few hundred long.
    """
    out: dict[str, float] = {}
    for i in range(0, len(cids), 20):
        chunk = cids[i:i + 20]
        try:
            rows = session.get(GAMMA, params={"condition_ids": chunk,
                                              "limit": len(chunk)},
                               timeout=20).json()
        except Exception:
            continue
        if isinstance(rows, dict):
            rows = rows.get("data") or []
        for r in rows:
            cid = r.get("conditionId")
            if cid:
                out[cid] = float(r.get("volume24hr") or 0.0)
    return out


def gamma_spread_universe(session: requests.Session,
                          pages: int = 2, per_page: int = 100) -> list[dict]:
    """Liquid short-dated markets that pay NO rewards, shaped like CLOB rows.

    `/sampling-markets` lists reward-funded markets and nothing else, so the
    ranker structurally could not see the markets that actually trade. The
    entire 2026-07-31 universe came from there: 20 markets, 48 tape prints in
    11.6 hours, nine of them never traded at all, and every `tape_json`
    recorded in run/fleet.db is `{}`.

    Gamma sorts by 24h volume and carries the book summary (`spread`,
    `bestBid`, `bestAsk`) inline, so the expensive part -- one CLOB round trip
    per market -- happens only for candidates that already clear volume and
    horizon.

    Reward-funded markets are excluded here rather than merged: they are
    already sourced, priced and floored by the reward path, and a market
    scored twice would compete against itself in the water-fill.

    Returned rows use CLOB field names (`condition_id`, `tokens`, `rewards`)
    because `evaluate` reads them, plus the two gamma-only figures the spread
    pot needs.
    """
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    # ADVANCE BY WHAT THE ENDPOINT ACTUALLY RETURNED, NOT BY WHAT WE ASKED FOR.
    #
    # Gamma caps a page at 100 rows and ignores a larger `limit` -- measured
    # 2026-08-02: limit=100, 250 and 500 all return exactly 100. Stepping the
    # offset by the REQUESTED size therefore jumped a gap: at per_page=250,
    # page 1 started at offset 250 while the response had ended at 99, so rows
    # 100-249 were never fetched. They exist; offset=100 returns a full page.
    #
    # Unreachable today only because the volume floor stops the scan inside the
    # first page -- which is luck, not a design. Tracking the real cursor makes
    # it correct whatever the cap turns out to be.
    offset = 0
    page_cap: int | None = None
    for _ in range(pages):
        params = {
            "closed": "false", "active": "true", "archived": "false",
            "order": "volume24hr", "ascending": "false",
            "limit": per_page, "offset": offset,
            "end_date_min": now.isoformat(),
            "end_date_max": (now + timedelta(days=MAX_DAYS_TO_RESOLVE)).isoformat(),
        }
        try:
            rows = session.get(GAMMA, params=params, timeout=30).json()
        except Exception:
            break
        if isinstance(rows, dict):
            rows = rows.get("data") or []
        if not rows:
            break
        offset += len(rows)
        for m in rows:
            vol = float(m.get("volume24hr") or 0.0)
            if vol < MIN_VOLUME_24H:
                # Sorted by volume, so the first market under the floor ends
                # the useful part of the listing. Verified against the live
                # endpoint 2026-08-02 with the date filters applied: 100 rows,
                # zero inversions, and the first row under the floor had no
                # qualifying market after it. The `order=volume24hr&
                # ascending=false` sort survives `end_date_min`/`end_date_max`.
                return out
            if not m.get("enableOrderBook") or not m.get("acceptingOrders"):
                continue
            # Reward-funded markets belong to the other path.
            if m.get("clobRewards"):
                continue
            try:
                toks = json.loads(m.get("clobTokenIds") or "[]")
            except (TypeError, ValueError):
                continue
            if len(toks) != 2:
                continue
            spread = float(m.get("spread") or 0.0)
            if spread <= 0:
                continue
            out.append({
                "condition_id": m.get("conditionId"),
                "question": m.get("question") or "",
                "market_slug": m.get("slug") or "",
                "tokens": [{"token_id": str(t)} for t in toks],
                # No reward config exists on these markets. The scan below
                # still needs a window and a scoring minimum to measure
                # competing depth with, so the venue's usual defaults stand in
                # -- they set the units of `theirs`, and reallocate() reads the
                # same units back out. They are NOT a claim that this market
                # pays rewards; `daily` stays 0 and `source` says spread.
                "rewards": {"max_spread": float(m.get("rewardsMaxSpread") or 3.5),
                            "min_size": float(m.get("rewardsMinSize") or 50)},
                "minimum_tick_size": float(m.get("orderPriceMinTickSize") or 0.01),
                "end_date_iso": m.get("endDate"),
                # Quoting minimum, which is the venue's order minimum here --
                # there is no reward score to qualify for, so rewardsMinSize
                # would only inflate the lot the allocator has to buy.
                "_order_min": float(m.get("orderMinSize") or 5),
                "_volume_24h": vol,
                "_spread": spread,
            })
        # A SHORT PAGE MEANS THE LISTING ENDED -- measured against what this
        # endpoint actually serves, not what we asked for.
        #
        # This compared against `per_page`, and Gamma caps a page at 100 however
        # large a limit is requested. At the old per_page=250 every response was
        # "short", so the loop broke after the first page every time: `pages=2`
        # was never honoured and the scan never saw past the first 100 markets.
        # The first response establishes the real page size.
        if page_cap is None:
            page_cap = len(rows)
        if len(rows) < page_cap:
            break
    return out


def order_score(v: float, s: float, size: float, min_size: float) -> float:
    if s < 0 or s > v or size < min_size:
        return 0.0
    return ((v - s) / v) ** 2 * size


def evaluate(session: requests.Session, rate: float, m: dict,
             volume_24h: Optional[float] = None,
             source: str = "rewards") -> dict | None:
    """Income and capital for one market, from its live book.

    `rate` is the market's pot in $/day, and `source` says what pays it. For a
    reward market that is the venue's emission and the $1.50 minimum payout
    applies; for a spread market it is `spread_capture_daily`, paid by the
    taker on the trade, and no minimum distribution exists to apply.
    """
    rw = m.get("rewards") or {}
    v = (rw.get("max_spread") or 3.5) / 100.0
    min_size = rw.get("min_size") or 50
    toks = [t.get("token_id") for t in (m.get("tokens") or [])]
    if len(toks) != 2 or OFFSET >= v:
        return None

    q1 = q2 = 0.0
    capital_per_share = 0.0
    mids: dict[int, float] = {}
    best_bids: dict[int, float] = {}
    for j, tok in enumerate(toks):
        try:
            b = session.get("https://clob.polymarket.com/book",
                            params={"token_id": tok}, timeout=12).json()
        except Exception:
            return None
        bids = [(float(x["price"]), float(x["size"])) for x in (b.get("bids") or [])]
        asks = [(float(x["price"]), float(x["size"])) for x in (b.get("asks") or [])]
        if not bids or not asks:
            return None
        mid = (max(bids)[0] + min(asks)[0]) / 2.0
        # Outside [0.05, 0.95] the book is one-sided in practice and the
        # position is mostly a bet on a near-settled outcome.
        if not 0.05 < mid < 0.95:
            return None
        mids[j] = mid
        best_bids[j] = max(bids)[0]
        capital_per_share += mid
        for levels, sign, is_bid in ((bids, 1.0, True), (asks, -1.0, False)):
            for p, s in levels:
                d = (mid - p) * sign
                if 0 <= d <= v and s >= min_size:
                    sc = ((v - d) / v) ** 2 * s
                    if is_bid == (j == 0):
                        q1 += sc
                    else:
                        q2 += sc

    theirs = q_min(q1, q2)
    n = max(min_size, 120)

    # Score the price we would ACTUALLY quote, not the price we would like to.
    # The bot never bids more than one tick above the best bid (see
    # quotes._decide_quotes_rewards): on a wide book, mid-minus-offset sits
    # deep inside the spread, and a market whose reward window is empty is
    # usually empty because quoting there means being the most exposed order in
    # the book. Ranking on the uncapped price overstated those markets badly --
    # one showed 15%/day for a quote six cents above the next best bid.
    tick = m.get("minimum_tick_size") or 0.01
    sides = []
    for j in range(2):
        mid = mids[j]
        want = mid - OFFSET
        price = min(want, best_bids[j] + tick)
        s = mid - price
        sides.append(order_score(v, s, n, min_size))
    ours = q_min(sides[0], sides[1])
    if ours <= 0:
        return None            # cannot score here without overbidding the book
    income = rate * ours / (ours + theirs)
    capital = n * capital_per_share

    # U6. A market must be able to produce the observations before its yield
    # is worth comparing. Both reasons are recorded rather than dropped, so
    # the report can show that a universe was refused for being untradeable
    # rather than for being unprofitable -- the distinction the last six runs
    # could not make.
    days = days_to_resolve(m.get("end_date_iso"))
    can_trade, why = tradable(volume_24h, days)
    # The payout floor is a REWARD rule -- the venue's minimum distribution.
    # A spread market is paid by whoever lifts the offer, in the amount of the
    # spread, so there is no distribution to be under. Holding it to the floor
    # would reject exactly the liquid markets this path exists to admit.
    pays = income >= MIN_PAYOUT * FLOOR_MULTIPLE if source == "rewards" else income > 0
    if not why and not pays:
        why = (f"income ${income:.2f}/day under payout floor"
               if source == "rewards" else "no spread income")

    return {
        "source": source,
        "spread": round(float(m.get("_spread") or 0.0), 4) or None,
        # Below the payout floor this market pays exactly zero, however good
        # its return_pct_day looks. Recorded rather than filtered here so the
        # report can show what was rejected and why.
        "eligible": pays and can_trade,
        "reject_reason": why,
        "volume_24h": round(volume_24h, 2) if volume_24h is not None else None,
        "days_to_resolve": round(days, 2) if days is not None else None,
        "cid": m["condition_id"],
        "title": m.get("question", "")[:90],
        "slug": m.get("market_slug", ""),
        # THE REWARD POT, and zero is the honest figure for a market that pays
        # none. `fleet.reallocate` keys the spread path off `daily <= 0` and
        # recomputes the pot from `volume_24h` and `spread`, so the capture
        # assumption stays in config where it can be revised without
        # re-ranking. `est_income` below reports what that pot projects.
        "daily": rate if source == "rewards" else 0.0,
        # Reward markets must quote at least rewardsMinSize or they score
        # nothing; a spread market only has to clear the venue's order
        # minimum, and using the larger figure would force the allocator to
        # buy a lot several times bigger than the market needs.
        "min_size": min_size if source == "rewards" else m.get("_order_min", 5),
        "max_spread": rw.get("max_spread") or 3.5,
        "tick": m.get("minimum_tick_size") or 0.01,
        "shares": n,
        "est_income": round(income, 3),
        "est_capital": round(capital, 2),
        "return_pct_day": round(100 * income / capital, 3) if capital else 0,
        "their_score": round(theirs, 1),
    }


def _positive_int(v: str) -> int:
    n = int(v)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse the command line BEFORE any network work happens.

    This used to be a hand-rolled scan of sys.argv for "--top", which meant
    `--help` was not a flag the script recognised -- it was silently ignored,
    and asking for usage instead ran the whole pipeline: a few hundred requests
    against the venue, ending in an overwrite of run/markets.json. A mistyped
    flag did the same, and `--top` with no value raised IndexError. argparse
    turns all three into a usage error, and exits before the first request.
    """
    p = argparse.ArgumentParser(
        prog="python -m scripts.rank_markets",
        description=__doc__.split("\n\n")[0],
        epilog="Writes the winners to run/markets.json, which strategy.fleet "
               "reads as its market universe.")
    p.add_argument("--top", type=_positive_int, default=20, metavar="N",
                   help="how many markets to write (default: 20)")
    p.add_argument("--dry-run", action="store_true",
                   help="score and print the ranking, but leave "
                        "run/markets.json untouched")
    return p.parse_args(argv)


def main() -> None:
    args = parse_args()
    top = args.top

    s = requests.Session()
    data = s.get("https://clob.polymarket.com/sampling-markets", timeout=30).json()
    cands = []
    for m in data.get("data") or []:
        if not m.get("accepting_orders") or m.get("closed"):
            continue
        rate = sum(x.get("rewards_daily_rate", 0) or 0
                   for x in ((m.get("rewards") or {}).get("rates") or []))
        if rate > 0:
            cands.append((rate, m))
    cands.sort(key=lambda x: -x[0])
    print(f"funded live markets: {len(cands)}  (scoring top 250 by rate)")

    # Volume lives on gamma, the book lives on the CLOB. Fetched up front for
    # the whole candidate list so the per-market workers stay one round trip
    # each, as they were before the filter existed.
    short = [(rate, m) for rate, m in cands[:250]
             if (days_to_resolve(m.get("end_date_iso")) or -1) >= 0]
    vols = gamma_volume(s, [m["condition_id"] for _, m in short])
    print(f"volume read for {len(vols)}/{len(short)} unexpired candidates")

    # THE SECOND UNIVERSE. Reward-funded markets are chosen for paying rent,
    # and rent is paid on resting size whether or not anyone trades -- which is
    # why the reward-only universe could run 74 hours and produce 9 tape-backed
    # fills. Markets that pay no rewards at all are sourced here, on volume,
    # and priced on the spread they pay instead.
    spread_cands = gamma_spread_universe(s)
    print(f"unfunded liquid markets: {len(spread_cands)} "
          f"(>= ${MIN_VOLUME_24H:,.0f}/24h, <= {MAX_DAYS_TO_RESOLVE:.0f}d)")

    jobs = [(rate, m, vols.get(m["condition_id"]), "rewards")
            for rate, m in short]
    jobs += [(spread_capture_daily(m["_volume_24h"], m["_spread"],
                                   _CFG.spread_capture_frac),
              m, m["_volume_24h"], "spread")
             for m in spread_cands]

    out = []
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        for r in ex.map(lambda a: evaluate(s, a[0], a[1], a[2], source=a[3]),
                        jobs):
            if r:
                out.append(r)
    # Eligibility BEFORE ranking. Sorting on return_pct_day alone put the
    # top-ranked market at $0.25/day actual against $18.96 projected, because a
    # spectacular percentage return on an income of eleven cents is still
    # eleven cents -- and under the payout floor it is zero.
    eligible = [r for r in out if r["eligible"]]
    rejected = len(out) - len(eligible)
    eligible.sort(key=lambda r: -r["return_pct_day"])
    picked = eligible[:top]

    if not args.dry_run:
        RUN.mkdir(exist_ok=True)
        marker = RUN / "ranking.marker"
        # Validate marker both for PID liveness AND timestamp freshness
        if marker.exists():
            try:
                mdata = json.loads(marker.read_text(encoding="utf-8"))
                owner_pid = mdata.get("pid")
                marker_ts = mdata.get("ts")
                now_ts = time.time()
                # Stale threshold: 5 minutes. If marker is older, it's abandoned.
                MARKER_STALE_SEC = 300
                is_stale = (marker_ts is None or (now_ts - marker_ts) > MARKER_STALE_SEC)
                is_dead = (owner_pid is None or not _pid_is_running(owner_pid))

                if is_stale or is_dead:
                    # Clear stale or invalid marker
                    try:
                        marker.unlink()
                    except OSError:
                        pass
                elif owner_pid != os.getpid():
                    # Valid, fresh marker from a different running process
                    print(f"Skipping write: concurrent ranker run detected (PID {owner_pid})")
                    return
            except Exception:
                # Malformed marker, clear it
                try:
                    marker.unlink()
                except OSError:
                    pass
        try:
            marker.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}), encoding="utf-8")
        except Exception:
            pass

        # Temp file and rename: the fleet re-reads this on its own schedule and
        # a half-written file is a SystemExit on the next re-rank.
        f = RUN / "markets.json"
        tmp = RUN / f"markets.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            tmp.write_text(json.dumps(picked, indent=1), encoding="utf-8")
            tmp.replace(f)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            # Remove the marker after successful write so subsequent runs aren't blocked
            if marker.exists():
                try:
                    marker.unlink()
                except OSError:
                    pass

    ti = sum(r["est_income"] for r in picked)
    tc = sum(r["est_capital"] for r in picked)
    # Rejections grouped by cause. A run that returns nothing must say whether
    # the venue had no tradeable market today or the filters are set wrong --
    # a bare count cannot, and a silent empty universe is how the fleet ended
    # up quoting markets that never traded.
    # Bucket by GATE, not by first word. Splitting on whitespace put one gate
    # in two buckets -- "volume unknown" landed under `volume` while "24h
    # volume $900 < $5,000" landed under `24h` -- and "no spread income"
    # became `no`. Labels that do not match the gates cannot answer the
    # question this block exists to answer.
    def _cause(reason: str) -> str:
        r = reason.lower()
        if "volume" in r:
            return "volume"
        if "horizon" in r:
            return "horizon"
        if "income" in r:
            return "income"
        return reason.split(" $")[0] or "other"

    causes: dict[str, int] = {}
    for r in out:
        if not r["eligible"]:
            k = _cause(r["reject_reason"])
            causes[k] = causes.get(k, 0) + 1
    print(f"scored {len(out)}, rejected {rejected} "
          f"({', '.join(f'{k}={v}' for k, v in sorted(causes.items())) or 'none'}), "
          f"{'would write' if args.dry_run else 'wrote'} top {len(picked)}"
          f" -> run/markets.json")
    print(f"gates: 24h volume >= ${MIN_VOLUME_24H:,.0f}, "
          f"resolves within {MAX_DAYS_TO_RESOLVE:.0f}d, "
          f"income >= ${MIN_PAYOUT * FLOOR_MULTIPLE:.2f}/day\n")
    n_spread = sum(1 for r in picked if r["source"] == "spread")
    print(f"picked {n_spread} spread / {len(picked) - n_spread} reward\n")
    print(f"{'market':<40}{'src':>7}{'$/day':>7}{'capital':>9}{'ret%/d':>8}")
    for r in picked:
        # Windows consoles default to a legacy codepage; market titles carry
        # curly quotes and accents that crash a plain print AFTER the file is
        # already written, which looks like a failed run when it succeeded.
        title = r["title"][:40].encode("ascii", "replace").decode("ascii")
        print(f"{title:<40}{r['source'][:6]:>7}{r['est_income']:>7.2f}"
              f"{r['est_capital']:>9.0f}{r['return_pct_day']:>8.2f}")
    if tc:
        print(f"\nTOTAL capital ${tc:,.0f}  income ${ti:,.2f}/day  = {100*ti/tc:.2f}%/day")


if __name__ == "__main__":
    main()
