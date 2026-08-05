"""Slim shim — only the live helpers ``strategy.fleet`` consumes are kept here.

The single-market quoting loop, the resolve_finished pass, the single-instance
pidguard, ``loop()`` / ``main()`` / ``argparse`` entrypoints, the live-state
publisher, the partial-fill hedge, and every reward-share calculation that
used to live in this module were the pre-fleet code path on port 8788. The
fleet pipeline (``strategy.fleet`` + ``server.fleet_dash`` on port 8800) has
superseded them, and the originals have been preserved on the
``archive/legacy-bot-8788`` git branch at
``archive/legacy-bot-8788/strategy/main.py``.

The only functions below are the two that ``strategy.fleet`` still imports:

    from strategy.main import full_book, recent_trades

Anything else that used to import from ``strategy.main`` is importing dead
code; either re-point it at the live equivalent in ``strategy.markets`` /
``strategy.fills`` / ``strategy.quotes``, or import from the archive branch
if the historical surface is genuinely needed.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger("maker")

TRADES_API = "https://data-api.polymarket.com/trades"

# (connect, read) rather than one scalar. Split deliberately: a host that is not
# answering its SYN at all is abandoned in ~3s, while a host that did answer
# gets 5s to finish the body. The old scalar 10s applied to BOTH phases, so a
# single unreachable endpoint could add 20s to one market visit -- three such
# markets in a sweep is the difference between a 60s cycle and the >120s the
# dashboard calls dead.
BOOK_TIMEOUT = (3.05, 5.0)
TAPE_TIMEOUT = (3.05, 5.0)

# One pooled session for the process. A bare `requests.get` opens a new
# connection per call and pays a full TCP + TLS handshake every time; the fleet
# makes three of these per market visit, so a 20-market sweep spent 60
# handshakes' worth of latency inside the cycle the staleness check measures.
# Keep-alive reuses the socket instead.
#
# `max_retries=0` is explicit rather than incidental: retrying inside the
# trading loop turns a slow venue into a long stall, and both callers already
# degrade correctly on failure -- a failed book skips the market for this
# visit, a failed tape falls back to book-only fill inference.
_SESSION = requests.Session()
for _scheme in ("https://", "http://"):
    _SESSION.mount(_scheme, requests.adapters.HTTPAdapter(
        pool_connections=8, pool_maxsize=8, max_retries=0))


class BookGone(Exception):
    """The venue holds no orderbook at all for this token.

    `/book` answers 404 with `{"error": "No orderbook exists for the requested
    token id"}` once every maker has pulled off a token, which is what happens
    the moment an outcome stops being in doubt. The market itself keeps
    reporting `active: true, accepting_orders: true` until UMA finalises it days
    later, so nothing else in the system can tell this apart from a market that
    is merely quiet.

    A distinct type rather than a generic HTTPError because the two mean
    opposite things to the caller: a timeout or a 5xx is a venue blip worth
    retrying every rotation, while this is terminal -- there is no book to quote
    into and no bid to exit against, only settlement to wait for.
    """


def full_book(clob_host: str, token_id: str) -> dict:
    """Full depth, not just top-of-book -- queue position needs the level sizes."""
    r = _SESSION.get(f"{clob_host}/book", params={"token_id": token_id},
                     timeout=BOOK_TIMEOUT)
    if r.status_code == 404:
        raise BookGone(token_id)
    r.raise_for_status()
    b = r.json()
    bids = {round(float(x["price"]), 4): float(x["size"]) for x in (b.get("bids") or [])}
    asks = {round(float(x["price"]), 4): float(x["size"]) for x in (b.get("asks") or [])}
    return {
        "token_id": token_id,
        "bids": bids,
        "asks": asks,
        "best_bid": max(bids) if bids else None,
        "best_ask": min(asks) if asks else None,
    }


def recent_trades(condition_id: str, seen: set, limit: int = 500) -> dict:
    """Volume by (token_id, price) that has actually TRADED since we last looked.

    The fill model needs this to tell a level that was TRADED from one that was
    CANCELLED -- from the book they are identical, and guessing costs an order
    of magnitude: on recorded books the book-only model reported a 50% fill
    rate where the tape-confirmed rate was 3%, because every fill it produced
    came from the "level emptied, credit the whole remainder" branch.

    De-duplicated by trade identity rather than by timestamp window: the API
    stamps trades to the second while we poll faster than that, so a time-based
    cursor would double-count or skip. `seen` is per-market and is dropped when
    the window rolls.
    """
    out: dict[str, dict[float, float]] = {}
    try:
        r = _SESSION.get(TRADES_API,
                         params={"market": condition_id, "limit": limit},
                         timeout=TAPE_TIMEOUT)
        r.raise_for_status()
        rows = r.json() or []
    except Exception as e:
        log.debug("tape fetch failed: %s", e)
        return out                      # no tape -> caller falls back to books
    for t in rows:
        key = (str(t.get("transactionHash") or ""), str(t.get("asset")),
               t.get("timestamp"), t.get("price"), t.get("size"))
        if key in seen:
            continue
        seen.add(key)
        tok = str(t.get("asset"))
        p = round(float(t.get("price") or 0), 4)
        out.setdefault(tok, {})[p] = out.setdefault(tok, {}).get(p, 0.0) + \
            float(t.get("size") or 0)
    return out
