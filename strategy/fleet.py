"""One process, many markets.

Breadth beats depth here. Income is `rate * ours/(ours+theirs)` per market, so
piling capital into one book fights yourself over a fixed pot -- measured,
$3,000 into a single market returns ~1.0%/day while the same money spread over
20 markets returns ~5%/day, because each market is a separate pot with its own
competition.

Why one process rather than 20 copies of strategy.main: 20 markets x 2 books
polled every second is 40 requests/second against a public API. That gets
rate-limited, and the failure mode is silent -- the bots keep reporting uptime
while scoring nothing. Here markets are visited on a rotation with a fixed
request budget, so adding markets lengthens the sweep instead of raising the
request rate.

    python -m scripts.rank_markets --top 20    # choose markets first
    python -m strategy.fleet
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import replace
from pathlib import Path

from strategy import (gate, markout, merge, profit_take, resolve, rewards,
                      store)
from strategy.allocate import (allocate_fundable, capital_scarcity, shares_for,
                               spread_capture_daily)
from strategy.config import load as load_cfg
from strategy.fills import QueueFillEngine
from strategy.main import full_book, recent_trades
from strategy.markets import fetch_pinned_market
from strategy.net_config import load_net as load_bot_cfg
from strategy.quotes import Inventory, decide_quotes, mid_price

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "run"
(ROOT / "logs").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.FileHandler(ROOT / "logs" / "fleet.log", encoding="utf-8"),
              logging.StreamHandler()])
log = logging.getLogger("fleet")

# Two book requests per market visit. This budget keeps us far under any sane
# public rate limit even at 40 markets.
REQ_PER_SEC = 2.0

# HEARTBEAT.
#
# The dashboard declares the fleet dead when it has not seen progress for
# STALE_AFTER_SEC (120s), and the only thing it could see was
# run/fleet_state.json -- written once per COMPLETE sweep. A healthy 20-market
# sweep is 50-70s, so a single slow venue pushed the write past two minutes and
# a fleet that was trading correctly reported STALE with figures the operator
# was told not to trust.
#
# The pulse separates "the loop is alive" from "a sweep finished". The trading
# loop stamps an in-memory `_Pulse` once per iteration -- roughly every second,
# before any network or DB work, so nothing in the body can skip it -- and a
# background thread copies that stamp to disk on a fixed cadence.
#
# The thread publishes the LOOP's timestamp, never its own. Writing time.time()
# would keep reporting a healthy fleet after the trading loop had wedged, which
# is the single failure this indicator exists to catch. `written_ts` is carried
# alongside purely so a reader can tell "the loop stopped" from "the whole
# process died".
PULSE_FILE = RUN / "fleet_pulse.json"
PULSE_WRITE_SEC = 10.0

# Cooldown before re-attempting a market whose metadata would not load. Long
# enough that a closed market stops costing a request per rotation, short enough
# that a market recovering from a venue blip is picked back up within a sweep or
# two.
MARKET_RETRY_SEC = 60.0

# How often to ask the venue which of our filled markets have closed. Settlement
# is
# the only ground truth this strategy has, and until a market is recorded
# resolved its capital is never released -- but a resolution is a once-per-
# market event on a market that has already stopped trading, so polling it any
# faster than this spends requests the trading loop needs.
RESOLVE_INTERVAL_SEC = 900.0


class _Pulse:
    """Last-known progress of the trading loop, shared with the writer thread.

    Small enough to guard with one lock: the loop holds it for the duration of
    three assignments, so the writer never waits measurably and the loop never
    waits on the writer's disk I/O -- which is the whole point of moving the
    write off the loop thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ts = time.time()
        self._iterations = 0
        self._sweeps = 0
        self._market = ""
        self._markets = 0

    def touch(self, market: str, markets: int) -> None:
        with self._lock:
            self._ts = time.time()
            self._iterations += 1
            self._market = market
            self._markets = markets

    def sweep_done(self) -> None:
        with self._lock:
            self._sweeps += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {"loop_ts": self._ts, "iterations": self._iterations,
                    "sweeps": self._sweeps, "market": self._market,
                    "markets": self._markets, "pid": os.getpid(),
                    "written_ts": time.time()}


def _atomic_write_json(path: Path, data: Any, **dumps_kwargs: Any) -> None:
    """Write data to path atomically using a temporary file and replace."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, **dumps_kwargs), encoding="utf-8")
    tmp.replace(path)


def _write_pulse(pulse: _Pulse) -> None:
    """Publish the pulse atomically.

    Written to a temp file and renamed because the dashboard reads this on an
    unrelated schedule; a reader that catches a partial write would parse a
    truncated JSON object and report the fleet dead for exactly the reason the
    file exists to disprove.
    """
    _atomic_write_json(PULSE_FILE, pulse.snapshot())


def _pulse_writer(pulse: _Pulse, stop: threading.Event,
                  interval: float = PULSE_WRITE_SEC) -> None:
    """Copy the loop's pulse to disk every `interval` seconds until stopped."""
    while not stop.wait(interval):
        try:
            _write_pulse(pulse)
        except Exception as e:
            # A heartbeat that cannot be written is not worth taking the fleet
            # down for, and this thread has no other job -- try again next tick.
            log.warning("pulse write failed: %s: %s", type(e).__name__, e)


def _inventory_from_db(cid: str) -> Inventory:
    """Rebuild a market's share position from its persisted fills.

    The fills table is the ledger; Inventory was only ever a running total of
    it held in memory. Recomputing from the ledger on startup makes the two
    agree, which is the difference between a dashboard that says "no position"
    and one that shows the shares we are actually holding.

    Returns an empty Inventory on any failure -- a fresh DB, a missing table.
    That is the same state as before this function existed, so a broken read
    degrades to the old behaviour rather than stopping the fleet.
    """
    inv = Inventory()
    try:
        with store.db() as c:
            for side, size, price in c.execute(
                    "SELECT side, size, price FROM fills WHERE condition_id=?",
                    (cid,)):
                if side == "UP":
                    inv.up_shares += size or 0.0
                    inv.up_cost += (size or 0.0) * (price or 0.0)
                else:
                    inv.down_shares += size or 0.0
                    inv.down_cost += (size or 0.0) * (price or 0.0)
                inv.fills += 1
            for shares, cost_basis, up_removed, dn_removed in c.execute(
                    "SELECT shares, cost_basis, up_cost_removed, "
                    "dn_cost_removed FROM closes WHERE condition_id=?",
                    (cid,)):
                # A close removed one UP and one DOWN share per pair, each at
                # its OWN average cost at close time -- not in proportion to
                # share counts, which only coincides with the true split when
                # both legs happen to share the same average price. The exact
                # per-leg amounts removed are recorded on the row, so use them
                # directly instead of re-deriving (and getting wrong) a split.
                n = shares or 0.0
                inv.up_shares -= n
                inv.down_shares -= n
                if up_removed is not None and dn_removed is not None:
                    inv.up_cost -= up_removed
                    inv.down_cost -= dn_removed
                else:
                    # Row written before up_cost_removed/dn_cost_removed
                    # existed: fall back to the old (approximate) even split
                    # rather than crashing on a NULL.
                    inv.up_cost -= (cost_basis or 0.0) * 0.5
                    inv.down_cost -= (cost_basis or 0.0) * 0.5
    except Exception as e:
        log.warning("inventory rehydrate failed for %s: %s", cid[:10], e)
    return inv


def _gate_from_db(cid: str) -> str:
    """The persisted gate verdict, defaulting to NORMAL.

    Only EXITED is honoured. A stored NORMAL/WIDENED is treated as absent, so
    the state machine recomputes it from this run's markout instead of carrying
    a mid-graduation position across a restart it knows nothing about.

    Degrades to NORMAL on any failure -- a fresh DB, a table that predates this
    feature. That is the behaviour from before gate persistence existed, so a
    broken read costs us the old bug rather than the whole fleet.
    """
    try:
        return gate.EXITED if store.get_gate_state(cid) == gate.EXITED else gate.NORMAL
    except Exception as e:
        log.warning("gate rehydrate failed for %s: %s", cid[:10], e)
        return gate.NORMAL


class MarketState:
    """Per-market state -- everything that was module-level in the single loop."""

    def __init__(self, spec: dict, base_cfg):
        self.spec = spec
        self.cid = spec["cid"]
        self.title = spec["title"]
        # `daily`, `source` and `pot` are all set by refresh_pot, which the
        # re-rank calls again when the spec is rewritten. See its docstring.
        self.refresh_pot(spec, base_cfg)
        # Each market publishes its own reward window, minimum order size and
        # tick. Quoting under a market's min_size scores exactly zero, so these
        # are load-bearing, not cosmetic.
        self.cfg = replace(
            base_cfg,
            objective="rewards",
            min_quote_shares=int(spec["min_size"]),
            quote_shares=int(spec["shares"]),
            max_spread_from_mid=spec["max_spread"] / 100.0,
            price_tick=float(spec["tick"]),
            min_t_remaining_sec=0.0,
            market_title=spec["title"],
            market_daily_rate=spec["daily"],
        )
        self.market = None
        # Earliest time we may try loading this market again. A market that is
        # closed, or whose metadata endpoint is timing out, costs a full request
        # on EVERY visit while returning nothing -- twenty of those turn a 60s
        # sweep into a multi-minute one and the dashboard reports the fleet
        # dead. Retrying on a cooldown instead keeps a broken market cheap.
        self.market_retry_ts = 0.0
        self.engine = QueueFillEngine()
        # Rehydrate from the fills table instead of starting at zero. Fills are
        # persisted, inventory was not, so every restart silently dropped the
        # position while the DB kept the fills -- the dashboard then reported
        # "no position" against 77 recorded fills. Open orders are NOT restored
        # (the venue would not have them after a restart either); only shares
        # already bought, which are the part that can still lose money.
        self.inv = _inventory_from_db(self.cid)
        self.seen_trades: set = set()
        self.tape_primed = False
        # Pairs merged back to collateral this process. Session-scoped on
        # purpose: it feeds the pairing rate against fills observed in the same
        # window, and the durable record is the `closes` table.
        self.merged_shares = 0.0
        # Rolling (ts, theirs) observations. One snapshot sized the entire
        # fleet on 2026-07-29 and read a competing score of 35 for a market
        # that measured 3,727 live -- a 100x error, and the reason the
        # top-ranked market delivered $0.25/day against $18.96 projected.
        self.theirs_samples: list[tuple[float, float]] = []
        self.err = ""
        # Rehydrate an EXITED verdict, and only an EXITED verdict.
        #
        # This used to start every market at NORMAL on the argument that a
        # restart must not inherit a stale judgement. The argument is backwards
        # for this particular state. EXITED is not a guess -- it is the
        # conclusion of `markout_min_sample` fills proving the market takes
        # money off us, and a process restart is not new information about the
        # market. Starting fresh meant every restart re-entered every toxic
        # market and bought that same evidence a second time, which is the one
        # cost the gate exists to stop us paying twice.
        #
        # NORMAL and WIDENED are deliberately not restored: they are cheap to
        # recompute (one sample, at an offset that still earns rent) and both
        # are recoverable, so inheriting them buys nothing. EXITED is the
        # asymmetric one, and it is terminal by design -- `next_state` never
        # leaves it -- so there is no stale-verdict risk to trade off.
        self.gate = _gate_from_db(self.cid)
        self.markout: dict = {"verdict": "insufficient_sample",
                              "mean_per_share": None, "n": 0}

    def refresh_pot(self, spec: dict, base_cfg) -> None:
        """Re-read what this market pays from a freshly ranked spec.

        WHAT THIS MARKET PAYS, AND WHAT PAYS IT.

        `daily` is the reward pot, and it is 0 for every market publishing
        clobRewards: 0 -- which is most of the ones that trade. Those are paid
        in spread instead, so the pot is reconstructed from volume and book
        width. Three separate consumers need the same answer: the allocator
        sizes on it, the live-state income projection reports it, and the
        dashboard reads that.

        Called from `__init__` and again on every re-rank, because volume
        moves. A market surviving a re-rank keeps its MarketState object, so
        nothing else would re-read the spec -- and for a spread market the pot
        IS the volume estimate, so a stale one sizes against volume the market
        no longer has.

        Derived from config rather than read from the spec, so revising the
        capture assumption takes effect on the next sweep.
        """
        # MERGED IN PLACE, NEVER REBOUND.
        #
        # `main` builds `specs` and `states` from the SAME dict objects, writes
        # live telemetry through `st.spec["_live"]`, and serialises `specs` to
        # run/fleet_state.json. Rebinding `self.spec` to the fresh dict detached
        # a surviving market from that list: its later `_live` writes landed in
        # an object `specs` no longer referenced, so after the first re-rank the
        # dashboard file kept reporting the pre-re-rank snapshot forever.
        #
        # `_live` is excluded from the merge because the fresh spec off disk has
        # none, and copying that absence in would blank the current reading --
        # `visit` reads `prev_live` to decide the merge-velocity exception.
        if spec is not self.spec:
            self.spec.update({k: v for k, v in spec.items() if k != "_live"})
            # cfg carries spec-derived numbers too. Left stale they disagree
            # with the dict `reallocate` reads `min_size` from.
            self.cfg = replace(
                self.cfg,
                min_quote_shares=int(self.spec["min_size"]),
                max_spread_from_mid=self.spec["max_spread"] / 100.0,
                price_tick=float(self.spec["tick"]),
                market_title=self.spec["title"],
                market_daily_rate=self.spec["daily"])

        self.daily = self.spec["daily"]
        self.source = "rewards" if self.daily > 0 else "spread"
        self.pot = self.daily if self.daily > 0 else spread_capture_daily(
            float(self.spec.get("volume_24h") or 0.0),
            float(self.spec.get("spread")
                  or base_cfg.spread_capture_default_spread),
            base_cfg.spread_capture_frac)

    def observe_theirs(self, ts: float, theirs: float, window_sec: float) -> None:
        """Add one competitor-depth reading and drop anything past the window."""
        self.theirs_samples.append((ts, theirs))
        cutoff = ts - window_sec
        self.theirs_samples = [(t, v) for t, v in self.theirs_samples
                               if t >= cutoff]

    def avg_theirs(self) -> float | None:
        """Mean competing depth over the window, or None with no samples.

        None rather than 0.0: no observation is not an empty book, and an
        empty book is the single most attractive-looking input the allocator
        can receive. Guessing it would concentrate capital into exactly the
        markets we know least about.
        """
        if not self.theirs_samples:
            return None
        return sum(v for _, v in self.theirs_samples) / len(self.theirs_samples)


def load_specs() -> list[dict]:
    f = RUN / "markets.json"
    if not f.exists():
        raise SystemExit("run/markets.json missing -- run: "
                         "python -m scripts.rank_markets")
    return json.loads(f.read_text(encoding="utf-8"))


def fleet_naked_cost(states) -> float:
    """Dollars of unhedged inventory across the whole fleet.

    The unpaired leg is the only thing that can lose: a matched pair always
    pays $1. Cost is used rather than share count because $1 of exposure at
    0.85 and at 0.26 are the same amount of money at risk, while 100 shares of
    each are not.
    """
    total = 0.0
    for s in states:
        naked = abs(s.inv.up_shares - s.inv.down_shares)
        if naked <= 0:
            continue
        avg = (s.inv.avg("UP") if s.inv.up_shares > s.inv.down_shares
               else s.inv.avg("DOWN"))
        total += naked * (avg or 0.0)
    return total


def _affordable_cross_size(book_asks: dict, requested: float,
                           available_usd: float) -> float:
    """Maximum taker-hedge size whose ask notional fits the cap."""
    remaining = min(float(requested), sum(float(v) for v in book_asks.values()))
    budget = max(float(available_usd), 0.0)
    size = 0.0
    for price in sorted(book_asks):
        if remaining <= 1e-9 or budget <= 1e-9:
            break
        depth = max(float(book_asks.get(price, 0.0)), 0.0)
        take = min(depth, remaining, budget / price) if price > 0 else 0.0
        size += take
        remaining -= take
        budget -= take * price
    return size


def fleet_committed_cost(states) -> float:
    """Every dollar that has left the wallet or is spoken for.

    Inventory cost -- BOTH legs, paired and naked -- plus the notional resting
    in unfilled offers. `fleet_naked_cost` deliberately counts only the
    unhedged residue because that is what can lose money; this counts what is
    committed, which is a different question and the one nobody was asking.

    Measured 2026-07-30, the gap between them was the whole problem: $767
    naked (inside its $800 cap, looking healthy) against $9,588 committed.
    """
    total = 0.0
    for s in states:
        total += (s.inv.up_cost or 0.0) + (s.inv.down_cost or 0.0)
        # Resting offers are not spent yet, but they are promised: the venue
        # holds collateral against an open bid, and a fill converts the promise
        # into inventory without asking. Excluding them would let the fleet sit
        # exactly at the cap with thousands more already in flight.
        for o in s.engine.open_orders():
            total += o.price * max(0.0, o.size - o.filled)
    return total


def reallocate(states, base) -> dict:
    """Resize every market by marginal return instead of a flat 120 shares.

    Measured 2026-07-29, the flat size produced returns spanning 27.58%/day to
    0.28%/day on identical $115 stakes -- because income is pot x share, and
    share is set by the competition, not by the pot. Big pots are big precisely
    because makers crowd them.

    Two income sources, one water-fill. Reward markets are sized on their pot;
    markets paying no rewards at all are sized on expected spread capture, and
    only the reward ones are held to the $1.50 minimum-distribution floor.

    Runs only on markets that have reported a live share; a market we have not
    measured yet keeps its current size rather than being sized off a guess.
    Markets the allocator funds below their min_size get 0 and stop quoting,
    which is the intended outcome -- capital in a market returning 0.3%/day is
    worse than capital sitting idle.
    """
    obs = []
    # Measured markets with nothing behind them. Held separately from `obs` so
    # they cannot influence the water-fill, then merged into `dollars` at 0.0
    # so their size is actively driven to zero.
    unpayable: list[str] = []
    floor = base.reward_min_payout_usd * base.reward_floor_multiple
    for s in states:
        # SIZE OFF THE COMPETITION, NOT OFF OUR OWN ORDERS.
        #
        # `_live["share"]`, `["capital"]` and `["income"]` are all measured
        # from our resting orders, so all three read zero the moment a market
        # is defunded -- and this function used to consult exactly those. A
        # market defunded for earning nothing then reported nothing, was
        # skipped for having no share, and could never be funded again. One
        # way. Measured on the 13.4h run of 2026-07-30: samples scoring
        # anything decayed from 67/219 to 0/190, and the fleet posted its last
        # quote at T+8.1h while continuing to poll for another five hours.
        #
        # `theirs` is the input that survives, because it is scored over the
        # whole book whether or not we are in it -- Taylor Swift still
        # measured 1,504 while we quoted nothing at all. Averaged, not
        # instantaneous: one snapshot read 35 for a market that measured 3,727
        # and sized the entire fleet off it.
        avg_theirs = s.avg_theirs()
        if avg_theirs is None:
            continue        # never sampled -- keep its size rather than guess

        # A score converts to dollars through the per-share score, since a
        # pair costs ~$1 and N dollars therefore buys ~N shares a side. Any
        # reference capital returns the same competitor depth out of
        # competitor_depth() -- it cancels -- so this is a change of units,
        # not an assumption about size.
        k = rewards.score_per_share(s.cfg.max_spread_from_mid,
                                    s.cfg.reward_offset)
        ref = 100.0
        ours_ref = ref * k
        total = ours_ref + avg_theirs

        # A market that pays no rent still pays a spread, and `MarketState`
        # has already converted that into the same $/day pot, so everything
        # here -- income, marginal, the water-fill -- is unchanged. What does
        # NOT carry over is the $1.50 payout floor: it is the venue's minimum
        # reward DISTRIBUTION, and a spread market makes no distribution.
        # `source` is what tells allocate_fundable which rule applies.
        #
        # No reward pot and no measured volume leaves a zero pot. That is not
        # a cheap market, it is an unknown one, and unknown must not size as
        # zero-competition upside -- so it stays out of the water-fill.
        #
        # But it must NOT be dropped the way an unsampled market is. We have
        # measured this one; `avg_theirs` above proves it. Dropping it here
        # left its cid absent from `dollars`, and the loop below reads absence
        # as "never sampled" and keeps the previous size -- so a spread market
        # funded on an earlier sweep whose volume later read 0 went on quoting
        # its funded size with no pot behind it. That is the exact failure the
        # zeroing below exists to prevent. Recorded as unpayable instead.
        if s.pot <= 0:
            unpayable.append(s.cid)
            continue

        obs.append({"cid": s.cid, "daily": s.pot, "source": s.source,
                    "capital": ref,
                    "share": (ours_ref / total) if total > 0 else 1.0,
                    "min_dollars": float(s.spec["min_size"])})

    if not obs and not unpayable:
        return {}

    # REWARD ELIGIBILITY, applied inside the allocation rather than ahead of
    # it. Polymarket pays nothing below $1 per distribution, so a market
    # projecting under the floor is not a small earner -- it is committed
    # capital earning exactly zero, and 16 of 20 markets were in that state on
    # 2026-07-30 while the fleet funded every one.
    #
    # Judged at the size actually allocated, because income is monotone in
    # size: the same market that fails the floor at its 100-share minimum
    # clears it 3.6x over at the 600 shares the budget affords.
    #
    # Unfunded, NOT dropped: the market stays in `states` so its inventory is
    # still merged, marked out and reconciled. Removing it here would strand a
    # real position with nothing tending it.
    dollars = allocate_fundable(obs, base.allocation_budget,
                                base.marginal_return_floor, floor)

    # Whether the BUDGET, rather than the floor, is what stopped the water-fill
    # while a market was still returning well above the floor. That is the only
    # condition under which holding a stagnant pair has a measurable
    # alternative use, and it is what licenses profit_take's relaxed threshold.
    # Computed once per sweep, on the same observation set the sizing used, so
    # the flag and the allocation cannot disagree.
    scarce = capital_scarcity(obs, dollars, base.allocation_budget,
                              base.marginal_return_floor,
                              base.scarcity_marginal_multiple)

    # Merged only now, AFTER scarcity: these markets were deliberately kept out
    # of the observation set, so they must not colour the budget-vs-floor
    # verdict either. Present at 0.0 the loop below reads as "measured and
    # refused" -- the same treatment a market failing the floor gets.
    # setdefault, not assignment, so a real allocator verdict always wins.
    for cid in unpayable:
        dollars.setdefault(cid, 0.0)

    out = {}
    for s in states:
        # Every market learns the fleet-wide flag, including the ones the
        # allocator did not fund this sweep -- an unfunded market is precisely
        # one whose locked capital we most want released.
        s.cfg = replace(s.cfg, capital_scarce=scarce)

        # Absent from `dollars` means never sampled, and only that -- a market
        # `allocate_fundable` refused is present at 0.0. The two need opposite
        # treatment: an unmeasured market keeps its current size (sizing it
        # off a guess is worse than leaving it alone), while a measured market
        # that cannot pay must be zeroed, or it keeps quoting its startup size
        # while earning nothing.
        #
        # Caught by the smoke run, not the tests: 17 markets kept quoting 120
        # shares each while only 4 were funded, so offers alone reached $2,108
        # against a $2,000 committed cap before a single share was bought.
        if s.cid not in dollars:
            continue
        n = shares_for(dollars[s.cid], int(s.spec["min_size"]))
        out[s.cid] = n
        # quote_shares drives size; min_quote_shares is the venue's scoring
        # floor and must not be raised above it, or we would quote below our
        # own threshold and score zero.
        s.cfg = replace(s.cfg, quote_shares=max(n, 0))
    return out


def visit(st: MarketState, bot_cfg, now: float,
          fleet_naked_usd: float = 0.0, committed_usd: float = 0.0,
          states=None) -> None:
    """One poll of one market: books -> fills -> requote -> reward sample."""
    cfg = st.cfg
    # The single-market helper remains callable in tests; the fleet runner
    # passes the complete state list so emergency-hedge affordability and
    # resting-order reservation use the same fleet-wide committed total.
    committed_states = states if states is not None else [st]
    if st.market is None:
        if now < st.market_retry_ts:
            # Still cooling down from a failed load. Return without spending a
            # request: the loop's time budget is what the dashboard measures as
            # liveness, and a market that was closed ten seconds ago is not
            # worth re-asking about on every rotation.
            return
        st.market_retry_ts = now + MARKET_RETRY_SEC
        # Reward funding is not a loadability condition. The ranker already
        # decided this market belongs in the universe, and half of it now pays
        # spread rather than rent.
        st.market = fetch_pinned_market(st.cid, require_rewards=False)
        if st.market is None:
            # Funding is no longer a rejection cause here: `require_rewards`
            # is False, so an unfunded market comes back fine. What is left is
            # closed, not accepting orders, or a token count other than 2 --
            # and the dashboard renders this string as the market's `err`, so
            # naming rewards sent an operator hunting a pot that was never the
            # problem.
            st.err = "closed / not accepting orders"
            return
    m = st.market

    try:
        up = full_book(bot_cfg.clob_host, m.up_token)
        dn = full_book(bot_cfg.clob_host, m.down_token)
    except Exception as e:
        st.err = f"book fetch: {e}"
        return
    st.err = ""

    # Fills are decided by the TAPE, not by the book emptying: a level that
    # vanishes on cancellations must fill us nothing.
    tape = recent_trades(m.condition_id, st.seen_trades)
    first_pass = not st.tape_primed
    st.tape_primed = True
    for book in (up, dn):
        # A token with NO trades this poll must read as an empty tape, not a
        # missing one. `tape.get(...)` returns None in both cases, and before
        # U1 that None sent the engine down the cancel-ambiguous delta path --
        # so the quietest markets, where nothing traded at all, were exactly
        # the ones generating phantom fills. `{}` says measured-and-empty;
        # None is reserved for a tape we genuinely could not read.
        traded = None if tape is None else (tape.get(book["token_id"]) or {})
        if first_pass:
            traded = None      # a startup backlog is not evidence about us
        mark = len(st.engine.unverified)
        recon_mark = len(st.engine.reconciliation)
        fills = st.engine.on_book(book["token_id"], book["bids"], now,
                                  traded=traded)
        new_unverified = st.engine.unverified[mark:]
        new_recon = st.engine.reconciliation[recon_mark:]

        # U6. Classify the outcome now, while the queue position that produced
        # it is still known. Reconstructing "were we behind the queue?" later
        # from `fill_evidence` is not possible -- the blob records the book,
        # never our place in it.
        try:
            store.log_fill_recon([
                (r.ts, m.condition_id, r.token_id, r.side, r.price,
                 r.tape_volume, r.queue_ahead, r.remaining, r.credited,
                 r.outcome) for r in new_recon])
        except Exception as e:
            log.warning("fill recon not recorded for %s: %s", st.title[:30], e)
        # The engine's list is an append-only log and this loop runs every
        # poll for the life of the process; without draining it the fleet
        # leaks a row per order per poll for as long as it runs.
        del st.engine.reconciliation[:]

        # Persist the decision inputs so a later engine change can be replayed
        # offline -- the capability whose absence forced Phase A to verify by
        # forward running instead of replaying the 18.7h run.
        try:
            store.log_fill_evidence(
                ts=now, condition_id=m.condition_id,
                token_id=book["token_id"],
                bids_json=json.dumps({str(p): s for p, s in book["bids"].items()}),
                tape_json=(None if traded is None
                           else json.dumps({str(p): v for p, v in traded.items()})),
                credited=sum(f.size for f in fills),
                unverified=sum(f.size for f in new_unverified))
        except Exception as e:
            log.warning("fill evidence not recorded for %s: %s", st.title[:30], e)

        for f in new_unverified:
            # Recorded, never applied. These shares were not bought.
            store.log_unverified_fill(
                ts=now, market_slug=m.market_slug,
                condition_id=m.condition_id, token_id=f.token_id,
                side=f.side, price=f.price, size=f.size,
                queue_waited=f.queue_waited, reason=f.reason)
        for f in fills:
            if f.side == "UP":
                st.inv.up_shares += f.size
                st.inv.up_cost += f.size * f.price
            else:
                st.inv.down_shares += f.size
                st.inv.down_cost += f.size * f.price
            st.inv.fills += 1
            store.log_fill(
                market_slug=m.market_slug, condition_id=m.condition_id,
                token_id=f.token_id, side=f.side, price=f.price, size=f.size,
                quote_id=f.quote_id, mid_at_post=None, edge_vs_mid=None,
                queue_waited=getattr(f, "queue_waited", 0.0),
                seconds_to_fill=0.0, crossed=False, reason=f.reason,
            )
            # Open the markout clock. `ref_mid_source` is the load-bearing
            # field: in paper mode our quotes never reach the venue, so this
            # book is already clean of our own size. A LIVE run must pass
            # 'contaminated' unless it subtracts our resting size first --
            # otherwise markout measures our own footprint and hands it back
            # as edge.
            store.log_markout_open(
                ts=now, condition_id=m.condition_id,
                market_slug=m.market_slug, side=f.side,
                fill_price=f.price, size=f.size,
                ref_mid=mid_price(book.get("best_bid"), book.get("best_ask")),
                ref_mid_source="venue_clean")
            log.info("FILL %-28s %-4s %.0fsh @ %.3f",
                     st.title[:28], f.side, f.size, f.price)

    # Price every fill whose horizon has just matured, then re-read this
    # market's verdict. Both are cheap: sample_due touches only rows already
    # due, and the verdict is a mean over rows we have.
    mids = {m.condition_id: {
        "UP": mid_price(up.get("best_bid"), up.get("best_ask")),
        "DOWN": mid_price(dn.get("best_bid"), dn.get("best_ask"))}}
    markout.sample_due(mids, now, cfg.markout_horizons)

    stats = markout.per_market_stats(cfg.markout_min_sample).get(
        m.condition_id,
        {"verdict": "insufficient_sample", "mean_per_share": None, "n": 0})
    prev_gate = st.gate
    st.gate = gate.next_state(st.gate, stats, cfg)
    st.markout = stats
    # Persist the moment we give up on a market, and only that moment. Writing
    # every cycle would be one DB write per market per sweep for a value that
    # almost never changes; writing on the transition costs one write, ever,
    # and is the only write a restart actually needs to read back.
    if st.gate == gate.EXITED and prev_gate != gate.EXITED:
        try:
            store.save_gate_state(m.condition_id, st.gate)
        except Exception as e:
            # An unpersisted EXIT still holds for this process. Losing it on a
            # restart is the old behaviour, not a reason to stop trading.
            log.warning("gate persist failed for %s: %s", st.title[:30], e)
        log.info("GATE EXIT %-28s markout %.4f/sh on n=%d",
                 st.title[:28], stats.get("mean_per_share") or 0.0,
                 stats.get("n", 0))
    # Fleet exposure is a property of every OTHER market as well, so it has to
    # be injected here rather than derived from this market's inventory.
    cfg = replace(cfg, gate_state=st.gate,
                  fleet_naked_usd=fleet_naked_usd,
                  committed_usd=committed_usd)

    # MERGE FIRST, then consider selling. A matched pair redeems for exactly
    # 1.00 through the collateral adapter with no spread and no taker fee, so
    # whenever both exits are available merge strictly dominates: selling the
    # same pair pays 3.4c of fees into a bid sum bounded by 1.00. Running the
    # sell path first would occasionally book a worse exit for no reason.
    #
    # Simulation only in Phase A -- the on-chain executor is U6, and fleet.py
    # deliberately does not import it. What this records is what a merge WOULD
    # realize, on the same terms the real one will.
    try:
        # Projected rent comes from this market's MEASURED income, not an
        # assumed rate -- the velocity exception is only as honest as the
        # number backing it. None when we have not scored here yet, which
        # blocks the exception rather than assuming it favourable.
        prev_live = st.spec.get("_live") or {}
        mg = merge.should_merge(
            st.inv, cfg, gas_cost=cfg.merge_gas_usd,
            projected_rent_per_day=prev_live.get("income"),
            hold_days=cfg.merge_velocity_hold_days)
        if mg["take"]:
            n = mg["shares"]
            up_removed, dn_removed = mg["up_cost_removed"], mg["dn_cost_removed"]

            # Ledger first, memory second -- same ordering discipline as the
            # sell path below, and for the same reason: _inventory_from_db
            # rebuilds from this table on restart, so a merge must never exist
            # in memory without also existing on disk.
            store.log_close(
                condition_id=m.condition_id, market_slug=m.market_slug,
                method="merge", gas=mg["gas"], shares=n,
                cost_basis=mg["cost_basis"], proceeds=mg["proceeds"],
                realized_pnl=mg["realized_pnl"],
                # Merging forgoes nothing: parity IS the settlement value, so
                # there is no concession against holding, only the gas.
                forgone_vs_settlement=0.0,
                up_cost_removed=up_removed, dn_cost_removed=dn_removed)

            # Cost before shares: avg() divides by the share count, so
            # decrementing shares first would rewrite the basis of the residue.
            st.inv.up_cost -= up_removed
            st.inv.down_cost -= dn_removed
            st.inv.up_shares -= n
            st.inv.down_shares -= n
            st.merged_shares += n
            log.info("MERGE %-28s %.0f pairs realized $%+.2f | %s",
                     st.title[:28], n, mg["realized_pnl"], mg["why"])
    except Exception as e:
        log.warning("merge failed on %s: %s: %s",
                    st.title[:30], type(e).__name__, e)
        mg = {"take": False, "why": f"error: {e}"}

    # Take profit on the paired portion, if the market has moved far enough to
    # cover selling both legs and still pay. Wrapped for the same reason
    # `reallocate` is: a bug in a money-making refinement must not stop the
    # data collection the whole run exists for.
    try:
        # The scarcity flag is the allocator's, computed once per sweep, and it
        # relaxes the close threshold to a slightly negative number. It is
        # passed rather than read off cfg inside should_close so the decision
        # stays a pure function of its arguments.
        pt = profit_take.should_close(st.inv, up.get("bids"),
                                      dn.get("bids"), cfg,
                                      capital_scarce=cfg.capital_scarce)
        if pt["take"]:
            n = pt["shares"]
            # Cost removed must be captured BEFORE the mutations below, since
            # avg("UP")/avg("DOWN") divide by the current share counts.
            up_removed = n * st.inv.avg("UP")
            dn_removed = n * st.inv.avg("DOWN")

            # Write the ledger FIRST, mutate memory SECOND. If log_close
            # throws (disk full, DB locked), the position must still be
            # exactly what the DB says it is -- _inventory_from_db rebuilds
            # from this table on every restart, and that rebuild is only
            # correct if a close is never reflected in memory without also
            # landing in the database first.
            store.log_close(
                condition_id=m.condition_id, market_slug=m.market_slug,
                shares=n, up_price=pt["up_avg_price"],
                dn_price=pt["dn_avg_price"], cost_basis=pt["cost_basis"],
                proceeds=pt["proceeds"], fee=pt["fee"],
                realized_pnl=pt["realized_pnl"],
                forgone_vs_settlement=pt["forgone_vs_settlement"],
                up_cost_removed=up_removed, dn_cost_removed=dn_removed)

            # Remove the closed pairs at their own average cost, which leaves
            # the average cost of whatever remains unchanged -- the naked
            # residue keeps the basis it actually has.
            #
            # Order matters: avg("UP") divides by up_shares, so the cost must
            # be decremented BEFORE the share count. Reversing these two lines
            # silently rewrites the basis of the remaining shares.
            st.inv.up_cost -= up_removed
            st.inv.down_cost -= dn_removed
            st.inv.up_shares -= n
            st.inv.down_shares -= n
            log.info("CLOSE %-28s %.0f pairs realized $%+.2f | %s",
                     st.title[:28], n, pt["realized_pnl"], pt["why"])
    except Exception as e:
        log.warning("profit_take failed on %s: %s: %s",
                    st.title[:30], type(e).__name__, e)
        pt = {"take": False, "why": f"error: {e}"}

    # Requote. Long-dated markets never expire mid-session, so t_remaining is
    # effectively infinite and every 5-min timing rule is inert by construction.
    intents, why = decide_quotes(cfg, up, dn, st.inv, 1e9, None)

    # An emergency-hedge intent is a TAKER order and must not be posted as a
    # resting bid. Under the queue fill model a lone bid at the ask has nothing
    # queued at its price, so no bid-side delta can ever be attributed to it --
    # it would fill 0 shares while the book traded straight through, and the
    # stop-loss would silently do nothing at all. That exact bug is documented
    # on QueueFillEngine.cross(), which is the correct primitive here: consume
    # real ask depth at real prices and accept a partial fill as a real result.
    crossing = [qi for qi in intents if qi.crossed]
    intents = [qi for qi in intents if not qi.crossed]
    if crossing:
        # A taker hedge is an exit action, not an additional resting position.
        # Release every open bid before measuring affordability so stale offers
        # cannot consume capacity and incorrectly block the hedge. The next
        # requote pass below may restore only the intents that still qualify.
        released = st.engine.open_orders()
        for o in released:
            o.cancelled = True
        store.mark_cancelled([o.quote_id for o in released
                              if o.quote_id is not None])

    for qi in crossing:
        book = up if qi.side == "UP" else dn
        asks = book.get("asks") or {}
        # Emergency hedges are the only path that can add inventory without
        # going through the resting-order reservation below. Cap them too:
        # the stop-loss may take a partial hedge, but it must never turn a
        # $1,000 wallet into a larger simulated position.
        available = max(cfg.max_committed_usd
                       - fleet_committed_cost(committed_states), 0.0)
        cross_size = _affordable_cross_size(asks, qi.size, available)
        if cross_size <= 1e-9:
            store.log_decision(
                market_slug=m.market_slug, condition_id=m.condition_id,
                action="EMERGENCY_HEDGE_BLOCKED", side=qi.side,
                price=qi.price, mid=qi.mid, edge_vs_mid=qi.edge_vs_mid,
                t_remaining=None, balance=st.inv.balance,
                pair_cost=st.inv.pair_cost(),
                reason=f"{qi.reason}; committed cap leaves no affordable hedge",
            )
            continue
        got = 0.0
        qid = store.log_quote(
            market_slug=m.market_slug, condition_id=m.condition_id,
            token_id=qi.token_id, side=qi.side, price=qi.price, size=cross_size,
            queue_ahead=0.0, mid=qi.mid, edge_vs_mid=qi.edge_vs_mid,
            t_remaining=None,
        )
        for f in st.engine.cross(qi.token_id, qi.side, cross_size,
                                 asks, now):
            if f.side == "UP":
                st.inv.up_shares += f.size
                st.inv.up_cost += f.size * f.price
            else:
                st.inv.down_shares += f.size
                st.inv.down_cost += f.size * f.price
            st.inv.fills += 1
            got += f.size
            # crossed=True is load-bearing downstream: kpi.py excludes these
            # from the maker fill rate and charges them the taker fee. A
            # crossed lot recorded as a maker fill would flatter both numbers.
            store.log_fill(
                quote_id=qid, market_slug=m.market_slug,
                condition_id=m.condition_id, token_id=f.token_id,
                side=f.side, price=f.price, size=f.size, mid_at_post=qi.mid,
                edge_vs_mid=None, queue_waited=0.0, seconds_to_fill=0.0,
                crossed=True, reason=f.reason,
            )
        # A shallow ask can leave a residual portion of the capped cross
        # unfilled. It was never a resting order, so close its quote row now;
        # otherwise historical open-offer metrics overstate live exposure.
        if got + 1e-9 < cross_size:
            store.mark_cancelled([qid])
        store.log_decision(
            market_slug=m.market_slug, condition_id=m.condition_id,
            action="EMERGENCY_HEDGE", side=qi.side, price=qi.price,
            mid=qi.mid, edge_vs_mid=qi.edge_vs_mid, t_remaining=None,
            balance=st.inv.balance, pair_cost=st.inv.pair_cost(),
            reason=f"{qi.reason}; filled {got:.0f}/{cross_size:.0f}sh "
                   f"(requested {qi.size:.0f})",
        )
        log.info("EMERGENCY_HEDGE %-28s %-4s %.0f/%.0fsh bal=%.2f",
                 st.title[:28], qi.side, got, qi.size, st.inv.balance)

    # Cancel stale or resized orders before reserving the next batch. Keeping
    # an old-size order when the allocator just reduced `quote_shares` makes
    # the allocation advisory rather than a capital limit.
    want = {qi.side: qi for qi in intents}
    keep = set()
    cancelled = []
    for o in st.engine.open_orders():
        qi = want.get(o.side)
        if (qi is not None and round(qi.price, 4) == o.price
                and o.size == qi.size):
            keep.add(o.side)      # leave it alone: requoting loses queue position
        else:
            o.cancelled = True
            cancelled.append(o.quote_id)
    store.mark_cancelled([qid for qid in cancelled if qid is not None])

    # `committed_usd` was sampled before this visit. It is useful for the
    # decision layer, but it cannot reserve the order we are about to add.
    # Enforce the hard wallet cap against the post-cancellation state and size
    # each new order to the remaining dollars. A final remainder below the
    # venue's minimum is left idle rather than creating a quote that scores 0.
    available = max(cfg.max_committed_usd
                       - fleet_committed_cost(committed_states), 0.0)
    budget_blocked: list[str] = []
    for qi in intents:
        if qi.side in keep:
            continue
        if qi.price <= 0:
            continue
        size = min(qi.size, int(available / qi.price))
        if size < cfg.min_quote_shares:
            budget_blocked.append(f"{qi.side}: committed cap leaves "
                                 f"{size:.0f}sh < {cfg.min_quote_shares} minimum")
            continue
        book = up if qi.side == "UP" else dn
        o = st.engine.post(qi.token_id, qi.side, qi.price, size, book["bids"], now)
        available -= o.price * o.size
        o.quote_id = store.log_quote(
            market_slug=m.market_slug, condition_id=m.condition_id,
            token_id=qi.token_id, side=qi.side, price=qi.price, size=size,
            queue_ahead=o.queue_ahead, mid=qi.mid, edge_vs_mid=qi.edge_vs_mid,
            t_remaining=None,
        )
    if budget_blocked:
        why = "; ".join(x for x in (why, *budget_blocked) if x)

    bq1, bq2 = rewards.book_scores(up, dn, cfg.max_spread_from_mid,
                                   cfg.min_quote_shares)
    oq1, oq2 = rewards.our_scores(st.engine.open_orders(), up, dn,
                                  cfg.max_spread_from_mid, cfg.min_quote_shares)
    ours, theirs, share = rewards.share_of_pool(oq1, oq2, bq1, bq2)
    # Feed the rolling window the allocator averages over, so sizing responds
    # to the competition's typical depth rather than to one lucky snapshot.
    st.observe_theirs(now, theirs, cfg.rank_sample_window_sec)
    store.log_reward_sample(
        ts=now, market_slug=m.market_slug, condition_id=m.condition_id,
        our_score=ours, market_score=theirs,
        offset_c=100 * cfg.reward_offset,
        n_sides=len({o.side for o in st.engine.open_orders()}),
    )

    # Everything below is expressed on ONE price axis: the UP price. A bid on
    # DOWN at p is economically an offer to sell UP at 1-p, so folding it onto
    # the UP axis puts both of our orders on the same line and makes the shape
    # of the position visible -- our bid below mid, our effective ask above it,
    # straddling symmetrically. Two separate books hide that.
    orders = st.engine.open_orders()
    our_up = next((o.price for o in orders if o.side == "UP"), None)
    our_dn = next((o.price for o in orders if o.side == "DOWN"), None)
    up_bid, up_ask = up.get("best_bid"), up.get("best_ask")
    dn_bid, dn_ask = dn.get("best_bid"), dn.get("best_ask")

    st.spec["_live"] = {
        "share": share, "ours": ours, "theirs": theirs,
        # Projected income at the CURRENT score share, off whichever pot pays
        # this market. Reading `daily` here reported $0.00/day for every
        # spread market, which is true of its rent and false of its income.
        "income": share * st.pot,
        "pot": st.pot, "source": st.source,
        "capital": sum(o.price * (o.size - o.filled) for o in orders),
        "quotes": [{"side": o.side, "price": round(o.price, 4), "size": o.size}
                   for o in orders],
        "up_sh": st.inv.up_shares, "dn_sh": st.inv.down_shares,
        "up_avg": st.inv.avg("UP"), "dn_avg": st.inv.avg("DOWN"),
        # Paired shares are safe: one YES + one NO always pays exactly $1.00,
        # so what matters is the leftover. NAKED shares are the only thing that
        # can lose -- they pay $1 or $0 on resolution, nothing in between.
        "paired": min(st.inv.up_shares, st.inv.down_shares),
        "naked_side": ("UP" if st.inv.up_shares > st.inv.down_shares
                       else ("DOWN" if st.inv.down_shares > st.inv.up_shares else "")),
        "naked_sh": abs(st.inv.up_shares - st.inv.down_shares),
        "naked_cost": (abs(st.inv.up_shares - st.inv.down_shares)
                       * (st.inv.avg("UP") if st.inv.up_shares > st.inv.down_shares
                          else st.inv.avg("DOWN"))),
        "pair_paid": (min(st.inv.up_shares, st.inv.down_shares)
                      * (st.inv.avg("UP") + st.inv.avg("DOWN"))),
        "gate": st.gate,
        # Surfaced because it silently changes the close threshold: a close
        # booked at -0.3c/sh is correct under scarcity and a bug without it,
        # and the dashboard cannot tell the two apart from the P&L alone.
        "capital_scarce": cfg.capital_scarce,
        "markout": st.markout.get("mean_per_share"),
        "markout_n": st.markout.get("n", 0),
        "close_why": pt.get("why", ""),
        # Merge, reported separately from the sell path. Recycled capital is
        # the number that distinguishes this strategy from a carry trade: it
        # is money that went back to work rather than sitting until 2027.
        "merge_why": mg.get("why", ""),
        "merged_shares": st.merged_shares,
        "recycled_usd": st.merged_shares * merge.PARITY,
        # Merged pairs against shares filled -- the assumption merge economics
        # rest on. None until something fills; no observation is not a zero.
        "pairing_rate": merge.pairing_rate(
            st.merged_shares, st.engine.filled_shares(include_crossed=False)),
        "fills": st.inv.fills, "err": st.err, "ts": now,
        "up_bid": up_bid, "up_ask": up_ask,
        "dn_bid": dn_bid, "dn_ask": dn_ask,
        "mid_up": ((up_bid + up_ask) / 2.0) if (up_bid and up_ask) else None,
        "our_up": our_up,
        # our DOWN bid, drawn on the UP axis
        "our_dn_as_up": (round(1.0 - our_dn, 4) if our_dn is not None else None),
        # market's own best DOWN bid, also on the UP axis: this is the price
        # someone else is already willing to sell UP at.
        "dn_bid_as_up": (round(1.0 - dn_bid, 4) if dn_bid else None),
        "max_spread": cfg.max_spread_from_mid,
        "pair_cost": (round(our_up + our_dn, 4)
                      if (our_up is not None and our_dn is not None) else None),
        "why": why,
    }


def _maybe_resolve(bot_cfg, last: float, now: float) -> float:
    """Run the settlement pass at most once per RESOLVE_INTERVAL_SEC.

    Returns the deadline to carry forward. The deadline advances even when the
    pass fails: a persistently unreachable venue would otherwise be retried on
    every iteration of the trading loop -- once per second, against dozens of
    markets -- instead of once per interval.
    """
    if now - last < RESOLVE_INTERVAL_SEC:
        return last
    try:
        n = resolve.resolve_finished(bot_cfg.clob_host)
        if n:
            log.info("RESOLVE %d market(s) settled", n)
    except Exception as e:
        # `resolve_finished` already swallows per-market failures; this catches
        # only a failure of the pass itself, which must not stop the fleet.
        log.warning("resolution pass failed: %s: %s", type(e).__name__, e)
    return now


def main() -> None:
    RUN.mkdir(exist_ok=True)
    base = load_cfg()
    bot_cfg = load_bot_cfg()
    specs = load_specs()
    states = [MarketState(s, base) for s in specs]
    log.info("fleet starting | %d markets | $%.0f/day funded | offset %.1fc",
             len(states), sum(s["daily"] for s in specs), 100 * base.reward_offset)

    # Start the heartbeat before the first visit, and publish once immediately:
    # a dashboard polled during startup should see a fresh pulse rather than a
    # missing file for the first PULSE_WRITE_SEC.
    pulse = _Pulse()
    stop_pulse = threading.Event()
    threading.Thread(target=_pulse_writer, args=(pulse, stop_pulse),
                     name="fleet-pulse", daemon=True).start()
    try:
        _write_pulse(pulse)
    except Exception as e:
        log.warning("initial pulse write failed: %s: %s", type(e).__name__, e)

    gap = 2.0 / REQ_PER_SEC
    i = 0
    last_rerank = time.time()
    # Deliberately 0.0 rather than `now`: a fleet that has just restarted is
    # exactly when the unresolved set is most likely to contain markets that
    # closed while it was down, and waiting a full interval to notice keeps
    # that capital committed for another 15 minutes.
    last_resolve = 0.0
    empty_logged = False
    while True:
        # PERIODIC RE-RANK. run/markets.json was written 2026-07-29 01:39 and
        # the fleet ran against it for a day and a half while competitors
        # arrived and reward rates changed underneath it.
        #
        # Re-picking the candidate set means scoring hundreds of books, which
        # does not belong inside the trading loop -- `scripts/rank_markets`
        # owns that and writes the file. What happens here is adopting the
        # result: any market the ranker has since added is picked up, and any
        # market it dropped is retained if it still holds inventory, because
        # dropping a live position strands it with nothing to merge or
        # reconcile it.
        now_ts = time.time()

        # SETTLEMENT. Runs before the re-rank so that a market closing this
        # cycle is recorded resolved before the ranker is allowed to drop it:
        # `unresolved()` is keyed off fills, not off the current market set,
        # but recording it first keeps the two views consistent within a cycle.
        last_resolve = _maybe_resolve(bot_cfg, last_resolve, now_ts)

        if now_ts - last_rerank >= base.rerank_interval_sec:
            last_rerank = now_ts
            try:
                fresh = {s["cid"]: s for s in load_specs()}
                known = {s.cid for s in states}
                by_cid = {s.cid: s for s in states}
                for cid, spec in fresh.items():
                    if cid not in known:
                        states.append(MarketState(spec, base))
                        log.info("RERANK + %s", spec["title"][:40])
                    else:
                        # A SURVIVING MARKET KEEPS ITS OBJECT, SO ITS POT MUST
                        # BE RE-READ EXPLICITLY. For a spread market the pot IS
                        # the volume estimate, and `__init__` computed it once
                        # from the spec present at process start. A market whose
                        # 24h volume halved went on sizing and reporting against
                        # the volume it had hours ago, for the life of the
                        # process -- which is exactly what the periodic re-rank
                        # exists to prevent.
                        by_cid[cid].refresh_pot(spec, base)
                held = [s for s in states if s.cid not in fresh
                        and (s.inv.up_shares or s.inv.down_shares)]
                dropped = [s for s in states
                           if s.cid not in fresh and s not in held]
                if dropped:
                    for s in dropped:
                        log.info("RERANK - %s", s.title[:40])
                    states = [s for s in states if s not in dropped]
                if held:
                    log.info("RERANK %d dropped market(s) retained: still "
                             "holding inventory", len(held))
            except Exception as e:
                # A stale market set is survivable; a dead fleet is not.
                log.warning("rerank failed, keeping current markets: %s: %s",
                            type(e).__name__, e)

        if not states:
            # Every market dropped, or the ranker has not written a usable
            # markets.json yet. `states[i % 0]` would take the process down;
            # idling keeps it and its heartbeat alive so the next re-rank can
            # refill the fleet. The pulse still advances -- the loop IS running,
            # it just has nothing to visit, and the empty market count on the
            # pulse is what tells the operator which of the two it is.
            pulse.touch("", 0)
            # Logged on the transition only. At one iteration per second an
            # unconditional warning here fills the log faster than the re-rank
            # interval that would clear it.
            if not empty_logged:
                log.warning("no markets in fleet; waiting for re-rank")
                empty_logged = True
            time.sleep(gap)
            continue
        if empty_logged:
            log.info("markets restored: %d in fleet", len(states))
            empty_logged = False

        st = states[i % len(states)]
        i += 1
        # THE HEARTBEAT TOUCHPOINT. Stamped before the visit, not after, so a
        # market that throws, times out or returns early still counts as a live
        # cycle -- the loop's health is whether it is cycling, not whether this
        # particular venue answered.
        pulse.touch(st.title[:40], len(states))
        try:
            # Both totals are recomputed per visit rather than per sweep: a
            # fill in the market visited two seconds ago has already changed
            # them, and a cap evaluated against a stale total is a cap that
            # lets the overshoot through.
            visit(st, bot_cfg, time.time(), fleet_naked_cost(states),
                  fleet_committed_cost(states), states)
        except Exception as e:
            log.warning("%s: %s", st.title[:30], e)
            st.err = str(e)

        if i % len(states) == 0:
            live = [s for s in states if s.spec.get("_live", {}).get("ours", 0) > 0]
            inc = sum(s.spec.get("_live", {}).get("income", 0) for s in states)
            cap = sum(s.spec.get("_live", {}).get("capital", 0) for s in states)
            # Resize once per full sweep, when every market has a fresh share
            # reading. Doing it mid-sweep would size half the fleet off this
            # cycle's data and half off the last one's.
            # Wrapped because a sizing bug must never stop data collection.
            # Unwrapped, a ZeroDivisionError in the allocator killed the whole
            # fleet mid-sweep on 2026-07-29 and nothing was collected for 3.5
            # hours. Quoting at the previous size is an acceptable degraded
            # mode; being dead is not.
            try:
                sizes = reallocate(states, base)
            except Exception as e:
                log.warning("reallocate failed, keeping previous sizes: %s: %s",
                            type(e).__name__, e)
                sizes = {}
            funded = sum(1 for n in sizes.values() if n > 0)
            # The verified ratio rides on the sweep line because it is the one
            # number that decides what happens after Phase A, and a figure that
            # lives only in the database is a figure nobody reads. `--` means
            # nothing observed yet, deliberately not 0% -- an idle fleet has
            # not measured anything.
            try:
                vr = store.verified_ratio()
                vr_txt = ("--" if vr["ratio"] is None
                          else f"{100 * vr['ratio']:.1f}%")
                fills_txt = f"{vr['verified_fills']}v/{vr['unverified_fills']}u"
            except Exception as e:
                vr_txt, fills_txt = "err", str(type(e).__name__)
            # `capital` is offers only; `committed` is every dollar out the
            # door. The pair is logged together on purpose -- reading the
            # first without the second is how a 0.256%/day return got reported
            # as 1.80%/day for a day and a half.
            committed = fleet_committed_cost(states)
            # Sample the projection so it can be integrated over time. The
            # instantaneous figure swings by an order of magnitude within an
            # hour as markets are funded and defunded, so a single reading is
            # only whichever moment the reader happened to look at.
            try:
                store.log_income_sample(time.time(), inc, committed)
            except Exception as e:
                log.warning("income sample failed: %s", e)
            log.info("sweep | %d/%d scoring | est $%.2f/day | offers $%.0f "
                     "| committed $%.0f/%.0f | naked $%.0f | funded %d/%d "
                     "| verified %s (%s)",
                     len(live), len(states), inc, cap,
                     committed, base.max_committed_usd,
                     fleet_naked_cost(states), funded, len(states),
                     vr_txt, fills_txt)
            pulse.sweep_done()
            # SERIALISE FROM `states`, NOT FROM `specs`. `specs` is the list
            # read at startup and it was never kept in step with the re-rank:
            # a market the ranker ADDED got a MarketState but no entry here, so
            # its live telemetry never reached the dashboard, and a market the
            # ranker DROPPED kept its final `_live` in the file forever. Reading
            # the states list makes the file exactly the set being traded.
            #
            # Written via a temp file and renamed because the dashboard reads
            # this on its own schedule; json.loads on a half-written file throws
            # and the page then reports the fleet as not running.
            #
            # Wrapped for the same reason every other sweep-end step is: a full
            # disk must degrade the dashboard, not stop the trading loop before
            # it can reach the next heartbeat.
            try:
                f = RUN / "fleet_state.json"
                _atomic_write_json(f, [s.spec for s in states], default=str)
            except Exception as e:
                log.warning("fleet_state write failed: %s: %s",
                            type(e).__name__, e)
        time.sleep(gap)


if __name__ == "__main__":
    main()
