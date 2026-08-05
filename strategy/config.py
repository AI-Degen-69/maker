"""Maker strategy config. Numbers derived from powerwinner's measured fills.

Source: 56,768 of his BTC/ETH 5-min fills over 2026-07-14..21 (2,970 markets).
See research/powerwinner_analysis.md.
"""
from __future__ import annotations

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
    bankroll_usd: float = 1000.0

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
    skew_full_shares: float = 240.0     # imbalance that produces max skew
    max_skew: float = 0.015             # cap, in price units
    min_reward_offset: float = 0.005    # never quote nearer mid than this

    # HARD CAP on directional exposure, in shares of imbalance. Skew is a
    # spring and it bottoms out at skew_full_shares: past that, more imbalance
    # produces no more response because `skew` is already clamped to max_skew.
    # Measured live over ~40 minutes, unhedged positions ran to 681 shares on a
    # single market (2.8x saturation) and fleet-wide exposure went $252 -> $1333
    # against $47/day of rent, because the only hard stop -- max_cost_per_market
    # at $400 -- never engaged on a $109 position. Set at 1.5x saturation: skew
    # owns the range where it has authority, this owns the range past it.
    max_naked_shares: float = 360.0

    # EMERGENCY STOP-LOSS. The hard cap above stops us ADDING to the heavy
    # side; it does nothing about the exposure already on the book. Skew is
    # supposed to flatten that with resting orders, and it does -- when someone
    # comes to hit our light-side bid. In a market moving hard against us
    # nobody does: our light-side bid sits under a mid that keeps walking away
    # from it while the heavy leg loses money every tick. That is precisely the
    # case where paying the taker fee is the cheap option.
    #
    # Fraction of max_naked_shares at which the light side is allowed to CROSS
    # the spread instead of resting. 0.8 puts the exception inside the cap, so
    # it fires while there is still a hedge to buy rather than at the moment
    # the cap freezes us at maximum exposure.
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
    markout_horizons: tuple[float, ...] = (300.0, 3600.0, 21600.0)
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
    select_min_volume_24h_usd: float = 25_000.0
    # A market resolving in 2027 cannot contribute a settled observation to a
    # run measured in days, and settlement is the only ground truth this
    # strategy has. The whole 2026-07-31 universe resolved between September
    # 2026 and 2027, which is why `resolutions` is zero in all six databases.
    # 7 days keeps n growing fast enough that a sample is reachable.
    select_max_days_to_resolve: float = 7.0

    # How long to average competitor depth before sizing a position. One
    # snapshot sized the whole fleet on 2026-07-29 and read a competing score
    # of 35 for a market that measured 3,727 live -- a 100x error that put the
    # top-ranked market at $0.25/day actual against $18.96 projected.
    rank_sample_window_sec: float = 1800.0
    # Re-rank cadence. run/markets.json was frozen from 2026-07-29 01:39 while
    # the fleet ran against it for a day and a half.
    rerank_interval_sec: float = 3600.0
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
    # fleet_naked_usd. False here so a single-market bot (strategy.main) is
    # unaffected -- it has no allocator and therefore no scarcity to report.
    capital_scarce: bool = False

    # FLEET-WIDE exposure ceiling, in dollars of unhedged cost.
    #
    # max_naked_shares bounds ONE market. It works -- and it is not enough.
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
    # runner. Zero here so a single-market bot (strategy.main) is unaffected --
    # it has no fleet to be over budget.
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
    max_committed_usd: float = 1000.0
    # Injected each cycle by the fleet runner, same pattern as fleet_naked_usd.
    # Zero for a single-market bot, which has no fleet to total up.
    committed_usd: float = 0.0

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

    # His fill sizes: median 120sh, p10 20, p90 160. 61% were >=50sh.
    quote_shares: int = 120

    # --- powerwinner's two entry rules ------------------------------------
    # PRICE BAND. 54% of his volume enters at 0.30-0.70, and he has ZERO trades
    # at 0.98+. That is where the spread -- and the taker fee he is avoiding,
    # fee = 0.07*p*(1-p), which peaks at p=0.50 -- are widest, so it is where
    # being the maker is worth most. Outside the band the spread collapses
    # toward one tick on a near-certain outcome and there is nothing to capture,
    # while the downside stays the full $1.00.
    price_band_low: float = 0.30
    price_band_high: float = 0.70
    # QUOTE TIMING. 57% of his entries land in the FIRST 40% of the window.
    # A passive order needs time to be reached; posting late means resting into
    # the minutes when the price is converging on the outcome, which is exactly
    # when a fill is most likely to be adverse.
    quote_window_frac: float = 0.40
    # Both rules are switchable so their effect can be measured one at a time
    # rather than bundled -- a previous run changed two things at once and the
    # result could not be read.
    enforce_price_band: bool = True
    enforce_quote_window: bool = True

    # --- inventory --------------------------------------------------------
    # He finishes markets ~92% balanced between UP and DOWN (median 0.923).
    # Below this we stop adding to the heavy side and only quote the light one.
    target_balance: float = 0.92
    # Stop quoting a side once the pair would cost more than this. The pair
    # pays exactly $1.00, so anything at/above 1.00 is a guaranteed loss.
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

    sim_only: bool = True

    def db_path(self) -> Path:
        return Path(os.environ.get("MAKER_DB", str(ROOT / "maker.db")))


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
    MAKER_DB, port and MAKER_MARKET. Nothing else differs, so a difference in
    results is a difference in the MARKET, not in the settings.
    """
    kw: dict = {}
    cid = os.environ.get("MAKER_MARKET", "").strip()
    if cid:
        kw["pinned_condition_id"] = cid
        kw["market_title"] = os.environ.get("MAKER_TITLE", "")
        kw["market_url"] = os.environ.get("MAKER_URL", "")
        kw["market_daily_rate"] = float(os.environ.get("MAKER_DAILY_RATE", "0") or 0)
        # Long-dated markets do not close in 15 seconds, and the min-size floor
        # is per-market (20, 50, 100, 200) -- quoting under it earns nothing.
        kw["min_t_remaining_sec"] = 0.0
        ms = os.environ.get("MAKER_MIN_SIZE")
        if ms:
            kw["min_quote_shares"] = int(float(ms))
            kw["quote_shares"] = max(int(float(ms)), 120)
        sp = os.environ.get("MAKER_MAX_SPREAD")   # in cents, from the venue
        if sp:
            kw["max_spread_from_mid"] = float(sp) / 100.0
        tk = os.environ.get("MAKER_TICK")
        if tk:
            kw["price_tick"] = float(tk)
    return MakerConfig(**kw)
# hook probe
# hook probe
