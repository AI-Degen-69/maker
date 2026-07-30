"""Markout: the cost of being filled, measured in hours instead of years.

The fleet reports rent confidently and says nothing about the other half of

    EV/day = rent/day - expected loss from unpaired fills/day

These markets resolve in 2026-2027, so settlement P&L reads $0.00 for months
and cannot answer the question in any useful timeframe. Markout answers it
early: after we were filled, where did the price actually go? A maker who is
systematically filled just before the price moves against him is losing money
no matter how healthy the rent line looks.

THE CORRECTNESS CONSTRAINT. The reference mid must exclude our own resting
size. On the best markets we hold a majority of the book (63% measured on one),
so a mid that included our own orders would measure our own footprint and
report it back as edge -- a number that looks plausible and means nothing.

In paper mode this is automatic: our quotes live in QueueFillEngine and are
never sent to the venue, so the fetched book is already clean. A LIVE run has
no such guarantee and must record `ref_mid_source='contaminated'`, which
excludes those rows from every aggregate below rather than silently poisoning
them.
"""
from __future__ import annotations

import statistics

from strategy import store


def markout_per_share(fill_price: float, mid_later: float, side: str) -> float:
    """Cost of one filled share, in price units.

    We only ever buy, so a mid that sits below our fill price means the fill
    was informed against us. `side` is part of the signature because each side
    is measured against its OWN token's mid -- buying DOWN at 0.38 is scored
    against the DOWN mid, not against 1 minus the UP mid. Once the caller has
    supplied the right mid the arithmetic is identical for both, which is why
    the parameter is not branched on.
    """
    return mid_later - fill_price


def _stats_from_rows(rows: list[dict], min_sample: int) -> dict:
    """Aggregate one market's fills into a verdict.

    Returns `insufficient_sample` rather than a mean when the sample is thin.
    That is the load-bearing behaviour: a three-fill mean on a thin book is
    noise, and the gate consuming this would happily evict a sound market on
    it, forfeiting real rent for an imaginary reason.
    """
    clean = [r for r in rows if r.get("ref_mid_source") != "contaminated"]
    if len(clean) < min_sample:
        return {"n": len(clean), "verdict": "insufficient_sample",
                "mean_per_share": None}
    mean = statistics.mean(r["markout"] for r in clean)
    return {"n": len(clean), "mean_per_share": mean,
            "verdict": "losing" if mean < 0 else "earning"}


def drift_per_share(ref_mid: float, mid_later: float) -> float:
    """How far the market moved AFTER it filled us -- the adverse-selection
    term on its own.

    This is the correction to the original design, and it matters. Total
    markout is `mid_later - fill_price`, which silently includes the ~2c we
    quote under mid. A market whose price never moved therefore reads +2.15c
    and looks like pure edge, and a quality gate built on it could only trip
    if drift exceeded -2.5c -- a catastrophe detector, not the erosion monitor
    it was meant to be.

    Measured live on 2026-07-29: +2.11c captured spread, +0.04c drift. Almost
    all of the apparent edge was our own entry discount handed back to us.
    """
    return mid_later - ref_mid


def _matured(row: dict) -> list[float]:
    """Drift at every horizon already sampled for this fill, in order.

    Deliberately drift and not total: this feeds the gate, and the gate must
    react to the market moving against us, never to our own offset.
    """
    ref = row.get("ref_mid")
    if ref is None:
        return []
    out = []
    for i in range(3):
        mid = row.get(f"mid_h{i}")
        if mid is not None:
            out.append(drift_per_share(ref, mid))
    return out


def per_market_stats(min_sample: int) -> dict[str, dict]:
    """Per-market verdicts, using each fill's LONGEST matured horizon.

    The longest horizon is the honest one: a 5-minute reading on a market that
    resolves in 2027 mostly measures microstructure noise, while the 6-hour
    reading is where genuine repricing on news would show up. Fills with no
    matured horizon yet contribute nothing.
    """
    by: dict[str, list[dict]] = {}
    for r in store.markout_rows():
        matured = _matured(r)
        if not matured:
            continue
        by.setdefault(r["condition_id"], []).append(
            {"markout": matured[-1], "ref_mid_source": r.get("ref_mid_source")})
    return {cid: _stats_from_rows(rows, min_sample) for cid, rows in by.items()}


def sample_due(mids_by_cid: dict, now: float, horizons) -> int:
    """Record the mid at every horizon that has just matured.

    `mids_by_cid` maps condition_id -> {"UP": mid, "DOWN": mid}. Returns how
    many rows were updated so the caller can log progress. A market we have no
    fresh book for is skipped and retried next cycle rather than recorded
    against a stale price.
    """
    n = 0
    for row in store.pending_markouts(now, horizons):
        mids = mids_by_cid.get(row["condition_id"])
        if not mids:
            continue
        mid = mids.get(row["side"])
        if mid is None:
            continue
        i = row["_due"]
        store.close_markout(row["id"], i, mid, last=(i == len(horizons) - 1))
        n += 1
    return n
