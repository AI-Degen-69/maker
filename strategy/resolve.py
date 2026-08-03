"""Settlement pass: record which outcome won, for markets we have filled.

The pass itself is ported from the pre-fleet code path (`resolve_finished` in
archive/legacy-bot-8788/strategy/main.py). The fleet rewrite dropped it and
never replaced it, so `store.record_resolution` sat with no caller and
`resolutions` stayed empty in every database this project has produced --
which read on the dashboard as "nothing has settled yet" rather than as
"nothing can ever settle".

Two things depend on that table, and both were silently dead without it:
settled P&L (strategy/kpi.py, server/fleet_dash.py), which is the only ground
truth this strategy has, and `store.unresolved()`, which is what tells the
fleet a market is finished and its capital free.

WHY THE CLOB MARKET ENDPOINT AND NOT GAMMA `/events?slug=`
The archived pass asked Gamma for an event by slug and read `markets[0]`.
Both halves are wrong for this question, and both failed on live data:

  - An event is not a market. `atp-zheng-kecmano-2026-08-02` carries 16 of
    them -- the match, completed-match, first-set-winner, totals, handicaps --
    each with its own condition_id, its own `closed` flag and its own winner.
    Position 0 is our market only by luck; on the first live pass it scored 7
    of 8 settlements against a sibling market.
  - Many market slugs are not event slugs at all. The game-2 markets, the
    handicaps, and `will-bitcoin-dip-to-62k-july-27-august-2-2026` all
    returned zero events, so they could never settle by that route.

`/markets/{condition_id}` is keyed on the identity we already hold, so there
is nothing to match and nothing to disambiguate, and it states the winner
outright instead of leaving it to be inferred from prices. It is the same
endpoint `strategy.markets.market_meta` already uses.
"""
from __future__ import annotations

import logging

import requests

from strategy import store

log = logging.getLogger("maker")

# (connect, read), matching strategy.markets.MARKET_TIMEOUT. This runs on the
# fleet's own thread, so a hung settlement lookup is a stalled trading loop.
MARKET_TIMEOUT = (3.05, 5.0)

# Pooled for the same reason as strategy.markets._SESSION: keep-alive rather
# than a fresh TLS handshake per market. No retries -- a market that fails to
# resolve this pass is asked again next pass, and retrying inside the loop
# spends the trading budget silently.
_SESSION = requests.Session()
for _scheme in ("https://", "http://"):
    _SESSION.mount(_scheme, requests.adapters.HTTPAdapter(
        pool_connections=4, pool_maxsize=4, max_retries=0))


def _winning_token(market: dict) -> str | None:
    """The single token flagged `winner`, or None if that is not unambiguous.

    A closed market can briefly report no winner at all, and a malformed one
    can report two. Neither is a coin flip: `resolutions` is keyed by
    condition_id, so a row written now is never revisited or corrected -- a
    guess here is permanent. Skipping costs one interval.
    """
    tokens = market.get("tokens")
    if not isinstance(tokens, list):
        return None
    winners = [t for t in tokens
               if isinstance(t, dict) and t.get("winner")]
    if len(winners) != 1:
        return None
    token_id = winners[0].get("token_id")
    return str(token_id) if token_id else None


def resolve_finished(clob_host: str) -> int:
    """Record a resolution row for every filled market the venue reports closed.

    Returns the number of markets newly recorded. Never raises, and one bad
    market never strands the ones behind it: a settlement lookup is not worth
    taking the fleet down for, and anything skipped is retried next pass.
    """
    n = 0
    for cond, slug in store.unresolved():
        try:
            r = _SESSION.get(f"{clob_host}/markets/{cond}",
                             timeout=MARKET_TIMEOUT)
            market = r.json()
            if not isinstance(market, dict):
                continue
            # An open market is the normal case, not an error -- most of the
            # unresolved set is still trading.
            if not market.get("closed"):
                continue

            winner = _winning_token(market)
            if winner is None:
                log.warning("resolve: %s closed with no unambiguous winner",
                            slug)
                continue

            store.record_resolution(cond, winner)
            log.info("resolved %s -> %s", slug, winner[:12])
            n += 1
        except Exception as e:
            # Debug, not warning: an unreachable venue during a pass over
            # dozens of markets would otherwise write dozens of warning lines
            # per interval for a condition the next pass handles by itself.
            log.debug("resolve failed %s: %s: %s", slug, type(e).__name__, e)
    return n
