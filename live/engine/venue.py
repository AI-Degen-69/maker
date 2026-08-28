"""The Polymarket CLOB adapter: client construction, order caps, and venue
response plumbing.

Extracted from engine.live_exec so the fleet loop can cross a public seam
instead of importing live_exec's privates. Everything here is venue plumbing —
no strategy, no registry. The strategy-side decision modules and the registry
stay in their own homes.

Credential rule, inherited from live_exec: a key is read from the environment
and passed on in a single expression — never bound to a module global, never
returned, never in a log line.
"""
from __future__ import annotations

import hashlib
import os

# Hard ceilings. Not configuration -- this is the difference between a POC and
# an unbounded loss, so they live in code where a stray env var cannot raise
# them. Edit deliberately, never to "just get this order through".
MAX_ORDER_USD = 25.0
MAX_TOTAL_USD = 100.0


# ── UA patch ──────────────────────────────────────────────────────────────────
# Polymarket's WAF now returns HTTP 403 for the SDK's default User-Agent
# ("py_clob_client_v2"). Monkey-patch _overload_headers at import time so
# every request carries a browser-like UA. Done here rather than in
# site-packages so pip upgrades don't silently revert it.
def _patch_sdk_user_agent():
    import py_clob_client_v2.http_helpers.helpers as _h
    _orig = _h._overload_headers

    def _patched(method, headers):
        headers = _orig(method, headers)
        headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
        return headers
    _h._overload_headers = _patched


_patch_sdk_user_agent()

# One built client per (funder, sig_type, host) for the life of the process.
# Every client() call used to POST /auth/api-key and then GET
# /auth/derive-api-key, so a single command that touched the venue twice
# derived twice, and a session of CLI runs derived once per run. Derivation is
# the most rate-limit-sensitive call in the API. Process-local only: the creds
# are never written to disk, matching the "never stored" rule in live_exec.
_CLIENT_CACHE: dict = {}


def client(funder: str | None = None):
    """Build a CLOB client from the environment. Raises if anything is absent.

    The key is read and passed on in a single expression: never bound to a
    module global, never returned, never in a log line.

    `funder` overrides POLY_FUNDER for one call. That exists so a candidate
    address can be balance-checked before it is committed to .env.
    """
    from py_clob_client_v2.client import ClobClient

    key = os.environ.get("POLY_PRIVATE_KEY") or os.environ.get("POLY_KEY")
    if not key:
        raise SystemExit(
            "POLY_PRIVATE_KEY not set. Put it in .env -- and confirm .env is "
            "in .gitignore before you paste anything into it.")

    funder = funder or os.environ.get("POLY_FUNDER")
    sig_type = int(os.environ.get("POLY_SIG_TYPE", "3"))
    host = os.environ.get("CLOB_HOST", "https://clob.polymarket.com")

    # Keyed on the signing key too, so a changed key never reuses a client
    # authenticated as someone else. Hashed: the key itself stays out of any
    # structure that could be printed or logged.
    cache_key = (hashlib.sha256(key.encode()).hexdigest(), funder, sig_type, host)
    cached = _CLIENT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    c = ClobClient(host, key=key, chain_id=137,
                   signature_type=sig_type, funder=funder)
    creds = api_creds_from_env()
    if creds is not None:
        # Already-issued L2 credentials. This is the path that makes no network
        # call at all: derivation is the most rate-limit-sensitive endpoint in
        # the API, and deriving once per command is what got this account
        # throttled -- `balance` succeeded and `account-sweep` timed out on the
        # same credentials twenty seconds later.
        c.set_api_creds(creds)
    else:
        c.set_api_creds(c.create_or_derive_api_key())
    _CLIENT_CACHE[cache_key] = c
    return c


def api_creds_from_env():
    """L2 API credentials from the environment, or None if incomplete.

    All three must be present. A partial set would build a client that fails
    every signed request with an error that looks like a venue outage.
    """
    from py_clob_client_v2.clob_types import ApiCreds

    api_key = os.environ.get("POLY_API_KEY")
    secret = os.environ.get("POLY_API_SECRET")
    passphrase = os.environ.get("POLY_API_PASSPHRASE")
    if not (api_key and secret and passphrase):
        return None
    return ApiCreds(api_key=api_key, api_secret=secret, api_passphrase=passphrase)


def open_notional(c) -> float:
    """Dollars currently committed in open resting orders on the venue."""
    try:
        orders = c.get_open_orders() or []
        return sum(float(o.get("price", 0) or 0)
                   * float(o.get("original_size", 0) or 0)
                   for o in orders)
    except Exception:
        return 0.0


def venue_order_id(resp) -> str | None:
    """Pull the venue order id out of a post_order response.

    Several spellings are accepted because the field name has moved across SDK
    versions, and a missing id is reported rather than guessed: attaching the
    wrong id would bind our row to somebody else's order.
    """
    if resp is None:
        return None
    for key in ("orderID", "orderId", "order_id", "id"):
        value = resp.get(key) if isinstance(resp, dict) else getattr(resp, key, None)
        if value:
            return str(value)
    return None
