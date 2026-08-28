"""Live fleet loop: decide -> submit -> reconcile, reusing the risk gates.

The live twin of `strategy.fleet`. The decision is `engine.quotes.decide_quotes`
-- already proven in simulation and already wired to the live risk gates -- so
this module adds NOTHING new to the decision. Its only jobs are:

1. `plan_orders`: turn "what we want resting" into "what to cancel / submit"
   without churning an order already resting at the desired price.
2. `run`: the rotation loop that ties reconcile -> decide -> submit -> sweep
   together, and never lets one market's error stop the others.

Everything that talks to the venue (fetch market, fetch books, decide, submit,
cancel, reconcile, sweep) is injectable, so the loop's behavior is tested
without a network and the production path is wired in `main`.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any, Callable, Optional

from engine.quotes import Inventory, QuoteIntent, evaluate_market_quote
from engine.cycle_stream import emit as _emit_cycle_event

log = logging.getLogger("live_fleet")

LIVE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = LIVE_ROOT.parent
RUN = REPO_ROOT / "run"


@dataclass
class LiveFleetResult:
    """One market's outcome from a single rotation visit."""
    status: str                       # QUOTED | DRY_RUN | DECLINED | ERROR
    condition_id: str = ""
    title: str = ""
    why: str = ""
    intents: list = field(default_factory=list)
    submitted: int = 0
    cancelled: int = 0
    error: str = ""


def plan_orders(
    open_orders: list[dict],
    intents: list[QuoteIntent],
    price_eps: float = 1e-9,
) -> tuple[list[dict], list[QuoteIntent]]:
    """Split open orders + desired intents into (cancel, submit).

    An order resting at (or within `price_eps` of) the desired price is kept.
    Orders on tokens we no longer quote are cancelled. An intent whose token has
    no resting order at that price is submitted. A venue rounding jitter below
    a tick must not churn cancel+resubmit, hence the epsilon.
    """
    wanted: dict[str, list[QuoteIntent]] = {}
    for i in intents:
        wanted.setdefault(i.token_id, []).append(i)

    resting: dict[str, list[dict]] = {}
    to_cancel: list[dict] = []
    for o in open_orders:
        tok = o["token_id"]
        resting.setdefault(tok, []).append(o)
        targets = wanted.get(tok)
        if not targets or not any(
            abs(i.price - o["price"]) <= price_eps for i in targets
        ):
            to_cancel.append(o)

    to_submit: list[QuoteIntent] = []
    for i in intents:
        sits = resting.get(i.token_id, [])
        if not any(abs(o["price"] - i.price) <= price_eps for o in sits):
            to_submit.append(i)

    return to_cancel, to_submit


def _cid(spec: Any) -> str:
    """The condition id from a market spec (dict or object)."""
    if isinstance(spec, dict):
        return str(spec.get("cid", ""))
    return str(getattr(spec, "cid", None) or getattr(spec, "condition_id", ""))


def _market_cfg(base, spec: Any):
    """Per-market config, mirroring strategy.fleet's MarketState.

    `decide_quotes` runs the same reward objective and the same live risk gates
    the paper fleet runs; the only per-market differences are the venue's
    min-size, spread window and tick, copied from the ranker's spec.
    """
    if not isinstance(spec, dict):
        return base
    return replace(
        base,
        objective="rewards",
        min_quote_shares=int(spec.get("min_size", base.min_quote_shares)),
        quote_shares=int(spec.get("shares", base.quote_shares)),
        max_spread_from_mid=float(
            spec.get("max_spread", base.max_spread_from_mid * 100.0)
        ) / 100.0,
        price_tick=float(spec.get("tick", base.price_tick)),
        min_t_remaining_sec=0.0,
        market_title=str(spec.get("title", "")),
        market_daily_rate=float(spec.get("daily", 0.0)),
    )


@dataclass
class VenueSeam:
    """Every venue-touching port the fleet loop needs, in one object.

    The interface of the loop: `run` reads every port off the seam, so
    adding a port does not rewire every call site. Production builds one
    in `main` (real venue calls); tests build one with fakes. A seam
    missing a required port raises in `run`, never silently.
    """
    client: Any = None
    registry: Any = None
    base_cfg: Any = None
    maker_address: Optional[str] = None
    clob_host: str = "https://clob.polymarket.com"
    fetch_market: Optional[Callable] = None
    fetch_books: Optional[Callable] = None
    decide: Optional[Callable] = None
    submit_fn: Optional[Callable] = None
    cancel_fn: Optional[Callable] = None
    reconcile_fn: Optional[Callable] = None
    sweep_fn: Optional[Callable] = None
    inventory_fn: Optional[Callable] = None
    open_orders_fn: Optional[Callable] = None
    fleet_state_fn: Optional[Callable] = None
    emit_fn: Optional[Callable] = None


def run(
    seam: VenueSeam,
    *,
    interval: float = 1.0,
    once: bool = False,
    live: bool = False,
    markets: Optional[list] = None,
    sleep_fn: Optional[Callable] = None,
) -> list[LiveFleetResult]:
    """Rotate over `markets`: reconcile, then decide+submit per market, then sweep.

    All venue-touching behaviour comes off `seam`; only loop control stays on
    the signature. The three venue-touching steps that could kill the loop --
    reconcile, sweep, and each market visit -- are each isolated so one failure
    degrades the cycle rather than stopping it. In dry-run (`live=False`)
    nothing is submitted or cancelled; reconcile and sweep are read-only and
    still run.

    With `once=True` the results of that one rotation are returned. In the
    long-running loop only the most recent rotation is kept, so a hands-off run
    does not accumulate every market visit in memory.
    """
    if seam.base_cfg is None:
        from engine.config import load
        seam.base_cfg = load()
    if sleep_fn is None:
        sleep_fn = time.sleep
    if seam.clob_host is None:
        seam.clob_host = os.environ.get("CLOB_HOST", "https://clob.polymarket.com")

    missing = [n for n, v in (
        ("fetch_market", seam.fetch_market), ("fetch_books", seam.fetch_books),
        ("decide", seam.decide), ("submit_fn", seam.submit_fn),
        ("cancel_fn", seam.cancel_fn), ("reconcile_fn", seam.reconcile_fn),
        ("sweep_fn", seam.sweep_fn),
    ) if v is None]
    if missing:
        raise TypeError(f"VenueSeam missing required ports: {', '.join(missing)}")

    # Telemetry is opt-in: main() wires engine.cycle_stream.emit; tests drive
    # the loop without it so no test ever writes into live/run/.
    emit_fn = seam.emit_fn or (lambda *a, **k: None)

    once_results: list[LiveFleetResult] = []
    last_cycle: list[LiveFleetResult] = []
    cycle = 0
    while True:
        cycle += 1
        # Fleet-wide aggregates (naked cost, committed capital, pooled posture)
        # are recomputed once per cycle and merged into the base config, so the
        # fleet-level gates inside decide_quotes see live numbers rather than
        # their 0.0 defaults. A failure here degrades to the defaults.
        if seam.fleet_state_fn is not None:
            try:
                seam.base_cfg = replace(seam.base_cfg, **seam.fleet_state_fn(seam.registry))
            except Exception as e:
                log.warning("fleet state failed: %s: %s", type(e).__name__, e)

        try:
            seam.reconcile_fn(seam.client, seam.registry, seam.maker_address)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.warning("reconcile failed: %s: %s", type(e).__name__, e)

        cycle_results: list[LiveFleetResult] = []
        for spec in list(markets or []):
            cycle_results.append(_visit_one(
                seam=seam, spec=spec, live=live, cycle=cycle,
                emit_fn=emit_fn,
            ))
        last_cycle = cycle_results
        if once:
            once_results.extend(cycle_results)

        try:
            seam.sweep_fn()
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.warning("sweep failed: %s: %s", type(e).__name__, e)

        if once:
            break
        try:
            sleep_fn(max(0.0, interval))
        except KeyboardInterrupt:
            break

    return once_results if once else last_cycle


def _visit_one(
    seam: VenueSeam,
    spec,
    live: bool,
    cycle: int = 0,
    emit_fn: Optional[Callable] = None,
) -> LiveFleetResult:
    """One poll of one market: fetch -> decide -> plan -> submit/cancel."""
    cid = _cid(spec)
    if emit_fn is None:
        emit_fn = lambda *a, **k: None
    try:
        market = seam.fetch_market(cid)
    except Exception as e:
        emit_fn(service="fleet", cycle=cycle, phase="quoting",
                action="market_error", market_slug="",
                reason=f"{type(e).__name__}: {e}")
        return LiveFleetResult(status="ERROR", condition_id=cid,
                               error=f"{type(e).__name__}: {e}")

    title = getattr(market, "market_slug", "") or cid[:16]
    cfg = _market_cfg(seam.base_cfg, spec)
    try:
        ev = evaluate_market_quote(
            cid, cfg, seam.clob_host,
            # The market is already fetched (the block above exists so a fetch
            # failure degrades to ERROR with no title); hand it to the shared
            # step, which owns the books -> inventory -> decide sequence.
            fetch_market=lambda c: market,
            fetch_books=seam.fetch_books,
            inventory_for=seam.inventory_fn or (lambda m: Inventory()),
            decide=seam.decide,
        )
        intents, why = ev.intents, ev.why
        open_orders = seam.open_orders_fn(market) if seam.open_orders_fn else []
        to_cancel, to_submit = plan_orders(open_orders, intents)
    except Exception as e:
        emit_fn(service="fleet", cycle=cycle, phase="quoting",
                action="market_error", market_slug=title,
                reason=f"{type(e).__name__}: {e}")
        return LiveFleetResult(status="ERROR", condition_id=cid, title=title,
                               error=f"{type(e).__name__}: {e}")

    emit_fn(service="fleet", cycle=cycle, phase="quoting", action="decide",
            market_slug=title, reason=why,
            extra={"intent_count": len(intents), "condition_id": cid})

    if not live:
        return LiveFleetResult(
            status="DRY_RUN" if intents else "DECLINED",
            condition_id=cid, title=title, why=why, intents=list(intents))

    submitted = cancelled = 0
    try:
        if to_submit:
            submitted = seam.submit_fn(seam.client, seam.registry, market, to_submit, cfg)
        if to_cancel:
            cancelled = seam.cancel_fn(seam.client, seam.registry, to_cancel)
    except Exception as e:
        # A submit/cancel failure (venue rejection, a split couple rolled back)
        # must degrade this market to ERROR, never stop the rotation.
        emit_fn(service="fleet", cycle=cycle, phase="quoting",
                action="market_error", market_slug=title,
                reason=f"submit/cancel: {type(e).__name__}: {e}",
                extra={"submitted": submitted, "cancelled": cancelled})
        return LiveFleetResult(
            status="ERROR", condition_id=cid, title=title, why=why,
            intents=list(intents), submitted=submitted, cancelled=cancelled,
            error=f"submit/cancel: {type(e).__name__}: {e}")

    emit_fn(service="fleet", cycle=cycle, phase="quoting", action="submit",
            market_slug=title,
            extra={"submitted": submitted, "cancelled": cancelled})
    return LiveFleetResult(
        status="QUOTED" if intents else "DECLINED",
        condition_id=cid, title=title, why=why, intents=list(intents),
        submitted=submitted, cancelled=cancelled)


# --- production wiring ------------------------------------------------------

def _market_specs(max_markets: Optional[int] = None) -> list[dict]:
    """Graduated markets as per-market dict specs, mirroring fleet.MarketState."""
    from engine.market_feed import load_graduated_markets
    gms = load_graduated_markets()
    if max_markets:
        gms = gms[:max_markets]
    return [{
        "cid": gm.cid,
        "min_size": gm.min_size,
        "shares": gm.shares,
        "max_spread": gm.max_spread,
        "tick": gm.tick,
        "daily": gm.daily,
        "title": gm.title,
        "slug": gm.slug,
    } for gm in gms]


def _fetch_market(cid: str):
    """Resolve one market on the venue, raising so the loop records ERROR."""
    from engine.markets import fetch_pinned_market
    m = fetch_pinned_market(cid, require_rewards=False)
    if m is None:
        raise LookupError(
            f"no tradeable market at {cid[:16]}... (missing, closed, or not 2 tokens)")
    return m


def _make_inventory_fn(registry, db_path: Path):
    def inventory_fn(market) -> Inventory:
        from engine.order_registry import inventory_from_registry
        return inventory_from_registry(
            market.condition_id, market.up_token, market.down_token,
            db_path=db_path)
    return inventory_fn


def _make_open_orders_fn(registry):
    """Open orders for a market, shaped for plan_orders (token/price/order_id/side)."""
    def open_orders_fn(market) -> list[dict]:
        out = []
        for o in registry.get_active_orders():
            if o.condition_id != market.condition_id or o.status != "open":
                continue
            out.append({
                "token_id": o.token_id,
                "price": o.price,
                "order_id": o.order_id or o.id,
                "id": o.id,
                "side": o.side,
                "status": o.status,
            })
        return out
    return open_orders_fn


def _submit_intents(client, registry, market, intents, cfg) -> int:
    """Place decided intents as BUY orders, reusing `live_exec.quote`'s discipline.

    Row first, then send: a registry row is written before the venue call so a
    crash in between leaves a `pending` row that reconcile's orphan adoption can
    claim, never an untracked live order. Passive legs batch-post post_only;
    crossed (emergency-hedge) legs are the one place this strategy takes
    liquidity, so they post as FOK.

    A couple whose legs split (one accepted, one rejected) is rolled back by
    cancelling the survivor -- a lone resting leg is a naked position taken on
    purpose. Rows the venue never acknowledged are marked cancelled, never left
    half-open.
    """
    from py_clob_client_v2.clob_types import (
        OrderArgsV2, OrderPayload, OrderType, PostOrdersV2Args,
    )
    from py_clob_client_v2.order_builder.constants import BUY
    from engine.venue import (
        MAX_ORDER_USD, MAX_TOTAL_USD, open_notional, venue_order_id,
    )
    from engine.order_registry import OrderRecord, QuoteRecord, get_run_id

    if not intents:
        return 0

    total_cost = sum(i.price * i.size for i in intents)
    for i in intents:
        if i.price * i.size > MAX_ORDER_USD:
            raise RuntimeError(
                f"leg {i.side} ${i.price * i.size:.2f} exceeds "
                f"MAX_ORDER_USD ${MAX_ORDER_USD:.2f}")
    already = open_notional(client)
    if already + total_cost > MAX_TOTAL_USD:
        raise RuntimeError(
            f"open ${already:.2f} + ${total_cost:.2f} exceeds "
            f"MAX_TOTAL_USD ${MAX_TOTAL_USD:.2f}")

    now_ms = int(time.time() * 1000)
    pair_id = f"pair-{uuid.uuid4().hex[:12]}"
    max_pair_cost = getattr(cfg, "max_pair_cost", 0.995)

    passive = [i for i in intents if not i.crossed]
    crossed = [i for i in intents if i.crossed]

    placed = 0
    for batch, order_type, post_only in (
        (passive, OrderType.GTC, True),
        (crossed, OrderType.FOK, False),
    ):
        if not batch:
            continue

        local_legs = []
        batch_args = []
        for i in batch:
            local_id = str(uuid.uuid4())
            registry.create_order(OrderRecord(
                id=local_id, order_id=None, condition_id=market.condition_id,
                token_id=str(i.token_id), side="BUY", price=i.price,
                original_size=i.size, status="pending",
                posted_ts=now_ms, last_polled_ts=now_ms,
                pair_id=pair_id, max_pair_cost_at_post=max_pair_cost,
            ))
            signed = client.create_order(OrderArgsV2(
                price=i.price, size=i.size, side=BUY, token_id=i.token_id,
                expiration=0))
            batch_args.append(PostOrdersV2Args(order=signed, orderType=order_type))
            local_legs.append({
                "local_id": local_id, "token_id": str(i.token_id),
                "price": i.price, "size": i.size, "side": i.side,
                "mid": i.mid, "edge_vs_mid": i.edge_vs_mid, "crossed": i.crossed,
            })

        t_start = time.perf_counter()
        resp = client.post_orders(batch_args, post_only=post_only)
        post_latency_ms = (time.perf_counter() - t_start) * 1000.0
        resp_list = (resp if isinstance(resp, list)
                     else [resp] if isinstance(resp, dict) else [])

        extracted = []
        for idx, leg in enumerate(local_legs):
            item = resp_list[idx] if idx < len(resp_list) else None
            extracted.append(venue_order_id(item))

        ok = sum(1 for v in extracted if v is not None)

        # PARTIAL FAILURE: a couple that split must not leave a naked survivor.
        if ok == 0:
            for leg in local_legs:
                registry.update_order_status(
                    leg["local_id"], status="cancelled", last_polled_ts=now_ms)
            log.warning("no order ids in post response for %s; rows cancelled",
                        market.condition_id[:12])
            continue

        if 0 < ok < len(local_legs):
            for idx, leg in enumerate(local_legs):
                v_id = extracted[idx]
                if v_id is not None:
                    try:
                        client.cancel_order(OrderPayload(orderID=v_id))
                    except Exception as e:
                        log.warning("rollback cancel of %s failed: %s", v_id, e)
                registry.update_order_status(
                    leg["local_id"], status="cancelled", last_polled_ts=now_ms)
            raise RuntimeError(
                f"partial post for {market.condition_id[:12]}: "
                f"{ok}/{len(local_legs)} legs accepted; survivors cancelled")

        # Full agreement: commit venue ids and log the quote telemetry.
        for idx, leg in enumerate(local_legs):
            v_id = extracted[idx]
            registry.attach_venue_order_id(
                leg["local_id"], v_id, status="open", last_polled_ts=now_ms)
            registry.log_quote(QuoteRecord(
                ts=time.time(), market_slug=market.market_slug,
                condition_id=market.condition_id, token_id=leg["token_id"],
                side=leg["side"], price=leg["price"], size=leg["size"],
                mid=leg["mid"], edge_vs_mid=leg["edge_vs_mid"],
                order_id=v_id, local_id=leg["local_id"], run_id=get_run_id(),
                latency_ms=post_latency_ms,
            ))
            placed += 1

    return placed


def _cancel_orders(client, registry, orders) -> int:
    """Cancel resting orders and mark the matching registry rows cancelled."""
    from py_clob_client_v2.clob_types import OrderPayload

    now_ms = int(time.time() * 1000)
    cancelled = 0
    for o in orders:
        v_id = o.get("order_id") or o.get("id")
        row_id = o.get("id")
        try:
            client.cancel_order(OrderPayload(orderID=v_id))
        except Exception as e:
            log.warning("cancel %s failed: %s", v_id, e)
            continue
        for row in registry.get_active_orders():
            if row.order_id == v_id or (row_id and row.id == row_id):
                registry.update_order_status(row.id, status="cancelled",
                                             last_polled_ts=now_ms)
                break
        cancelled += 1
    return cancelled


def _make_sweep_fn(funder: Optional[str], db_path: Path, registry):
    """Sweep the account and log a float mark, without failing the loop."""
    def sweep_fn() -> None:
        from engine.account import log_float_mark_if_measured
        from engine.live_exec import account_sweep
        if not funder:
            return
        mark = account_sweep(funder=funder, db_path=str(db_path), quiet=True)
        log_float_mark_if_measured(registry, mark)
    return sweep_fn


def _fleet_state(registry, cfg) -> dict:
    """The fleet-wide aggregates `decide_quotes` gates on, read once per cycle.

    `run` recomputes these before every rotation and merges them into the base
    config, so the fleet-level gates inside `decide_quotes` see live numbers
    instead of their 0.0 / NORMAL defaults. A failure raises to `run`, which
    keeps the previous cycle's values -- never resetting a live cap to open on
    a bad read.
    """
    from engine.gate import fleet_posture
    from engine.markout import fleet_stats
    from engine.order_registry import (
        registry_committed_usd, registry_naked_usd,
    )

    return {
        "fleet_naked_usd": registry_naked_usd(registry),
        "committed_usd": registry_committed_usd(registry),
        "fleet_posture": fleet_posture(
            fleet_stats(registry, cfg.markout_fleet_min_sample), cfg),
    }


def main(argv: Optional[list[str]] = None) -> int:
    """The live equivalent of `python -m strategy.fleet`.

    Dry-run by default; `--live` places real orders. `--once` runs a single
    rotation (the smoke-test path); without it the loop runs until interrupted.
    """
    from engine.config import load
    from engine.markets import full_book
    from engine.order_registry import DEFAULT_DB_PATH, OrderRegistry
    from engine.order_registry import reconcile_orders
    from engine.quotes import decide_quotes

    ap = argparse.ArgumentParser(
        description="LIVE fleet: decide -> submit -> reconcile across graduated markets.")
    ap.add_argument("--live", action="store_true",
                    help="actually send. Without it, everything is a dry run.")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="rotation cadence in seconds (default: 5.0)")
    ap.add_argument("--once", action="store_true",
                    help="run one rotation and exit")
    ap.add_argument("--db", default=None,
                    help="registry db path (default: run/live.db)")
    ap.add_argument("--max-markets", type=int, default=None,
                    help="cap the number of markets rotated (default: all)")
    ap.add_argument("--funder", default=None,
                    help="funder address (default: POLY_FUNDER)")
    ap.add_argument("--no-reconcile", action="store_true",
                    help="skip the reconcile pass (the poll loop owns it when "
                         "running alongside this fleet)")
    ap.add_argument("--no-sweep", action="store_true",
                    help="skip the account sweep (the poll loop owns it when "
                         "running alongside this fleet)")
    a = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stderr)],
    )

    cfg = load()
    try:
        from engine.account import fetch_live_balance
        live_bal = fetch_live_balance(a.funder)
        if live_bal is not None and live_bal > 0:
            cfg = replace(cfg, bankroll_usd=live_bal)
    except Exception as e:
        log.warning("live balance read failed, using config bankroll: %s", e)

    specs = _market_specs(a.max_markets)
    if not specs:
        log.warning("no graduated markets in run/markets.json; "
                    "run scripts/rank_markets.py first -- idling")
    else:
        log.info("rotating %d markets (live=%s interval=%ss once=%s)",
                 len(specs), a.live, a.interval, a.once)

    db_path = Path(a.db) if a.db else DEFAULT_DB_PATH
    registry = OrderRegistry(db_path=db_path)
    maker = a.funder or os.environ.get("POLY_FUNDER")

    if a.live:
        from engine.venue import client
        client = client(a.funder)
    elif os.environ.get("POLY_PRIVATE_KEY") or os.environ.get("POLY_KEY"):
        # Dry-run with credentials: reconcile and sweep (read-only) can run.
        from engine.venue import client
        client = client(a.funder)
    else:
        log.info("no credentials in env: dry-run will skip reconcile/sweep "
                 "(they need auth) and only show decide/plan outcomes")
        client = object()

    seam = VenueSeam(
        client=client,
        registry=registry,
        base_cfg=cfg,
        maker_address=maker,
        clob_host=os.environ.get("CLOB_HOST", "https://clob.polymarket.com"),
        fetch_market=_fetch_market,
        fetch_books=full_book,
        decide=decide_quotes,
        submit_fn=_submit_intents,
        cancel_fn=_cancel_orders,
        reconcile_fn=(
            (lambda c, r, m: None) if a.no_reconcile
            else (lambda c, r, m: reconcile_orders(c, r, maker_address=m))
        ),
        sweep_fn=(
            (lambda: None) if a.no_sweep
            else _make_sweep_fn(maker, db_path, registry)
        ),
        inventory_fn=_make_inventory_fn(registry, db_path),
        open_orders_fn=_make_open_orders_fn(registry),
        fleet_state_fn=lambda r: _fleet_state(r, cfg),
        emit_fn=partial(_emit_cycle_event, db_path=db_path),
    )
    results = run(
        seam,
        interval=a.interval, once=a.once, live=a.live, markets=specs,
    )
