---
type: note
title: polymarket-maker
captured_at: '2026-08-05T00:21:54.142Z'
captured_via: capture-cli
ingested_via: put_page
ingested_at: '2026-08-05T00:22:00.477Z'
source_kind: put_page
---

# polymarket-maker

Paper-trading simulation of a **maker** strategy on Polymarket's 5-minute
"Bitcoin Up or Down" market. Instead of crossing the spread it rests bids on
**both** outcomes, aiming to earn the spread and stay inventory-balanced, and
holds to resolution.

**Live dashboard:** https://polymarket-maker-production.up.railway.app

> Simulation only. It never places a real order and loads no wallet
> credentials at all — see [AGENTS.md](AGENTS.md).

## Why this exists

Measured from 56,768 of @powerwinner's fills: he wins only **41.4%** of markets
against a 56.1% breakeven, so he has no directional edge. His gross is
**+$39,884/week** — but **−$32,501** if charged a taker fee. The entire
difference is that he rests orders instead of crossing. That is the mechanism
this repo tests.

## The honest caveat

A maker simulation lives or dies on its fill model. Ours is **queue-aware**,
driven by observed order-book deltas, and its optimistic biases are documented
in [`strategy/fills.py`](strategy/fills.py) rather than hidden. Treat its
output as an **upper bound**. The dashboard shows live progress toward
90/95/99% statistical confidence so the sample size cannot be quietly ignored.

## Layout

    strategy/   engine (quotes, queue-aware fills, kpi, store, net_config)
    server/     dashboard.py (API) + kanban.py (page)
    research/   lab notebook, EN + HE
    deploy/     container entrypoint + preflight

The sibling repo [`polymarket-taker`](https://github.com/AI-Degen-69/polymarket-taker)
uses the same layout.

## Running locally

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
bash scripts/setup-hooks.sh        # required once: research-log enforcement
MAKER_DB=./maker.db .venv/bin/python -m strategy.main
.venv/bin/uvicorn server.dashboard:app --port 8788
```

## Current state — 53 settled markets, conclusively losing

Updated 2026-07-22. **The current run is measuring a known bug, not the
strategy.** Read this before drawing any conclusion about market making.

| metric | value |
|---|---|
| settled markets | 53 (3W / 50L) |
| win rate | 5.7% |
| realized P&L | **−$1,172.07** (equity $3,827.93 from $5,000) |
| ROI on capital | −4.1% |
| median pair cost | **1.0419** |
| pairs under $1.00 | **4%** |
| spread capture | +$263.81 |
| adverse selection | −$1,435.88 |
| fill rate | 37.6% (median 72 shares queued ahead) |
| avg edge vs mid | 0.48¢ (theory: 0.50¢) |

### What the numbers actually say

The mechanism is doing its job: **fill rate 37.6% against real queue depth**,
**0.48¢ captured versus a 0.50¢ theoretical half-spread**, and inventory
**balance 0.99** against a 0.92 target. Execution is fine.

The loss comes from one thing. **Median pair cost is 1.0419 for something that
pays exactly $1.00** — buying a $1.00 payout for $1.04 is a guaranteed ~4% loss
on the hedged portion, and observed ROI is −4.1%. Those two numbers matching is
the proof: this is not adverse selection or bad luck, it is the bot
systematically overpaying for its hedge.

**Cause.** A fix that let the balancing side bypass the pair-cost cap so that
inventory could always be hedged. It succeeded at balance (0.99) and broke
price discipline (only 4% of pairs now clear under $1.00). Logged as `OPEN` in
[research/RESEARCH_LOG.md](research/RESEARCH_LOG.md).

**Fix direction.** Cap the hedge at a price that keeps the resulting pair under
$1.00, and skip the hedge entirely when no such price exists — accepting some
imbalance rather than guaranteed loss.

### On the "statistically conclusive" banner

The dashboard reports 90/95/99% confidence all **reached** at n=53. That is
arithmetically correct — the loss is large and consistent (mean −$22.11,
σ $16.01) — but what has been proven is that *the bug loses money*, which was
already known. **It says nothing about whether the maker strategy works.**
Judging that needs a fresh run after the fix.

## Brain Index

gbrain-linked pages (retrieved via `gbrain think` / `gbrain search`):

- [[maker-ev-system]] — EV system implementation plan
- [[maker-ev-system-design]] — EV system design spec
- [[profit-take-supervisor]] — profit-take supervisor plan
- [[profit-take-supervisor-design]] — profit-take supervisor design spec
- [[he_research_log]] / [[he_research_summary]] — Hebrew research notebook (EN + HE)
- [[research_log]] / [[research_summary]] — research lab notebook
