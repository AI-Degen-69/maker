"""Maker strategy config. Numbers derived from powerwinner's measured fills.

Source: 56,768 of his BTC/ETH 5-min fills over 2026-07-14..21 (2,970 markets).
See research/powerwinner_analysis.md.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class MakerConfig:
    series_slug: str = "btc-up-or-down-5m"

    # --- virtual account --------------------------------------------------
    # Fresh paper run wallet. This is the total simulated capital available,
    # not a promise that the allocator may commit every dollar at once.
    bankroll_usd: float = 100.0

    # --- objective --------------------------------------------------------
    # "pair"    : the original bet -- rest under the ask, try to buy a hedged
    #             pair for under $1.00. Measured dead over 60 markets: quoting
    #             off the ASK puts every quote ~half a spread ABOVE mid, so the
    #             pair costs 1.00 + spread by construction. The book runs at
    #             101% all window; there is nothing to capture.
    # "rewards" : quote for the liquidity-reward score instead of for the fill.
    #             Gamma reports this series as incentivised --
    #               rewardsMaxSpread = 4.5c, rewardsMinSize = 50,
    #               feeSchedule.rebateRate = 0.2
    #             -- and the score is paid on RESTING size, filled or not:
    #               S(v, s) = ((v - s)/v)^2 * size,  v = 4.5c, s = own spread
    #             Two consequences the "pair" objective had backwards:
    #               1. Sitting out is the expensive move. The old gates skipped
    #                  69% of cycles (602/875), earning zero score for them.
    #               2. Quoting off MID rather than the ask makes the pair cost
    #                  1.00 - 2*offset, i.e. automatically under $1.00 -- the
    #                  exact thing the pair objective failed to achieve.
    objective: str = "rewards"

    # How far below mid to rest, in price units. This is the whole tradeoff:
    # score is quadratic in closeness (0.5c -> ~16% of market score, 2c -> ~7%,
    # measured over 3906 recorded legs), but closer means more fills, and fills
    # on this book are adverse. Start back from the touch and walk in only if
    # realised adverse selection stays small.
    reward_offset: float = 0.020

    # --- inventory skew (replaces the settlement cross-hedge) --------------
    # A resting quote that fills leaves us one-sided, and a one-sided position
    # held to settlement is a coin flip on the full $1.00. The old answer was
    # to cross the spread near close, which only ever executed once the outcome
    # was already known (measured hedge prices: 0.01, 0.02) -- it booked luck
    # as profit and protected nothing.
    #
    # The answer that actually works is the standard market-maker one: skew.
    # Long UP -> quote UP further from mid (harder to fill, adds less) and
    # DOWN closer to mid (easier to fill, which FLATTENS us). It runs every
    # cycle from the first share of imbalance, while both sides still cost real
    # money, and it keeps us two-sided so the reward score is preserved.
    # The spring is wound by DOLLARS at risk, not by share count: `skew_offset`
    # scales this cap by `risk_utilization` of the naked side, so it is at full
    # stretch exactly when the dollar budget is full. A share-denominated ramp
    # (240 shares to full stretch, removed) answered a 100-share naked leg with the
    # same push at 0.85, where it is $85 of downside, as at 0.15, where it is
    # $15 -- so on lol-maz-mg1 it was still ramping at 233 of 240 shares while
    # $190.26 was already at stake and the position was fully built.
    max_skew: float = 0.015             # cap, in price units
    min_reward_offset: float = 0.005    # never quote nearer mid than this

    # THE HARD CAP on directional exposure, in DOLLARS of naked cost. This is
    # the only unit the cap is stated in -- a share-denominated twin was
    # removed rather than kept alongside it, because two caps in two units
    # cannot both be the binding constraint and an operator reading "not
    # adding" would have no way to tell which one bound.
    #
    # Skew is a spring and it bottoms out AT this budget: `risk_utilization`
    # clamps at 1.0, so past the cap more exposure produces no more response.
    # Something has to own that range. The share cap that used to own it could
    # not, and the reason is the unit, not the level:
    # on a binary market the downside of one long share IS the price paid for
    # it, so 360 shares permitted $72 of risk at 0.20 and $293 at 0.8152 --
    # loosest exactly where a wrong resolution costs most.
    #
    # Measured 2026-08-05 on lol-maz-mg1: 233.40 UP shares at an average of
    # 0.8152, $190.26 at risk, against a 360-share cap that read 233 and stayed
    # silent while 85% of the fleet's -$223.32 unhedged float sat in that one
    # market. Three limits were armed and none of them bound.
    #
    # $120 binds between 171 shares (at 0.70) and 400 shares (at 0.30) inside
    # the price band, and would have stopped lol-maz-mg1 at roughly 147 shares
    # instead of 233. 0 disables the rule, same as every other cap here -- and
    # it disables the whole dollar system with it, because this number is the
    # denominator of all three rules that read it: the hard block, the skew
    # spring above, and U3's size ladder (`risk.size_for`), which decays resting
    # size as base*(1-utilization)^2 so the last order before the cap is 16% of
    # full size rather than 100% of it.
    max_naked_usd: float = 120.0
    # Switchable so the dollar gates can be measured on their own rather than
    # bundled with the rest of a release -- the same convention as
    # enforce_price_band and enable_emergency_hedge. False makes `hard_block`
    # return None for every side, and nothing else changes.
    enable_hard_blocks: bool = True

    # FLOAT-MARK RETENTION. The fleet writes one fleet-wide open-position mark
    # per sweep (unrealized float, committed dollars, naked residue -- the
    # same totals the dashboard derives at read time). 90 days keeps the
    # Total-equity history deep while bounding the table, instead of growing
    # forever like market_events; the dashboard downsamples to one point per
    # minute anyway, so finer-than-a-minute retention buys nothing.
    float_mark_retention_days: float = 90.0

    # BOOK HEALTH. Three arms, all on ONE token's book.
    #
    # A price this close to either end means the market has decided. There is
    # no spread left to capture and the naked leg a fill would create is
    # already decided against us -- wta-kalinsk-kessler finished quoting 0.999
    # bid against a 0.001 ask, and the position was unhedgeable at any price.
    decided_price: float = 0.02
    # Widest two-sided spread still worth quoting into. Above 2 x
    # max_spread_from_mid (9c) the entire reward window lies INSIDE the
    # spread, so landing in it means being the most exposed order in the book
    # -- measured on a 0.26/0.42 market, six cents better than anyone else.
    # 6c leaves margin under that arithmetic.
    max_book_spread: float = 0.06
    # Summed bid depth below which the book cannot absorb an exit. A proxy,
    # not a measurement of exit liquidity: one aggregated number is the most
    # the recorded book shape supports.
    min_book_depth_sh: float = 200.0

    # EMERGENCY STOP-LOSS. The hard cap above stops us ADDING to the heavy
    # side; it does nothing about the exposure already on the book. Skew is
    # supposed to flatten that with resting orders, and it does -- when someone
    # comes to hit our light-side bid. In a market moving hard against us
    # nobody does: our light-side bid sits under a mid that keeps walking away
    # from it while the heavy leg loses money every tick. That is precisely the
    # case where paying the taker fee is the cheap option.
    #
    # Fraction of max_naked_usd at which the light side is allowed to CROSS the
    # spread instead of resting. The deficit is valued at the HEAVY leg's
    # average cost, which is what the missing hedge is worth: 400 shares short
    # is $80 of exposure at 0.20 and $340 at 0.85, and only one of those is an
    # emergency. Stating the trigger in the same unit as the cap is what keeps
    # it inside the cap at every price.
    #
    # 0.8 puts the exception inside the cap on purpose, so it fires while there
    # is still a hedge to buy rather than at the moment the cap freezes us at
    # maximum exposure.
    emergency_hedge_frac: float = 0.8
    # Switchable so the exception can be measured on its own -- a taker order
    # is the one thing this strategy otherwise never does.
    enable_emergency_hedge: bool = True

    # --- EV system -------------------------------------------------------
    # See docs/superpowers/specs/2026-07-29-maker-ev-system-design.md.
    # Rent is measured; the cost of being filled is not. These parameters run
    # the measurement and the rule that acts on it. Every value is a starting
    # hypothesis, to be revised once real markout data exists.
    #
    # Horizons at which a fill is re-priced, in seconds. 5m catches immediate
    # adverse flow; 6h is the shortest horizon on which a long-dated market
    # plausibly repriced on news.
    #
    # 15m (900s, Session 50) is the EXIT-WINDOW read: the naked-exit
    # counterfactual -- where was the mid 15 minutes after we exited? -- which
    # Session 49 had to infer from the bid ladder. APPENDED LAST, not sorted
    # into place: a horizon maps to its column by position, so inserting it
    # between 5m and 1h would relabel every existing mid_h1/mid_h2 reading on
    # the live DB. Because it matures BEFORE the 1h and 6h horizons while
    # sitting AFTER them in the tuple, readers that want the longest matured
    # horizon must compare durations (never columns), and the sampling loop
    # must not seal a row done when this horizon records.
    markout_horizons: tuple[float, ...] = (300.0, 3600.0, 21600.0, 900.0)
    # Below this many fills the mean is dominated by noise on a thin book, and
    # evicting a sound market on noise costs real rent.
    #
    # Was 20, which was unreachable. Measured over the 2026-08-02 fleet run:
    # 58 fills produced 47 markout rows across 18 markets, and the BEST
    # sampled market matured 7. `per_market_stats` therefore returned
    # `insufficient_sample` for every market on every cycle, `next_state`
    # returns the state unchanged on that verdict, and `gate_state` stayed
    # NORMAL for the entire run -- `market_gate` finished with zero rows. A
    # gate that cannot fire is not a conservative gate, it is an absent one.
    # 8 keeps a real noise floor while letting the state machine run.
    markout_min_sample: int = 8
    # FLEET-WIDE markout sample. The per-market number above is thin by
    # nature on a universe that rotates daily, so a market can be toxic for
    # its whole life without ever qualifying for a verdict of its own. The
    # fleet aggregate matures far faster -- n=42 against a per-market best of
    # 7 on the same run -- so a market with no verdict of its own inherits
    # the fleet's rather than defaulting to NORMAL.
    #
    # Higher than the per-market floor on purpose: this reading gates every
    # market at once, so acting on it early is the more expensive mistake.
    markout_fleet_min_sample: int = 25
    # Half the ~1c edge a paired quote earns: losing more than this per share
    # means the fill was unprofitable before inventory risk even starts.
    markout_widen_threshold: float = -0.005
    # CATASTROPHIC markout. The graduated path (NORMAL -> WIDENED -> EXITED)
    # exists because one cent of mispricing and genuine toxic flow look alike
    # on a single reading -- but that argument only holds for SMALL losses. A
    # mean of -2c/share is four times the widen threshold and larger than a
    # full taker fee: no amount of backing off recovers it, and the second full
    # sample the WIDENED path demands is bought with real money. Past this
    # magnitude the gate exits immediately, skipping WIDENED and the doubled
    # sample requirement. The insufficient_sample guard still applies -- this
    # is a magnitude bypass, not a noise bypass.
    markout_catastrophic_threshold: float = -0.020
    # Widened quotes stay inside the 4.5c reward window, so rent continues.
    widen_offset: float = 0.035
    # Marginal $/day per $ committed, below which capital is better left idle.
    marginal_return_floor: float = 0.02
    # Leave wallet headroom for inventory and order-lifecycle timing.
    # Reference scale: 90% of the $1,000 simulation bankroll. The live run
    # overrides this via SPREAD_HUNTER_BANKROLL (load() sets allocation_budget
    # = bankroll * 0.9), so a $100 bankroll yields a $90 budget automatically.
    allocation_budget: float = 900.0
    # Ceiling on any ONE market's share of that budget.
    #
    # The water-fill was written as a diversifier and is not one. `marginal`
    # is daily*T/(capital+T)^2, which is nearly FLAT in capital whenever
    # competitor depth T dominates our size -- so the argmax never changes
    # and a single market absorbs every increment. Measured 2026-08-02: one
    # market took the full $900 budget, `shares_for` turned it into a
    # 900-share order, and it filled in one print for $792 -- 79% of a
    # $1,000 wallet, 1.98x the $400 per-market cost cap.
    #
    # 0.15 puts the floor at roughly seven concurrently funded markets. That
    # number is set by the variance, not by taste: per-fill markout measured
    # -$7.58 with a standard deviation of $56.68, so at full concentration a
    # single fill moves the book by more than the entire expected edge of a
    # hundred fills and no mean is readable at any sample size.
    max_market_frac: float = 0.15
    # Set per-market by the fleet each cycle: NORMAL | WIDENED | EXITED.
    gate_state: str = "NORMAL"

    # --- profit taking ----------------------------------------------------
    # Nothing in this strategy ever closed a position: every fill rode to
    # settlement in 2027, so a filled pair immobilised capital that had been
    # earning daily rent. Selling a pair that has appreciated converts locked
    # capital back into working capital.
    #
    # Selling means crossing the spread, so BOTH legs pay the taker fee. The
    # move therefore has to clear two fees before it clears anything else.
    profit_take_fee_per_share: float = 0.017

    # MERGE (U2). Total cost of one mergePositions transaction, in dollars --
    # per TRANSACTION, not per share, because a merge costs the same whether it
    # redeems ten pairs or ten thousand. That is why the economic floor is on
    # total gain rather than per-share gain.
    #
    # A seeded estimate, not a measurement. Polygon gas for a CTF merge is
    # cents, and this is deliberately set high enough to be conservative in
    # Phase A, where no transaction has been sent and nothing on-chain has been
    # verified. U6's verify_merge.py replaces it with the real figure from an
    # actual transaction, and U5 reads it to compute the minimum economic size.
    #
    # Never set this to 0. Zero-cost gas makes every merge look profitable,
    # including ones that lose money -- `strategy/merge.py` treats None as
    # blocking for the same reason.
    merge_gas_usd: float = 0.05

    # OVER-PARITY MERGES (U5, KTD2b). 4.5% of measured pairs cost more than
    # 1.00, so merging them books an immediate loss. Holding is not obviously
    # better: the pair pays exactly 1.00 either way, so the nominal comparison
    # is a wash and the real question is what the freed capital earns in the
    # meantime.
    #
    # The velocity test answers that -- merge when projected rent on the
    # released capital over the remaining hold beats the concession plus gas.
    # This is the hard bound around it. Checked BEFORE the velocity arithmetic
    # runs, and never yielded to: without it, a large projected-rent figure
    # would license an arbitrarily bad exit price, which is how a capital
    # efficiency rule turns into an inventory fire sale.
    #
    # 1c/share. Roughly a third of what selling the same pair would pay in
    # taker fees, so the exception stays cheaper than the alternative it
    # replaces.
    merge_max_loss_per_share: float = 0.01

    # How long the freed capital is assumed to keep earning, in days, when
    # pricing the velocity exception above. NOT the time to resolution --
    # markets here settle in 2027, and crediting ~500 days of rent would make
    # every over-parity pair mergeable regardless of price, which is precisely
    # what merge_max_loss_per_share exists to stop.
    #
    # 30 days is deliberately short: long enough that freeing capital is worth
    # something, short enough that the exception stays rare and the loss cap
    # remains the binding constraint rather than a formality.
    merge_velocity_hold_days: float = 30.0

    # REWARD ELIGIBILITY (U4). Polymarket's published rule: "The minimum reward
    # payout is $1; amounts below this will not be paid." A market projecting
    # under a dollar a day does not pay a fraction of a dollar -- it pays zero.
    #
    # Measured 2026-07-30, only 4 of 20 fleet markets cleared it. The other 16
    # held capital and earned exactly nothing, which makes spreading thin
    # actively harmful rather than merely inefficient. Concentration is not a
    # preference here, it is what the payout rule requires.
    #
    # Whether the threshold applies per-market or across a maker's aggregate is
    # not settled by the docs. Per-market is the conservative reading and is
    # safe under either; the first real payout settles it empirically.
    reward_min_payout_usd: float = 1.0
    # Multiple of the floor a market must clear to be funded. Projections are
    # noisy and competitors arrive, so 1.0x would fund markets that cross below
    # the line the moment anyone else quotes. 1.5x buys headroom.
    reward_floor_multiple: float = 1.5

    # SPREAD CAPTURE (U6). The payout floor above, and the sizing that feeds
    # it, assume income arrives as reward emissions. The markets that actually
    # trade -- bitcoin-up-or-down-*, ~$92k/24h -- publish clobRewards: 0, so
    # the allocator valued them at exactly zero and the fleet instead funded a
    # universe that printed 9 tape-backed fills in 74 hours.
    #
    # For those markets the income IS the spread, and
    # `allocate.spread_capture_daily` converts it into the same $/day pot the
    # reward path uses. These two are its inputs.
    #
    # Fraction of the quoted spread earned per share traded. A resting maker
    # earns at MOST half the spread on a round trip, and less in practice,
    # because flow that reaches a resting order is adversely selected. This is
    # a starting hypothesis at half of that theoretical half -- to be replaced
    # by the first real markout sample on one of these markets, exactly as
    # every other EV parameter here is.
    spread_capture_frac: float = 0.25
    # Fallback quoted spread when the market spec carries none. 1c is the
    # observed book on the up-or-down series. The estimate is most sensitive to
    # this number, so a spec reporting its own spread is always preferred.
    spread_capture_default_spread: float = 0.01

    # TRADABILITY AND HORIZON (U6). Reward yield per dollar of capital prefers
    # a thin book by construction, and a thin book is thin because nobody
    # trades it. Ranking on that metric alone selected a universe that, over
    # 11.6h, printed 48 trades across 20 markets and never traded at all in 9
    # of them -- so 74 hours of running produced 9 tape-backed fills.
    #
    # A market that does not trade cannot fill a resting order. That is not an
    # argument against reward farming, which is a real strategy earning real
    # emissions; it is an argument against measuring reward farming with
    # fill-based instruments and reading the zeros as a maker result.
    # HARD MARKET SELECTOR. These are intentionally stricter than the older
    # $25k research gate: a market must have enough real flow to make a resting
    # quote reachable and enough immediate exit liquidity to make a naked fill
    # survivable. The selector requires this depth independently on YES and NO.
    select_min_volume_24h_usd: float = 250_000.0
    # DEPTH AND SPREAD, ALIGNED TO THE LIVE GATE (2026-08-06). The entry
    # pre-filter was stricter than the continuous protection that actually
    # governs every quote decision: `risk.book_health` (below) requires only
    # 200 SHARES of depth (roughly $100 notional at a 0.50 mid) and a 0.06
    # spread, checked on EVERY quote, not once at pick time. $5,000 was an
    # 41x margin over the $120 `max_naked_usd` worst-case position this depth
    # exists to make exitable -- comfortable, but double-gating against a live
    # system that already refuses to add to a bad book, refuses an untradeable
    # hedge leg, and caps naked cost independent of how the market was picked.
    # Measured 2026-08-06: of 189 scored candidates in a live off-peak read,
    # ~120 failed on YES-side depth alone, most by a wide margin (not a close
    # call at $5,000 -- either well over or an order of magnitude under), so
    # this mainly widens the CANDIDATE POOL rather than admitting marginal
    # books. Set to the same bar the live system already enforces rather than
    # a stricter, redundant one: $1,000 (8x the $120 worst case) and 0.06.
    select_min_top3_depth_usd: float = 1_000.0
    # DEPTH-GATE TRIAL (U32). When set, the RANKER gates on this bar instead of
    # `select_min_top3_depth_usd` -- a controlled loosening licensed by the
    # near-miss tracker (READY_TO_TRIAL: 29 unique markets, 19 with measured
    # depth >= half the bar). The permanent bar above never changes; the trial
    # is opt-in per rank run, adopted markets are tagged `trial_depth_usd` in
    # run/markets.json, and their markouts are watched before the change is
    # made permanent. Overridable from HUNTER_DEPTH_TRIAL_USD; the ranker's own
    # `--trial-depth` flag wins over both.
    select_min_top3_depth_usd_trial: float | None = None
    # VOLUME-GATE TRIAL (U36). Same staging contract as the depth trial above:
    # when set, the RANKER gates 24h volume on this bar instead of
    # `select_min_volume_24h_usd`; adopted markets are tagged `trial_volume_usd`
    # and their markouts are the decision evidence. Measured 2026-08-10 funnel
    # audit: of 54 markets that cleared depth but failed volume, only 2 would
    # fund even with the gate fully lifted, and the top of that population
    # sits at $235-242k -- so the honest trial bar is $200k (a 20% staged
    # loosening), NOT the half-bar $125k the depth tracker's convention would
    # suggest (U33 measured the volume-reject population 1-3 orders of
    # magnitude under the bar; a $125k bar admits nothing). The depth trial
    # alone ($1,000 -> $750) admits 0 additional eligible markets today -- the
    # depth near-miss population has 3-25x-thin volume -- which is why the
    # volume trial is the real lever, and the two run together so a
    # $200k-volume market with a $750-depth book is admissible. Overridable
    # from HUNTER_VOLUME_TRIAL_USD; the ranker's own `--trial-volume` flag wins.
    select_min_volume_24h_usd_trial: float | None = None
    select_max_book_spread: float = 0.06
    # 30 days admits liquid macro, sports, and political markets while keeping
    # long-dated 2027 markets excluded.
    select_max_days_to_resolve: float = 30.0

    # How long to average competitor depth before sizing a position. One
    # snapshot sized the whole fleet on 2026-07-29 and read a competing score
    # of 35 for a market that measured 3,727 live -- a 100x error that put the
    # top-ranked market at $0.25/day actual against $18.96 projected.
    rank_sample_window_sec: float = 1800.0
    # Re-rank cadence. run/markets.json was frozen from 2026-07-29 01:39 while
    # the fleet ran against it for a day and a half.
    rerank_interval_sec: float = 600.0
    # Required profit per share AFTER both fees. Set at roughly one fee's
    # width again, so a close is only taken on a move clearly larger than the
    # cost of taking it -- at 1c the threshold sits inside the noise of a
    # one-tick book flicker and would close positions on nothing.
    profit_take_net_threshold: float = 0.020

    # OPPORTUNITY COST. The threshold above prices a close against zero -- it
    # asks "is closing better than holding?", never "is this dollar better
    # spent somewhere else?". When the allocator is budget-bound while markets
    # well above the marginal floor go underfunded, holding a stagnant pair
    # costs the return that dollar would have earned in the starved market, and
    # that cost is real whether or not the pair itself is up.
    #
    # Under scarcity the required net drops to a SLIGHTLY NEGATIVE number: pay
    # half a cent per share to free capital that can earn multiples of it per
    # day elsewhere. Deliberately small -- this releases capital, it does not
    # authorise dumping inventory at any price.
    scarcity_close_threshold: float = -0.005
    # How far above the marginal floor a market's return must still sit, with
    # the budget exhausted, before capital counts as scarce. At 1.0 any fully
    # spent budget would trip it, which would make the relaxed threshold the
    # normal case rather than the exception.
    scarcity_marginal_multiple: float = 2.0
    # Set per-cycle by the fleet allocator, same mechanism as gate_state and
    # fleet_naked_usd. False here so a single-market bot (archived on the
    # legacy-bot-8788 branch) is unaffected -- it has no allocator and therefore
    # no scarcity to report.
    capital_scarce: bool = False

    # FLEET-WIDE exposure ceiling, in dollars of unhedged cost.
    #
    # max_naked_usd bounds ONE market. It works -- and it is not enough.
    # Measured 2026-07-29: 16 markets, every one inside its own 360-share cap,
    # summing to $1,630 of unhedged exposure. Expected value on that book was
    # +$62 with a standard deviation of +/-$456 -- the variance is seven times
    # the edge. A per-market cap cannot see that, because no single market is
    # misbehaving.
    #
    # $800 is roughly half the observed exposure, which halves the swing while
    # leaving room for the light side to keep flattening. It costs rent:
    # capped markets quote one-sided and score at 1/c, c=3.0.
    max_fleet_naked_usd: float = 400.0
    # Current fleet-wide unhedged cost, injected each cycle by the fleet
    # runner. Zero here so a single-market bot (archived on the legacy-bot-8788
    # branch) is unaffected -- it has no fleet to be over budget.
    fleet_naked_usd: float = 0.0

    # TOTAL COMMITTED CAPITAL (U3). Everything above bounds the UNHEDGED leg;
    # nothing bounded the hedged one. A matched pair cannot lose -- it pays
    # exactly $1 -- so it was treated as free, and it is not: until U2 it was
    # frozen until 2027, and even with merge it is money that is committed
    # right now and cannot be committed anywhere else.
    #
    # The real ceiling was max_cost_per_market x markets = $400 x 20 = $8,000
    # against a nominal $1,200 allocation_budget. Measured 2026-07-30: $9,588
    # had left the wallet -- $7,452 in paired inventory, $767 naked, $1,369
    # resting in offers -- while the dashboard's headline return divided rent
    # by the $1,369 alone and reported 1.80%/day. Against the money actually
    # committed it was 0.256%/day. The cap and the honest denominator are the
    # same fix.
    #
    # Counts inventory cost PLUS resting offer notional, because both are
    # dollars that are spoken for. $2,000 leaves room above the observed
    # working set without permitting another $9.5k drift.
    # Default reference scale is $1,000 (the simulation's historical wallet);
    # the live $100 run overrides this to $100 via SPREAD_HUNTER_BANKROLL
    # (load() sets max_committed_usd = bankroll).
    max_committed_usd: float = 1000.0
    # Injected each cycle by the fleet runner, same pattern as fleet_naked_usd.
    # Zero for a single-market bot, which has no fleet to total up.
    committed_usd: float = 0.0

    # FLEET CIRCUIT BREAKER (U6): NORMAL | WIDENED | HALTED, derived once per
    # sweep from the POOLED markout by `gate.fleet_posture` and injected here
    # by the fleet runner, same per-cycle mechanism as `gate_state` above.
    #
    # Every cap above bounds a QUANTITY -- dollars naked, dollars committed,
    # shares per order -- and none of them reads whether the fills being bought
    # are any good. This one does, and it is the only fleet-wide rule that can
    # stop us adding while every individual market still looks healthy.
    #
    # Deliberately NOT persisted, unlike EXITED: it describes the current
    # pooled reading rather than judging a market, so it is re-derived every
    # sweep and lifts by itself when the pool recovers. NORMAL here so a
    # single-market bot (archived on the legacy-bot-8788 branch) is unaffected
    # -- it has no fleet whose markout could be pooled.
    fleet_posture: str = "NORMAL"

    # Maker pool per 5-min window, for turning score-share into dollars.
    # Measured 2026-07-28 from 15 recorded windows: 68405 shares traded per
    # window -> $716 median taker-fee pool -> 20% = $143 to makers. Treat as an
    # order of magnitude, not a promise: data-api /trades reports each
    # participant's own side, so the volume behind it may be double-counted,
    # in which case the true pool is nearer $70.
    est_reward_pool_usd: float = 143.0

    # --- quoting ----------------------------------------------------------
    # He posts on BOTH outcomes. In a binary market a bid on DOWN at 0.40 is
    # economically a sell of UP at 0.60, so bidding both sides IS two-sided
    # market making expressed as buys only. He never sells (0 SELLs in 56,768).
    quote_both_sides: bool = True

    # How far below the best ask to rest. 1 tick = passive, at the touch.
    # Deeper = better price but far lower fill probability.
    ticks_below_ask: int = 1
    tick_size: float = 0.01
    # The venue's real minimum tick, from Gamma: orderPriceMinTickSize = 0.001.
    # The 1c book spread is therefore TEN ticks, not one -- `tick_size` above is
    # a spread-width proxy the "pair" objective used, not the venue tick. Reward
    # prices round to this so we can sit at a genuine price level.
    price_tick: float = 0.001

    # Rebate qualification (research/btc_5min_market_spec.md):
    #   rewardsMinSize = 50 shares, rewardsMaxSpread = 4.5c from mid.
    # Quotes outside these earn no rebate, so they must not be posted casually.
    min_quote_shares: int = 50
    max_spread_from_mid: float = 0.045
    # QUEUE-DEPTH GATE (harden-to-reality). On live Polymarket a resting quote
    # only fills when it is at or near the FRONT of the book; a quote posted
    # behind thousands of shares almost never fills, and when it does it is
    # because a sweep blew through the entire queue -- i.e. informed flow hit
    # it, which is adverse selection. Measured 2026-08-23: quotes at
    # queue_ahead=0 filled 6-12%, at 50+ shares <3%. Default 50 shares: refuse
    # to rest when the book is deeper than that at our price. Set 0 to disable.
    max_rest_queue_ahead: float = 50.0

    # His fill sizes: median 120sh, p10 20, p90 160. 61% were >=50sh.
    quote_shares: int = 120

    # --- powerwinner's two entry rules ------------------------------------
    # PRICE BAND. 54% of powerwinner's BTC 5-min volume enters at 0.30-0.70, and
    # he has ZERO trades at 0.98+. That is where the spread -- and the taker fee
    # he is avoiding, fee = 0.07*p*(1-p), which peaks at p=0.50 -- are widest, so
    # it is where being the maker is worth most. Outside the band the spread
    # collapses toward one tick on a near-certain outcome and there is nothing to
    # capture, while the downside stays the full $1.00.
    #
    # WIDENED 0.30-0.70 -> 0.10-0.90 (2026-08-11). The 0.30-0.70 range was tuned
    # to the coin-flip BTC series, where every mid is ~0.50. The spread universe
    # the fleet now quotes (tennis, CS2, MLB favourites) legitimately trades at
    # 0.15-0.85, and the band's own protection -- refusing a near-settled outcome
    # at the extreme -- lives at the 0.95+ edge, which 0.10-0.90 still refuses.
    # Measured on the live funnel 2026-08-11: 7 of 8 adopted markets had mids
    # outside 0.30-0.70, and the allocator's funded picks were both blocked by
    # the band (Shnaider at 0.255/0.705) -- a funded market the strategy then
    # refused to quote for hours.
    price_band_low: float = 0.10
    price_band_high: float = 0.90
    # QUOTE TIMING. 57% of his entries land in the FIRST 40% of the window.
    # A passive order needs time to be reached; posting late means resting into
    # the minutes when the price is converging on the outcome, which is exactly
    # when a fill is most likely to be adverse.
    quote_window_frac: float = 0.40
    # Both rules are switchable so their effect can be measured one at a time
    # rather than bundled -- a previous run changed two things at once and the
    # result could not be read.
    #
    # `enforce_price_band` reads on BOTH objectives as of U4. It used to be a
    # rule of the "pair" objective alone -- not by design, but because
    # `_in_band` sits below the line where `_decide_quotes_rewards` returns, so
    # on the objective the fleet actually runs it never executed. Measured
    # 2026-08-05: fills averaged 0.8152 against this nominal 0.30-0.70 band.
    enforce_price_band: bool = True
    enforce_quote_window: bool = True

    # PRICE-DEPENDENT RISK (U4, R6). Every cap above is priced in dollars and
    # none of them notices WHERE in the 0..1 range a fill lands. On a binary
    # market two different risks move in opposite directions across that range,
    # so they get two different treatments rather than one blended knob:
    #
    #   * VARIANCE peaks at the coin flip. The payout is Bernoulli, so variance
    #     per share is p(1-p): 0.2500 at 0.50 against 0.2100 at either band edge
    #     and 0 at the ends. A 0.50 fill is the one least informative about the
    #     outcome, and it is answered with SIZE -- quote less where the coin is
    #     fairest.
    #   * MAGNITUDE rises with the price. The downside of one long share IS the
    #     price paid for it, so the same share count is $30 of risk at 0.30 and
    #     $70 at 0.70 -- the same unit error that made the old share cap
    #     loosest exactly where a wrong resolution cost most. It is answered
    #     with OFFSET -- demand a better price where a share costs more.
    #
    # `risk.band_risk_factor` computes both from one weight,
    #   w(p) = max(0, 1 - |p - 0.50| / coinflip_halfwidth)
    #   size   *= 1 - coinflip_size_cut * w(p)
    #   offset += price_risk_widen * (w(p) + p)
    #
    # 0.20 is half the width of the 0.30-0.70 band above, so the variance
    # treatment reaches zero exactly where the band stops permitting quotes at
    # all. The two rules then describe one geometry: inside the band the
    # response is graduated, at the edge it is nil, past the edge the band
    # refuses outright. Any other halfwidth would leave a seam -- either
    # in-band prices treated as risk-free, or a cut still ramping at a price
    # that is already forbidden.
    coinflip_halfwidth: float = 0.20
    # Originally a 10% trim, sized off the THEORETICAL variance differential
    # alone (0.2500 at 0.50 vs 0.2100 at the band edge, a 19% gap -- paying
    # much more than half of that in forgone rent looked like buying the
    # smaller risk with the larger certainty). The 2026-08-05 forensic audit
    # then measured the REALIZED cost directly and it dominates that estimate:
    # -10.68c mean drift and -$122.00 size-weighted loss in 0.40-0.60 (n=19),
    # the only price band that read negative on both the mean and the
    # size-weighted total. Raised to 55%, the strongest cut that does not
    # zero out at the coin flip: `size = int(ladder * size_mult)` also gates
    # the LIGHT side (the one order exempt from every OTHER risk rule,
    # because it is the only one that reduces exposure), and the 120-share
    # base falls below the 50-share reward minimum for any cut past ~58%
    # (120 * 0.42 =~ 50). 88% was tried first and silently zeroed BOTH sides
    # at 0.50, flat markets included -- the full exclusion this setting was
    # chosen instead of. Reassess against the go-live readiness panel's
    # per-band markout once more data accumulates under this setting.
    coinflip_size_cut: float = 0.55
    # 1c of extra offset at full weight, in price units. Reads as 0.3c at 0.30
    # and 0.7c at 0.70 on the magnitude term -- a 0.4c differential across the
    # band, comparable to the 0.5c `min_reward_offset` and therefore large
    # enough to change which side fills first -- plus up to another 1c at the
    # coin flip. Worst case 1.5c against a 4.5c window: it tilts the quote
    # without evicting it from the reward window on its own.
    price_risk_widen: float = 0.010

    # --- inventory --------------------------------------------------------
    # He finishes markets ~92% balanced between UP and DOWN (median 0.923).
    # Below this we stop adding to the heavy side and only quote the light one.
    target_balance: float = 0.92
    # Stop quoting a side once the pair would cost more than this. The pair
    # pays exactly $1.00, so anything at/above 1.00 is a guaranteed loss.
    # Default 0.995: a common healthy pair (0.51 + 0.48) costs 0.99, which
    # leaves 1c profit and must never be refused (test_healthy_two_sided_flow).
    max_pair_cost: float = 0.995

    # --- pacing -----------------------------------------------------------
    # He averages 19.1 fills/market (median 17), one every ~5s.
    max_fills_per_market: int = 25
    requote_interval_sec: float = 2.0
    poll_interval_sec: float = 1.0

    # Only quote while the window is open enough to resolve sensibly.
    min_t_remaining_sec: float = 15.0

    # --- risk -------------------------------------------------------------
    max_cost_per_market: float = 400.0
    max_open_markets: int = 3

    # --- pairs-only rule (U35): what happens to a one-sided fill -------------
    #
    # Origin (Session 36 / U32, 112h sample): merged pairs were 7/7 positive
    # while a naked leg held past 15 minutes drifted -18.5c/share by the 1h
    # markout. The rule converts each one-sided fill into either a COMPLETED
    # pair (cross the missing leg at ask when the pair stays under
    # `max_pair_cost`, then merge at parity) or a SAME-WINDOW EXIT of the
    # naked leg at the best bid (pay the half-spread instead of the drift).
    # Switchable like every other behavioural change here, so the rule can
    # be measured on its own.
    enable_pairs_rule: bool = True
    # How long after a one-sided fill the rule may still act. 15 minutes is
    # where the measured drift is still ~0 (+0.09c/share at the 5m horizon)
    # and long before the 1h mark where it is -18.5c.
    pairs_exit_window_sec: float = 900.0
    # The EV formula's payoffs (cents per share of a one-sided fill), RE-
    # MEASURED 2026-08-12 (Session 44) on the rule's own recorded decisions:
    # a completion pays the realized merge capture on rule-era completed
    # pairs (+$368.25 on 10,010.7 shares = +3.68c, 144 closes, 100% positive);
    # an exit costs the realized exit economics (-$16.93 on 461 shares =
    # -3.67c, n=4 closes, includes the taker fee; corrected Session 45 -
    # Session 44's -3.89c came from a $1 mis-add of the same four closes).
    # The original +16.3c was a 302.5-share sample (7 pairs) from the
    # pre-spread era and overstates the current instrument's capture ~4.4x;
    # the KPI would have read +15.78c instead of +3.48c per one-sided fill.
    # The RATES still come from the rule's own decisions; the dashboard KPI
    # is completion_rate x gain - exit_rate x cost.
    pairs_complete_gain_cents: float = 3.68
    pairs_exit_cost_cents: float = 3.67

    # --- experiment end criteria (decisive test of the maker mechanism) -----
    # Phase A (census): observe this many DISTINCT live markets and measure how
    # often a fillable sub-$1.00 hedged pair exists at ask-1tick. Below the
    # threshold the instrument simply cannot be made profitably -> stop (DEAD).
    # Phase B (verdict): once the census passes, settle this many markets with
    # the CORRECTED strategy and read P&L sign + confidence -> final save/lost
    # call. These numbers are the experiment's stop condition, rendered live in
    # the dashboard so the run ends on evidence, not vibes.
    experiment_census_markets: int = 60
    experiment_verdict_markets: int = 120
    hedge_fillable_min_rate: float = 0.50

    # --- balance enforcement --------------------------------------------
    # The proven loss driver on 5-min BTC is PARTIAL fills: one side rests
    # and fills, the other never does before the window closes, so the market
    # settles one-sided and eats the full resolution loss. A passive maker
    # cannot UN-fill an already-rested side, so the only enforceable balance
    # rule is to CROSS THE SPREAD for the missing leg near resolution. We do
    # this once, only if still imbalanced, inside this many seconds before
    # close. This guarantees every settled market is balanced -> Phase B's
    # win/loss is an unambiguous verdict on the INSTRUMENT, not our execution.
    balance_hedge_sec: float = 20.0

    # --- economics --------------------------------------------------------
    # crypto_fees_v2: takerOnly=true -> makers pay NO fee. Rebate pool is 20%
    # of taker fees, shared pro-rata among qualifying makers. We cannot see the
    # pool, so rebates are ESTIMATED and reported separately from trading PnL,
    # never blended in.
    maker_fee: float = 0.0
    rebate_rate: float = 0.20
    fee_rate: float = 0.07          # taker fee rate, for the rebate estimate

    # LATENCY AND PROPAGATION PARAMETERS (issue #27, Phase 1 components 2 & 3).
    # Shared one-way network leg. MEASURED -- median(RTT)/2 = 7.85ms/2 (N=250)
    # from TCP connect probe to CLOB gateway.
    net_oneway_ms: float = 3.93

    # Venue-side legs. MEASURED / ESTIMATE.
    # post_venue_accept_ms: MEASURED -- mean of window medians from live CLOB probe
    # (N=90 across 3 windows: 85.00, 75.51, 82.89ms; mean 81.13ms, between-window SD 4.98ms).
    # Per-window draw, not a fixed venue property.
    cancel_venue_ack_ms: float = 150.0     # tau_cancel = 153.93ms total (estimate)
    post_venue_accept_ms: float = 81.0     # tau_post   = 84.93ms total (~1.8x lower than 150ms estimate) [MEASURED]


    @property
    def cancel_net_oneway_ms(self) -> float:
        """Backward compatibility alias for net_oneway_ms."""
        return self.net_oneway_ms

    @property
    def tau_cancel_sec(self) -> float:
        return (self.net_oneway_ms + self.cancel_venue_ack_ms) / 1000.0

    @property
    def tau_post_sec(self) -> float:
        return (self.net_oneway_ms + self.post_venue_accept_ms) / 1000.0

    sim_only: bool = True

    def db_path(self) -> Path:
        return Path(os.environ.get("SPREAD_HUNTER_DB") or os.environ.get("HUNTER_DB", str(ROOT / "hunter.db")))


    # --- pinned market (multi-bot mode) -----------------------------------
    # Empty = the original rolling 5-min BTC series. Set = quote this one
    # long-dated market instead. Long-dated markets are the ones that actually
    # fund resting liquidity (rewards.rates != null), and because they resolve
    # months out they barely move in an hour -- which is what makes resting
    # survivable. Adverse selection, not fee level, is what killed 5-min BTC.
    pinned_condition_id: str = ""
    market_title: str = ""
    market_url: str = ""
    market_daily_rate: float = 0.0


def load() -> MakerConfig:
    """Config, with the per-bot fields overridable from the environment.

    Four bots run the same code against different markets; each gets its own
    HUNTER_DB, port and HUNTER_MARKET. Nothing else differs, so a difference in
    results is a difference in the MARKET, not in the settings.
    """
    kw: dict = {}
    cid = os.environ.get("HUNTER_MARKET", "").strip()
    if cid:
        kw["pinned_condition_id"] = cid
        kw["market_title"] = os.environ.get("HUNTER_TITLE", "")
        kw["market_url"] = os.environ.get("HUNTER_URL", "")
        kw["market_daily_rate"] = float(os.environ.get("HUNTER_DAILY_RATE", "0") or 0)
        # Long-dated markets do not close in 15 seconds, and the min-size floor
        # is per-market (20, 50, 100, 200) -- quoting under it earns nothing.
        kw["min_t_remaining_sec"] = 0.0
        ms = os.environ.get("HUNTER_MIN_SIZE")
        if ms:
            kw["min_quote_shares"] = int(float(ms))
            kw["quote_shares"] = max(int(float(ms)), 120)
        sp = os.environ.get("HUNTER_MAX_SPREAD")   # in cents, from the venue
        if sp:
            kw["max_spread_from_mid"] = float(sp) / 100.0
        tk = os.environ.get("HUNTER_TICK")
        if tk:
            kw["price_tick"] = float(tk)
    trial = os.environ.get("HUNTER_DEPTH_TRIAL_USD") or ""
    if trial.strip():
        kw["select_min_top3_depth_usd_trial"] = float(trial)
    vtri = os.environ.get("HUNTER_VOLUME_TRIAL_USD") or ""
    if vtri.strip():
        kw["select_min_volume_24h_usd_trial"] = float(vtri)
    pr = os.environ.get("HUNTER_PAIRS_RULE") or ""
    if pr.strip():
        kw["enable_pairs_rule"] = pr.strip().lower() not in ("0", "false", "off")
    cno = os.environ.get("HUNTER_NET_ONEWAY_MS") or os.environ.get("HUNTER_CANCEL_NET_ONEWAY_MS")
    if cno and cno.strip():
        kw["net_oneway_ms"] = float(cno)
    cva = os.environ.get("HUNTER_CANCEL_VENUE_ACK_MS")
    if cva and cva.strip():
        kw["cancel_venue_ack_ms"] = float(cva)
    pva = os.environ.get("HUNTER_POST_VENUE_ACCEPT_MS")
    if pva and pva.strip():
        kw["post_venue_accept_ms"] = float(pva)
    mf = os.environ.get("HUNTER_MARGINAL_FLOOR") or ""
    if mf.strip():
        kw["marginal_return_floor"] = float(mf)
    bk = os.environ.get("SPREAD_HUNTER_BANKROLL") or os.environ.get("HUNTER_BANKROLL") or ""
    if bk.strip():
        val = float(bk)
        if not math.isfinite(val) or val <= 0:
            raise ValueError(f"Bankroll override must be finite and strictly positive, got: {val}")
        kw["bankroll_usd"] = val
        kw["allocation_budget"] = val * 0.9
        kw["max_committed_usd"] = val
    return MakerConfig(**kw)

# hook probe
# hook probe
