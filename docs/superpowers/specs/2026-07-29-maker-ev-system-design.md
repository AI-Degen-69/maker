# Maker fleet: EV measurement, allocation and quality gating

**Date:** 2026-07-29
**Status:** approved, not yet implemented

## Problem

The fleet projects $52/day of rent on $2,340 of committed capital. That figure
is one half of an equation:

```
EV/day  =  rent/day  −  expected loss from unpaired fills/day
```

The first term is measured. The second term is **not measured at all**. Every
decision the bot currently makes optimises the term we can see and ignores the
term that can bankrupt it.

Two facts make this urgent rather than theoretical:

1. In 40 unattended minutes the fleet accumulated $1,333 of unhedged exposure
   against $47/day of rent. A hard naked-share cap (`max_naked_shares = 360`)
   has since shipped and is verified live; it bounds the damage but says
   nothing about whether the fills were good.
2. These markets resolve in 2026–2027. Settlement-based P&L will read $0.00 for
   months, so waiting for realized results is not a strategy.

The objective is long-run expected value, not daily drawdown control. The user
was explicit: bad days do not matter, EV does.

## Non-goals

- Raising the $52/day headline. It is already there in projection.
- Guaranteeing $52/day. Rent is predictable; fill losses are not. This design
  makes the unknown term visible and actionable, nothing more.
- Replacing the naked-share cap. Markout is the slow signal, the cap is the
  fast one.

## Component 1: markout meter

**Purpose:** estimate the cost of being filled, within hours instead of years.

New module `strategy/markout.py`, new `markouts` table.

On every fill, record the mid at fill time. On subsequent cycles, record the
market's mid at fixed horizons (+5m, +1h, +6h). For a buy:

```
markout = (mid_later − fill_price) × shares
```

Buy at 0.57, mid drifts to 0.55: that fill lost 2c/share in expectation.
Negative mean markout means our fills are systematically informed against us.

**Critical correctness constraint.** On the best markets we hold a majority of
the resting book (63% on Taylor Swift). Our own quotes move the mid, so a naive
markout measures our own footprint and reports it as edge. The reference mid
MUST exclude our own orders, or be derived from tape prints only. Getting this
wrong makes every number self-referential and the whole system worthless.

**Interface:** `record_fill_mid(fill) -> None`,
`sample_horizons(now) -> None`, `per_market_stats() -> dict[cid, MarkoutStats]`.
Depends on: `store`, book snapshots. Nothing depends on its internals.

## Component 2: allocator

**Purpose:** same income from far less capital.

New module `strategy/allocate.py`. Competitor depth is recoverable from data
already stored in `reward_samples`:

```
T           = C × (1 − share) / share      # competitors' score, in capital terms
income(C)   = daily × C / (C + T)
marginal(C) = daily × T / (C + T)²
```

Water-fill: allocate to the highest marginal return until marginals equalise or
the budget is exhausted; drop markets below a marginal-return floor.

Measured state at design time:

| group | capital | rent/day |
|---|---|---|
| top 10 markets | $1,145 | $44.55 |
| bottom 10 markets | $1,196 | $6.45 |

Returns span 27.58%/day to 0.28%/day on identical $115 stakes, because
`quote_shares = 120` is a flat constant applied regardless of expected return.

**Hard ceiling:** a market cannot pay more than its pot. Taylor Swift's pot is
$50/day and we already take 63%. Adding $1,150 there yields only ~$47/day.
Concentration has sharply diminishing returns; the allocator must model this
rather than chase the top market.

**Expected outcome:** ~$45–50/day on ~$1,200 committed, versus $52/day on
$2,340 today — roughly double the return on capital, not double the income.
Deploying the freed capital buys ~$5/day for ~$1,000 and ten markets of extra
fill risk, which is a bad trade.

**Interface:** `allocate(markets, budget) -> dict[cid, shares]`. Pure function
of measured state; no I/O, independently testable.

## Component 3: quality gate

**Purpose:** act on markout automatically, without evicting good markets on noise.

Per-market state machine in the quoting path:

```
NORMAL  --markout < −threshold, n ≥ min_sample-->  WIDENED (offset 2c → 3.5c)
WIDENED --still negative after min_sample more--> EXITED  (stop quoting)
WIDENED --markout recovers---------------------> NORMAL
```

Widening keeps us inside the reward band, so we keep earning rent while taking
fewer and better-priced fills. Only a market that stays bad after widening is
toxic enough to be worth abandoning the rent for.

`min_sample` matters more than the threshold. On thin, long-dated markets a
3-fill sample is noise, and evicting a sound market on noise costs real rent.
Hysteresis on the recovery edge prevents flapping.

## Starting parameters

Chosen to be explicit rather than correct — every one of them is a hypothesis
to be revised once real markout data exists.

| parameter | start | reasoning |
|---|---|---|
| `markout_horizons` | 5m, 1h, 6h | 5m catches immediate adverse flow; 6h is the shortest horizon on which a long-dated market plausibly repriced |
| `markout_min_sample` | 20 fills | below this the mean is dominated by noise on thin books |
| `markout_widen_threshold` | −0.5c/share | half the ~1c edge a paired quote earns; losing more than that makes the fill unprofitable |
| `widen_offset` | 2.0c → 3.5c | stays inside the 4.5c reward band, so rent continues |
| `marginal_return_floor` | 0.02 $/day per $ | drops anything paying under ~2%/day at the margin; the bottom 10 markets sit far below this |
| `allocation_budget` | $1,200 | ~half of today's $2,340, per the design goal |

## Data flow

```
fills          → markout meter → per-market markout → quality gate → offset/exit
reward_samples → competitor T  → allocator          → shares per market
                                          naked cap → hard exposure floor
```

## Testing

- **Markout:** known fills with known later mids produce known markout values;
  the reference mid provably excludes our own resting size.
- **Allocator:** water-filling equalises marginal returns; respects the budget;
  drops markets below the floor; models the pot ceiling (never allocates past
  the point where marginal return goes negative).
- **Gate:** transitions fire in both directions; refuses to fire below
  `min_sample`; hysteresis prevents oscillation.

## Failure condition

If markout comes back persistently negative across the fleet after an adequate
sample, the honest conclusion is that this strategy does not work. Finding that
out in a week is the point of building this.
