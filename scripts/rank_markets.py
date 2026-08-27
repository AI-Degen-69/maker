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
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategy.allocate import (marginal, spread_capture_daily)   # noqa: E402
from strategy.config import load as _load_cfg   # noqa: E402
from strategy.markets import parse_book   # noqa: E402
from strategy.rewards import score_per_share   # noqa: E402
from strategy.selector import identity_allowed, pair_books_allowed  # noqa: E402

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
MIN_TOP3_DEPTH_USD = _CFG.select_min_top3_depth_usd
MAX_BOOK_SPREAD = _CFG.select_max_book_spread

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
             days: Optional[float],
             title: object = "", slug: object = "",
             category: object = "", market_type: object = "",
             market_group: object = "", series_title: object = "",
             event_title: object = "",
             min_volume_usd: Optional[float] = None) -> tuple[bool, str]:
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
    # Keep this helper backwards-compatible for callers that only supply the
    # numeric tradability inputs. Full selector identity is enforced by
    # `evaluate`, where the venue metadata is available.
    all_meta = (title, slug, category, market_type, market_group,
                series_title, event_title)
    if any(_value not in (None, "") for _value in all_meta):
        identity_ok, identity_reason = identity_allowed(
            title, slug, category, market_type,
            market_group, series_title, event_title)
        if not identity_ok:
            return False, identity_reason
    if volume_24h is None:
        return False, "volume unknown"
    volume_bar = MIN_VOLUME_24H if min_volume_usd is None else min_volume_usd
    if volume_24h < volume_bar:
        return False, f"24h volume ${volume_24h:,.0f} < ${volume_bar:,.0f}"
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
                          pages: int = 2, per_page: int = 100,
                          min_volume_usd: Optional[float] = None) -> list[dict]:
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
    volume_bar = MIN_VOLUME_24H if min_volume_usd is None else min_volume_usd
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
    # PAGINATION CONTRACT (verified live 2026-08-10): this endpoint serves a
    # flat array and supports `offset` only. There is no cursor field in the
    # response, and `after_cursor` is silently ignored -- identical rows to
    # `offset=0` -- so keyset pagination is not possible here and there is no
    # next-cursor to feed back.
    offset = 0
    page_cap: int | None = None
    # The floor cutoff below is only sound while the venue keeps the verified
    # descending-volume sort. Track the first sub-floor row and whether a
    # qualifying row has appeared after one -- the regression that
    # invalidates the cutoff.
    floor_seen = False
    ordering_violated = False
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
            if vol < volume_bar:
                floor_seen = True
                continue
            if floor_seen and not ordering_violated:
                # A qualifying market after a sub-floor one: the venue's
                # sort regressed. Warn once, then keep filtering per-row
                # instead of trusting the cutoff -- silently dropping a
                # qualifying market is exactly the failure the ordering
                # assumption exists to rule out.
                ordering_violated = True
                print("WARNING: gamma page not sorted by volume24hr "
                      "(qualifying market below a sub-floor one); "
                      "falling back to per-row filtering for the rest "
                      "of the scan")
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
                "category": m.get("category") or m.get("categorySlug") or "",
                "market_type": m.get("marketType") or m.get("type") or "",
                "market_group": m.get("groupItemTitle") or "",
                "series_title": ((m.get("events") or [{}])[0].get("series") or [{}])[0].get("title", ""),
                "event_title": ((m.get("events") or [{}])[0].get("title") or ""),
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
        # Sorted by volume, so the first market under the floor ends the
        # useful part of the listing -- when the sort holds. Verified
        # against the live endpoint 2026-08-02 and re-verified 2026-08-10
        # with the date filters applied: 100 rows, zero inversions, and the
        # first row under the floor had no qualifying market after it. The
        # `order=volume24hr&ascending=false` sort survives
        # `end_date_min`/`end_date_max`. An inverted page sets
        # `ordering_violated`, which skips this cut and scans on. Inversion
        # detection is WITHIN a page only: a page that ends on a clean
        # sub-floor tail trusts the cut, so a venue regression at exactly
        # the page boundary (qualifying rows at the top of the next page)
        # is not detected -- the verified sort rules that case out.
        if floor_seen and not ordering_violated:
            break
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
             source: str = "rewards", *,
             min_depth_usd: Optional[float] = None,
             min_volume_usd: Optional[float] = None) -> dict | None:
    """Income and capital for one market, from its live book.

    `rate` is the market's pot in $/day, and `source` says what pays it. For a
    reward market that is the venue's emission and the $1.50 minimum payout
    applies; for a spread market it is `spread_capture_daily`, paid by the
    taker on the trade, and no minimum distribution exists to apply.
    """
    rw = m.get("rewards") or {}
    identity_ok, identity_reason = identity_allowed(
        m.get("question"), m.get("market_slug") or m.get("slug"),
        m.get("category"), m.get("market_type"),
        m.get("market_group"), m.get("series_title"), m.get("event_title"))
    if not identity_ok:
        return {
            "source": source, "eligible": False,
            "reject_reason": identity_reason,
            "cid": m.get("condition_id"),
            "title": m.get("question", "")[:90],
            "slug": m.get("market_slug", ""),
        }
    v = (rw.get("max_spread") or 3.5) / 100.0
    min_size = rw.get("min_size") or 50
    toks = [t.get("token_id") for t in (m.get("tokens") or [])]
    if len(toks) != 2 or OFFSET >= v:
        return None

    q1 = q2 = 0.0
    capital_per_share = 0.0
    mids: dict[int, float] = {}
    best_bids: dict[int, float] = {}
    books: list[tuple[str, list[tuple[float, float]], list[tuple[float, float]]]] = []
    for j, tok in enumerate(toks):
        try:
            b = session.get("https://clob.polymarket.com/book",
                            params={"token_id": tok}, timeout=12).json()
        except Exception:
            return None
        # The fetch is guarded with Exception, and so is the parse: the whole
        # point is that the scorer must never crash on venue data, whatever
        # parse_book's structural-failure type evolves into. This also rounds
        # prices to 4 decimals (shared with full_book) -- a no-op on the
        # venue's 3-decimal tick, deliberately one parse for every caller.
        try:
            book = parse_book(b, tok)
        except Exception:
            return None
        # A skipped level under-counts competitor depth, which OVERSTATES our
        # income share -- the dangerous direction for a funding decision.
        # Fail closed rather than scoring against a partial book; this used
        # to crash the whole ranking run (the parse sat outside the try and
        # the exception aborted every ThreadPool worker).
        if book["malformed"]:
            return None
        bids = list(book["bids"].items())
        asks = list(book["asks"].items())
        if not bids or not asks:
            return None
        books.append(("YES" if j == 0 else "NO", bids, asks))
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

    # The depth bar is injectable so a DEPTH-GATE TRIAL run can gate on a
    # lower bar without touching the permanent config value. None (the normal
    # path) means the permanent bar; `main` passes the resolved trial bar.
    depth_bar = (MIN_TOP3_DEPTH_USD if min_depth_usd is None
                 else min_depth_usd)
    books_ok, books_reason = pair_books_allowed(
        books, depth_bar, MAX_BOOK_SPREAD)
    if not books_ok:
        return {
            "source": source, "eligible": False,
            "reject_reason": books_reason,
            "volume_24h": round(volume_24h, 2) if volume_24h is not None else None,
            # The book WAS readable -- this market failed the depth/spread
            # gate, not the fetch -- so the competition reading an adopted
            # fleet would average over its window is already in hand here.
            # Carried so the pipeline view can estimate what the allocator
            # would have said had this market been admitted.
            "their_score": round(q_min(q1, q2), 1),
            "daily": rate if source == "rewards" else 0.0,
            "spread": round(float(m.get("_spread") or 0.0), 4) or None,
            "max_spread": rw.get("max_spread") or 3.5,
            "cid": m.get("condition_id"),
            "title": m.get("question", "")[:90],
            "slug": m.get("market_slug", ""),
        }

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
    can_trade, why = tradable(
        volume_24h, days, m.get("question"),
        m.get("market_slug") or m.get("slug"),
        m.get("category"), m.get("market_type"),
        m.get("market_group"), m.get("series_title"), m.get("event_title"),
        min_volume_usd=min_volume_usd)
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
        "category": m.get("category") or m.get("categorySlug") or "",
        "market_type": m.get("marketType") or m.get("type") or "",
        "market_group": m.get("market_group") or m.get("groupItemTitle") or "",
        "series_title": m.get("series_title") or "",
        "event_title": m.get("event_title") or "",
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


def _cause(reason: str) -> str:
    """Bucket a rejection reason by GATE, not by first word.

    Splitting on whitespace put one gate in two buckets -- "volume unknown"
    landed under `volume` while "24h volume $900 < $5,000" landed under `24h`
    -- and "no spread income" became `no`. Labels that do not match the gates
    cannot answer the question this bucketing exists to answer.
    """
    r = reason.lower()
    if "volume" in r:
        return "volume"
    if "horizon" in r:
        return "horizon"
    if "income" in r:
        return "income"
    # The book gate embeds the measured value in the reason -- "YES: spread
    # 0.8250 > 0.0600" -- so splitting on " $" left one bucket per spread
    # level (23 buckets in one live run). The side still matters (YES-side vs
    # NO-side failures are different problems), but the value only belongs in
    # the example text. Collapse to at most two cards, keeping the side tag.
    if "spread" in r:
        side = ("YES" if r.startswith("yes")
                else "NO" if r.startswith("no") else "")
        return f"{side} spread" if side else "spread"
    return reason.split(" $")[0] or "other"


def _if_adopted(r: dict) -> dict | None:
    """What the allocator WOULD have said had this rejected market been adopted.

    The allocator's admission test is the first-dollar marginal return --
    pot / competitor-depth, compared to `marginal_return_floor` -- and it is
    the same number the GRADUATED lane's alloc-verdict shows for a refused
    market. For a market the ranker refused there is no fleet-measured
    `avg_theirs` to use, so the venue's own score reading (`their_score`, the
    same q_min the fleet averages over its 30-min window) stands in as a
    single-snapshot estimate, and `k` -- the per-share score of the quote we
    would rest -- converts it to competitor depth in dollars exactly as
    `reallocate` does. `pot` is the reward rate for a reward market, or the
    spread-capture pot for a spread one, so both income sources are judged
    on the same axis as the fleet's water-fill.

    Returns None where no book reading exists -- an identity rejection never
    fetched the book, and a readable book with `their_score` 0.0 (nothing
    resting inside the reward window) is the thin-book shape the depth gate
    exists to catch, so treating it as an empty competitor field and guessing
    "we would take the whole pot" would be the wrong signal (the same
    no-guess principle as the fleet's `avg_theirs()` returning None). There
    is nothing honest to estimate from in either case.
    """
    theirs = r.get("their_score")
    if not theirs:
        return None
    pot = r.get("daily") or 0.0
    if pot <= 0 and r.get("source") == "spread":
        pot = spread_capture_daily(
            float(r.get("volume_24h") or 0.0),
            float(r.get("spread") or _CFG.spread_capture_default_spread),
            _CFG.spread_capture_frac)
    k = score_per_share(float(r.get("max_spread") or 3.5) / 100.0, OFFSET)
    # T == inf (k == 0 -- we would score nothing per share -- or a null
    # reading): the first dollar earns nothing, not NaN. Same guard as the
    # fleet's `_alloc_verdict`.
    T = theirs / k if k and k > 0 else float("inf")
    first = 0.0 if T == float("inf") else marginal(0.0, pot, T) * 100.0
    thresh = _CFG.marginal_return_floor * 100.0
    # With no pot `first` is always 0.0, so the floor comparison alone is
    # the admission test -- no separate pot guard needed.
    would_fund = first >= thresh
    # The MIRAGE arm: the estimate is pot / competition, so a book with
    # nobody resting inside the reward window (competition reading near zero)
    # divides by ~nothing and reports an absurd %/day -- 890%/day on the Dem
    # retirees book, 4,938%/day on UK inflation. That is the empty-book shape
    # the depth gate exists to catch, not an opportunity. A depth reject at
    # under half the gate bar is the same shape measured directly. Both are
    # flagged `trap` so the lanes can show them honestly instead of as green
    # "would clear the floor" wins -- same rule the tracker's pot tile uses.
    _d = _DEPTH_RE.search(r.get("reject_reason") or "")
    depth_trap = False
    if _d:
        dm = float(_d.group(1).replace(",", ""))
        db = float(_d.group(2).replace(",", ""))
        depth_trap = db > 0 and dm < 0.5 * db
    trap = depth_trap or first > 10.0
    if pot <= 0:
        reason = "unpayable: no pot (spread/volume unmeasured)"
    elif trap:
        reason = ("empty-book mirage: nobody resting in the reward window, "
                  "the estimate divides by ~zero competition and is not real")
    elif would_fund:
        reason = (f"first dollar clears the {thresh:.2f}%/day floor -- "
                  "the allocator would have admitted it")
    else:
        reason = f"below the {thresh:.2f}%/day floor"
    return {
        "marg_pct_day": round(first, 2),
        "would_fund": would_fund,
        "trap": trap,
        "threshold_pct": round(thresh, 2),
        "pot_day": round(pot, 2),
        "competition": round(theirs, 1),
        "reason": reason,
    }


def _effective_depth_bar(cli_trial_usd: Optional[float]) -> float:
    """The depth bar this run gates on: CLI trial > config trial > permanent.

    `select_min_top3_depth_usd_trial` (env HUNTER_DEPTH_TRIAL_USD) lets an
    operator stage the trial without touching the permanent config; an explicit
    `--trial-depth` on the command line wins over both because it is the most
    deliberate of the three. A non-positive trial value is a mistake, not a
    signal -- fall back to the permanent bar rather than gating on nothing.
    """
    trial = cli_trial_usd
    if trial is None:
        trial = _CFG.select_min_top3_depth_usd_trial
    if trial is not None and trial > 0:
        return float(trial)
    return MIN_TOP3_DEPTH_USD


def _effective_volume_bar(cli_trial_usd: Optional[float]) -> float:
    """The volume bar this run gates on: CLI trial > config trial > permanent.

    Mirrors `_effective_depth_bar` for the VOLUME-GATE TRIAL (U36): the
    permanent `select_min_volume_24h_usd` never changes, and a non-positive
    trial value is a mistake, not a signal.
    """
    trial = cli_trial_usd
    if trial is None:
        trial = _CFG.select_min_volume_24h_usd_trial
    if trial is not None and trial > 0:
        return float(trial)
    return MIN_VOLUME_24H


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
    p.add_argument("--trial-depth", type=float, default=None, metavar="USD",
                   help="DEPTH-GATE TRIAL (U32): gate on this top-3 bid depth "
                        "bar instead of the permanent one ($%.0f). Wins over "
                        "HUNTER_DEPTH_TRIAL_USD; the permanent config value is "
                        "never changed. Adopted markets are tagged "
                        "trial_depth_usd in run/markets.json so their "
                        "markouts can be watched before the bar is loosened "
                        "permanently. See scripts/trial_depth_gate.py for the "
                        "recorded-data replay that shows which markets a bar "
                        "adopts." % MIN_TOP3_DEPTH_USD)
    p.add_argument("--trial-volume", type=float, default=None, metavar="USD",
                   help="VOLUME-GATE TRIAL (U36): gate 24h volume on this bar "
                        "instead of the permanent one ($%.0f). Wins over "
                        "HUNTER_VOLUME_TRIAL_USD; the permanent config value is "
                        "never changed. Adopted markets are tagged "
                        "trial_volume_usd in run/markets.json so their "
                        "markouts can be watched before the bar is loosened "
                        "permanently." % MIN_VOLUME_24H)
    return p.parse_args(argv)


# A depth-gate reason embeds its measurement -- "YES: top-3 bid depth
# $612.00 <= $1,000.00" -- and the near-miss log records it so the stats can
# answer "how many of these would a modest loosening have admitted?".
_DEPTH_RE = re.compile(
    r"top-3 bid depth \$([\d,.]+) <= \$([\d,.]+)", re.IGNORECASE)

# A volume-gate reason embeds its measurement too -- "24h volume $12,906 <
# $250,000" -- and the VOLUME near-miss log (U34) records it so the stats can
# answer "how many of these would a looser bar have admitted?".
_VOLUME_RE = re.compile(
    r"24h volume \$([\d,.]+) < \$([\d,.]+)", re.IGNORECASE)


def _log_rank_near_misses(out, rejected, verdicts, ts=None) -> int:
    """Append this rank's near-misses to run/near_misses.jsonl.

    A NEAR-MISS is a rejected market whose if-adopted first-dollar marginal
    return clears the allocator's floor -- the green cards on the FILTERS
    lane. The ranker's own gates refused it, but the allocator would have
    funded it, so every one is a candidate for loosening a gate. Written as
    ONE line per rank (the greens embedded), so a rank with zero greens still
    records itself -- the stats reader needs to tell "no greens" from "no
    data", and the stability bar is a fraction of ranks.

    Telemetry only, and it is the input to the dashboard's near-miss tracker:
    it accumulates whether the estimate is CONSISTENT over days -- the
    precondition for a controlled gate-loosening trial. Consistency is not
    profitability; the trial measures that.

    Returns how many greens were logged this rank.
    """
    greens = []
    depth_unparsed = 0
    for r in out:
        if r.get("eligible"):
            continue
        v = verdicts.get(id(r))
        if not v or not v["would_fund"]:
            continue
        d = _DEPTH_RE.search(r.get("reject_reason") or "")
        if not d and "top-3 bid depth" in (r.get("reject_reason") or ""):
            # The reason format changed and the parse went quiet -- the
            # small-margin bar would undercount with no signal. Recorded on
            # the line so the tracker can show it.
            depth_unparsed += 1
        greens.append({
            "cid": r.get("cid"), "title": r.get("title"),
            "slug": r.get("slug"),
            "cause": _cause(r.get("reject_reason") or ""),
            "reason": r.get("reject_reason"),
            "source": r.get("source"),
            "marg_pct_day": v["marg_pct_day"], "pot_day": v["pot_day"],
            "competition": v["competition"],
            "trap": v.get("trap", False),
            "threshold_pct": v["threshold_pct"],
            "volume_24h": r.get("volume_24h"),
            "days": r.get("days_to_resolve"),
            "depth_measured": (float(d.group(1).replace(",", ""))
                                if d else None),
            "depth_bar": (float(d.group(2).replace(",", ""))
                           if d else None),
        })
    line = {"ts": ts if ts is not None else time.time(),
            "scored": len(out), "rejected": rejected,
            "depth_unparsed": depth_unparsed,
            "greens": greens}
    RUN.mkdir(exist_ok=True)
    with open(RUN / "near_misses.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    return len(greens)


def _log_rank_volume_near_misses(out, rejected, verdicts=None, ts=None) -> int:
    """Append this rank's volume-rejects to run/volume_near_misses.jsonl.

    The DEPTH near-miss log records only would-fund greens, and U33's triage
    showed the binding constraint is the VOLUME gate, not depth: on the
    recorded population, 83 of 110 depth-rejects -- including 5 of the 6
    near-misses -- would fail the live $250k/24h bar anyway, and the $500
    depth trial adopted zero new markets because the depth-clear candidates
    failed live re-verification on volume. That population never reached any
    log: volume rejects are refused by `tradable` inside `evaluate`, and the
    depth logger only keeps greens.

    This sibling log records EVERY volume-rejected market whose reason
    carries a measured 24h volume ("24h volume $X < $Y" parses it out;
    "volume unknown" is a data gap, not a near-miss, and is skipped but
    counted). One line per rank, exactly like the depth log, so the stats
    reader accumulates days, unique markets, and how many measured volumes
    came within half the bar -- the same evidence shape that licensed the
    depth trial, applied to the gate that actually binds.

    Returns how many volume-rejects were logged this rank.
    """
    vols = []
    volume_unknown = 0
    for r in out:
        if r.get("eligible"):
            continue
        reason = r.get("reject_reason") or ""
        if _cause(reason) != "volume":
            continue
        v = _VOLUME_RE.search(reason)
        if not v:
            # "volume unknown": gamma never returned a reading. Not a
            # near-miss -- but count it so the tracker can show the gap
            # instead of silently undercounting the population.
            volume_unknown += 1
            continue
        vd = verdicts.get(id(r)) if verdicts else None
        vols.append({
            "cid": r.get("cid"), "title": r.get("title"),
            "slug": r.get("slug"),
            "cause": "volume",
            "reason": reason,
            "source": r.get("source"),
            "volume_measured": float(v.group(1).replace(",", "")),
            "volume_bar": float(v.group(2).replace(",", "")),
            "volume_24h": r.get("volume_24h"),
            "days": r.get("days_to_resolve"),
            # The allocator verdict travels too, so the tracker can show the
            # pot and competition of the population, not just its volume.
            "pot_day": (vd["pot_day"] if vd else r.get("daily") or 0.0),
            "competition": (vd["competition"] if vd
                             else r.get("their_score")),
            "marg_pct_day": vd["marg_pct_day"] if vd else None,
            "trap": bool(vd and vd.get("trap")),
        })
    line = {"ts": ts if ts is not None else time.time(),
            "scored": len(out), "rejected": rejected,
            "volume_unknown": volume_unknown,
            "volumes": vols}
    RUN.mkdir(exist_ok=True)
    with open(RUN / "volume_near_misses.jsonl", "a",
              encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    return len(vols)


def _write_pipeline_snapshot(cands, spread_cands, out, eligible, picked,
                             causes, census, gates, attempted,
                             rejected, verdicts=None,
                             depth_gate_usd: Optional[float] = None,
                             trial_depth_usd: Optional[float] = None,
                             volume_gate_usd: Optional[float] = None,
                             trial_volume_usd: Optional[float] = None) -> None:
    """Persist the whole selection funnel to run/pipeline.json.

    run/markets.json keeps only the winners, so the dashboard can show the
    fleet but not the funnel that produced it. This file keeps the rest of
    the run: the raw pools the ranker listed, every rejection bucketed by
    gate with example titles, the eligible-but-unpicked ranking, and the
    picks -- enough to replay the funnel's shape live, run after run.

    Telemetry only: nothing in the fleet reads it as input, so writing it
    during a --dry-run audit is safe (and useful -- the dashboard then shows
    the audited funnel, timestamp and gates line included, rather than a
    silently stale picture).
    """
    def _days(m):
        return days_to_resolve(m.get("end_date_iso"))

    raw_rewards = []
    for rate, m in cands[:24]:
        _slug = m.get("slug") or m.get("market_slug") or ""
        raw_rewards.append({
            "title": (m.get("question") or "")[:80],
            "slug": _slug,
            "cid": m.get("condition_id") or "",
            "url": f"https://polymarket.com/market/{_slug}" if _slug else "",
            "rate": round(rate, 2),
            "days": _days(m),
        })
    raw_spread = []
    for m in spread_cands[:24]:
        _slug = m.get("slug") or m.get("market_slug") or ""
        raw_spread.append({
            "title": (m.get("question") or "")[:80],
            "slug": _slug,
            "cid": m.get("condition_id") or "",
            "url": f"https://polymarket.com/market/{_slug}" if _slug else "",
            "volume": round(float(m.get("_volume_24h") or 0.0), 0),
            "spread": m.get("_spread"),
            "days": _days(m),
        })

    # Estimated allocator verdict per scored-and-rejected market, keyed by
    # object id so the per-bucket near-miss count and the example cards are
    # computed from the same rows and cannot disagree. Precomputed by `main`
    # and shared with the near-miss logger; computed here for direct callers.
    if verdicts is None:
        verdicts = {id(r): _if_adopted(r) for r in out}

    rejections = []
    for cause, n in sorted(causes.items(), key=lambda kv: -kv[1]):
        bucket_rows = [r for r in out
                       if not r["eligible"] and _cause(r["reject_reason"]) == cause]
        examples = []
        for r in bucket_rows[:4]:
            v = verdicts.get(id(r))
            _slug = r.get("slug") or ""
            examples.append({
                "title": r["title"],
                "slug": _slug,
                "cid": r.get("cid") or "",
                "url": f"https://polymarket.com/market/{_slug}" if _slug else "",
                "reason": r["reject_reason"],
                "volume": r.get("volume_24h"),
                "days": r.get("days_to_resolve"),
                # Absent (not None) for markets with no book reading -- an
                # identity reject never fetched the book, so there is nothing
                # to estimate from and the card shows no verdict.
                **({"marg": v} if v else {}),
            })
        # would_fund counts only CREDIBLE near-misses: an empty-book mirage
        # (the estimate dividing by ~zero competition) is not evidence for
        # loosening a gate -- the traps are shown separately, not hidden.
        would_fund = sum(1 for r in bucket_rows
                         if (v := verdicts.get(id(r)))
                         and v["would_fund"] and not v["trap"])
        traps = sum(1 for r in bucket_rows
                    if (v := verdicts.get(id(r))) and v and v["trap"])
        rejections.append({"cause": cause, "n": n,
                           "would_fund": would_fund, "traps": traps,
                           "examples": examples})

    def _row(r: dict) -> dict:
        _slug = r.get("slug") or ""
        return {
            "title": r["title"], "slug": _slug,
            "cid": r.get("cid") or "",
            "url": f"https://polymarket.com/market/{_slug}" if _slug else "",
            "source": r["source"],
            "income": r.get("est_income"), "capital": r.get("est_capital"),
            "ret_day_pct": r.get("return_pct_day"),
            "volume": r.get("volume_24h"), "days": r.get("days_to_resolve"),
        }

    snap = {
        "ts": time.time(),
        "census": census,
        "gates": gates,
        # Which depth bar this rank gated on, and whether it was a TRIAL bar
        # rather than the permanent one -- the dashboard's funnel would
        # otherwise silently show a loosened gate as if it were the standing
        # contract.
        "depth_gate_usd": depth_gate_usd,
        "trial_depth_usd": trial_depth_usd,
        # Which volume bar this rank gated on -- same trial contract as depth.
        "volume_gate_usd": (volume_gate_usd if volume_gate_usd is not None
                            else MIN_VOLUME_24H),
        "trial_volume_usd": trial_volume_usd,
        "counts": {
            "funded": len(cands),
            "spread_universe": len(spread_cands),
            "attempted": attempted,
            "scored": len(out),
            "dropped_no_verdict": attempted - len(out),
            "rejected": rejected,
            "eligible": len(eligible),
            "picked": len(picked),
        },
        "raw": {"rewards": raw_rewards, "spread": raw_spread},
        "rejections": rejections,
        "final": [_row(r) for r in eligible],
        "picked": [_row(r) for r in picked],
    }
    RUN.mkdir(exist_ok=True)
    f = RUN / "pipeline.json"
    tmp = RUN / f"pipeline.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        tmp.write_text(json.dumps(snap, indent=1), encoding="utf-8")
        tmp.replace(f)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


_worker_local = threading.local()


def _worker_session() -> requests.Session:
    """One `requests.Session` per worker thread, created lazily.

    The requests documentation is explicit that a Session is not thread-safe
    ("use a session per thread, or use a connection pool elsewhere"), and the
    ranking pool ran 12 ThreadPool workers against one shared session -- a
    latent shared-state bug that every future edit to the fetchers could
    trip. Each thread lazily owns its own keep-alive pool instead; the
    session is created on first use inside the thread and reused for all of
    that thread's markets.
    """
    s = getattr(_worker_local, "session", None)
    if s is None:
        s = requests.Session()
        _worker_local.session = s
    return s


def score_pool(jobs: list[tuple[float, dict, Optional[float], str]],
               *, session_factory=_worker_session,
               max_workers: int = 12,
               min_depth_usd: Optional[float] = None,
               min_volume_usd: Optional[float] = None) -> list[dict]:
    """Score candidate jobs across a worker pool, one session per worker.

    `session_factory` is injected so a test can prove the pool never shares
    one session object across workers -- the documented-not-thread-safe
    shape `main` used to have. Each worker's session comes from the factory,
    so the #15 seam still holds: `evaluate` takes a session and opens no
    connection itself.
    """
    out: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for r in ex.map(
                lambda a: evaluate(session_factory(), a[0], a[1], a[2],
                                   source=a[3],
                                   min_depth_usd=min_depth_usd,
                                   min_volume_usd=min_volume_usd),
                jobs):
            if r:
                out.append(r)
    return out


def main() -> None:
    args = parse_args()
    top = args.top
    # DEPTH-GATE TRIAL (U32): the bar this run gates on. Same contract as the
    # permanent config value, but opt-in per run and never written back to
    # config; adopted markets are tagged so the trial's markouts can be
    # watched before the bar is loosened permanently.
    trial_bar = _effective_depth_bar(args.trial_depth)
    trial_active = trial_bar != MIN_TOP3_DEPTH_USD
    volume_bar = _effective_volume_bar(args.trial_volume)
    volume_trial_active = volume_bar != MIN_VOLUME_24H

    # Up-front universe fetches run sequentially on the main thread and keep
    # their own keep-alive session; the worker pool below uses one session
    # per thread (see `_worker_session`) instead of sharing this one.
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
    spread_cands = gamma_spread_universe(s, min_volume_usd=volume_bar)
    volume_str = (f"${volume_bar:,.0f}"
                  + (f" [TRIAL vs permanent ${MIN_VOLUME_24H:,.0f}]"
                     if volume_trial_active else ""))
    print(f"unfunded liquid markets: {len(spread_cands)} "
          f"(>= {volume_str}/24h, <= {MAX_DAYS_TO_RESOLVE:.0f}d)")

    jobs = [(rate, m, vols.get(m["condition_id"]), "rewards")
            for rate, m in short]
    jobs += [(spread_capture_daily(m["_volume_24h"], m["_spread"],
                                   _CFG.spread_capture_frac),
              m, m["_volume_24h"], "spread")
             for m in spread_cands]

    out = score_pool(jobs, min_depth_usd=trial_bar,
                     min_volume_usd=volume_bar)
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

        # STAGING MARKER. A trial-run adoption is not a permanent gate change:
        # each picked spec carries the bar it was admitted under, so the fleet
        # and dashboard can identify trial markets and their markouts are the
        # evidence that decides whether the bar becomes permanent.
        if trial_active:
            for r in picked:
                r["trial_depth_usd"] = trial_bar
        if volume_trial_active:
            for r in picked:
                r["trial_volume_usd"] = volume_bar

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
    causes: dict[str, int] = {}
    for r in out:
        if not r["eligible"]:
            k = _cause(r["reject_reason"])
            causes[k] = causes.get(k, 0) + 1
    census = (f"scored {len(out)}, rejected {rejected} "
              f"({', '.join(f'{k}={v}' for k, v in sorted(causes.items())) or 'none'}), "
              f"{'would write' if args.dry_run else 'wrote'} top {len(picked)}"
              f" -> run/markets.json")
    print(census)
    depth_bar_str = (f"${trial_bar:,.0f}"
                     + (f" [TRIAL vs permanent ${MIN_TOP3_DEPTH_USD:,.0f}]"
                        if trial_active else ""))
    volume_bar_str = (f"${volume_bar:,.0f}"
                      + (f" [TRIAL vs permanent ${MIN_VOLUME_24H:,.0f}]"
                         if volume_trial_active else ""))
    gates = (f"gates: primary/main-line only, blocked submarkets/live; "
             f"24h volume >= {volume_bar_str}, "
             f"YES+NO top-3 bid depth >= {depth_bar_str} each, "
             f"spread <= {MAX_BOOK_SPREAD:.2f}, "
             f"resolves within {MAX_DAYS_TO_RESOLVE:.0f}d, "
             f"income >= ${MIN_PAYOUT * FLOOR_MULTIPLE:.2f}/day\n")
    print(gates)
    if trial_active:
        print(f"DEPTH-GATE TRIAL: gating on ${trial_bar:,.0f} instead of "
              f"the permanent ${MIN_TOP3_DEPTH_USD:,.0f}; adopted markets "
              "are tagged trial_depth_usd and their markouts are the "
              "decision evidence (see scripts/trial_depth_gate.py)")
    if volume_trial_active:
        print(f"VOLUME-GATE TRIAL: gating on ${volume_bar:,.0f} instead of "
              f"the permanent ${MIN_VOLUME_24H:,.0f}; adopted markets are "
              "tagged trial_volume_usd and their markouts are the "
              "decision evidence")

    verdicts = {id(r): _if_adopted(r) for r in out}
    _write_pipeline_snapshot(
        cands=cands, spread_cands=spread_cands, out=out, eligible=eligible,
        picked=picked, causes=causes, census=census, gates=gates,
        attempted=len(jobs), rejected=rejected, verdicts=verdicts,
        depth_gate_usd=trial_bar,
        trial_depth_usd=(trial_bar if trial_active else None),
        volume_gate_usd=volume_bar,
        trial_volume_usd=(volume_bar if volume_trial_active else None))
    # The near-miss log is the accumulated evidence for a gate decision; a
    # dry-run audit must not pollute it (it would double-count against the
    # supervised every-10-min ranks).
    if not args.dry_run:
        n_greens = _log_rank_near_misses(out, rejected, verdicts)
        if n_greens:
            print(f"near-misses logged: {n_greens} would clear the floor")
        # The VOLUME tracker (U34): every volume-reject with a measured
        # reading, for the gate U33 showed actually binds. Same dry-run guard
        # as the depth log -- an audit must not pollute the evidence.
        n_vols = _log_rank_volume_near_misses(out, rejected, verdicts)
        if n_vols:
            print(f"volume-rejects logged: {n_vols} measured "
                  "(volume near-miss tracker)")
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
