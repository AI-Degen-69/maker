"""Queue-aware fill simulation for resting maker bids, driven by BOOK DELTAS.

WHY THIS IS THE WHOLE PROJECT
-----------------------------
A maker's edge is buying at the bid instead of crossing to the mid. Measured
from powerwinner's 56,768 fills, spread capture is ~68% of his gross; the
"buy both sides" pair clears only 0.10% and is favourable just 51% of the time,
so the arbitrage story is NOT the business. The business is: rest, don't cross.

That makes one question decisive -- do we actually get filled? A naive model
("the ask touched my price, so I filled") answers yes every time and therefore
invents 100% of the edge. This module refuses to do that.

WHY BOOK DELTAS AND NOT THE TRADE TAPE
--------------------------------------
First attempt keyed fills off the trade tape's `side` field: a taker SELL lifts
a bid. Measured on the live tape, 194 of 200 rows were "BUY". The reason is that
data-api /trades reports each PARTICIPANT's own side, not the aggressor's -- a
maker whose bid gets lifted appears as a "BUY" too (it's powerwinner's own fills
in that feed). So aggressor direction is not recoverable from it, and a
SELL-only rule would report almost no fills and wrongly kill the strategy.

Book deltas avoid the problem entirely. We poll the book; the size resting at
our price level is directly observable, and its decrease is exactly the queue
moving. Verified live: levels move materially every few seconds (60 -> 0 in 6s).

THE MODEL
---------
Posting a bid at price P puts us at the BACK of the queue at that level:
    queue_ahead = size currently resting at P

Each book poll:
  * size at P DECREASED by X  -> the queue ahead of us shrank by X. Once
    queue_ahead reaches 0, any further decrease is us being filled.
  * P is gone from the book / best bid fell below P -> the level was cleared
    outright, so our remainder filled.
  * size at P INCREASED -> people joined BEHIND us. Irrelevant to our fills;
    queue_ahead never grows.

STATED BIASES (each makes us OPTIMISTIC -- treat output as an upper bound)
  1. A decrease may be a CANCEL rather than a fill. Cancels do move us up the
     queue (correct), but we also credit the post-queue remainder as our fill,
     which over-fills us when the level is being cancelled rather than traded.
  2. We assume strict price-time priority and that we joined at the exact
     moment of the snapshot.
  3. Adverse selection is NOT softened anywhere: we get filled precisely when
     someone wants to sell to us, which is disproportionately when the market
     is about to move against us. That cost shows up in the resolution outcome,
     which is the honest place for it.
  4. Joiners during flight: We do not model queue depth changes during the
     in-flight post window tau_post. Orders queueing ahead would raise
     queue_ahead; trades would lower it. Both are unobservable without real
     orders; queue_ahead stays set from decision-time depth (direction optimistic).
  5. Feed broadcast latency (tau_pubsub): At median feed lag (~120ms, N=90 across
     3 windows), touch depth drifts only 4.46% (queue model survives at typical lag).
     In the P95 tail (~1.1s), depth drift expands to 44.20% and touch price moves in
     54.8% of intervals (direction optimistic in the tail).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RestingOrder:
    """One of our bids sitting on the book."""
    token_id: str
    side: str                 # 'UP' | 'DOWN' -- which outcome we are buying
    price: float
    size: float               # shares we want
    filled: float = 0.0
    # Shares the PRE-U1 engine would have considered filled by now: real
    # tape-backed fills plus the delta-path credits U1 withdrew. Tracked
    # separately so shadow accounting cannot double-count -- without it an
    # order whose queue shrinks and is then swept records candidates totalling
    # more than the order size, inflating the unverified count and making the
    # verified ratio read worse than it is.
    shadow_filled: float = 0.0
    queue_ahead: float = 0.0  # shares resting ahead of us when we joined
    posted_ts: float = 0.0
    # Database quote row corresponding to this in-memory order. Kept here so
    # cancellation paths can close the historical row as well as the simulator
    # order; otherwise the dashboard would report cancelled offers as open.
    quote_id: int | None = None
    cancelled: bool = False
    cancelled_ts: float | None = None
    cancel_reason: str = ""

    def cancel(self, ts: float = 0.0, reason: str = "") -> None:
        self.cancelled = True
        self.cancelled_ts = ts
        self.cancel_reason = reason

    @property
    def remaining(self) -> float:
        return max(0.0, self.size - self.filled)

    @property
    def shadow_remaining(self) -> float:
        """`remaining` as the pre-U1 engine would have seen it."""
        return max(0.0, self.size - self.shadow_filled)

    @property
    def is_open(self) -> bool:
        return (not self.cancelled) and self.remaining > 1e-9


@dataclass
class Fill:
    token_id: str
    side: str
    price: float
    size: float
    ts: float
    quote_id: int | None = None
    queue_waited: float = 0.0   # shares that had to clear ahead of us
    # HOW we decided this filled. The two are not equally trustworthy:
    #   'queue' -- size at our level shrank past the queue ahead of us. The
    #              shrink is observed; only its cause (trade vs cancel) is
    #              assumed.
    #   'sweep' -- the level emptied and the best bid dropped below us, so we
    #              credit our WHOLE remainder at once. This is documented
    #              optimistic bias #1, and it is the branch that can silently
    #              manufacture an edge: from the bid side alone a mass
    #              cancellation looks identical to a mass trade.
    #   'race'  -- trade occurred within cancel-propagation window (adverse race loss).
    # Reporting must be able to split these, or a fill rate carried mostly by
    # 'sweep' reads exactly like one earned in the queue.
    reason: str = "queue"


@dataclass
class Recon:
    """Why one resting order did or did not fill on one snapshot.

    A zero in the fills table is ambiguous, and the ambiguity is the whole
    question. Over 11.6h the fleet recorded 1,027 quotes and 0 fills, and
    nothing in the database distinguished "nobody traded where we were
    resting" from "plenty traded and we were behind the queue". The first is a
    market-selection result, the second is an execution result, and they call
    for opposite fixes.

    Outcomes:
      'credited'          -- tape volume beyond the queue reached our order.
      'behind_queue'      -- it traded at our price; the shares ahead took it.
      'no_trade_at_price' -- tape was read; nothing traded at our price.
      'not_yet_arrived'   -- order was still in flight (ts <= posted_ts + tau_post).
      'tape_unavailable'  -- tape could not be read; nothing is claimed.
      'race_loss'         -- trade landed during in-flight cancellation window (latency race loss).
    """
    ts: float
    token_id: str
    price: float
    side: str
    tape_volume: float
    queue_ahead: float
    remaining: float
    credited: float
    outcome: str


@dataclass
class QueueFillEngine:
    """Applies observed book changes to our resting orders, queue-first."""

    orders: list[RestingOrder] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    # One row per open order per snapshot, explaining that snapshot's outcome.
    # Pure observation: nothing here feeds back into crediting, or the
    # instrument would be changing the reading it takes.
    reconciliation: list[Recon] = field(default_factory=list)
    # Fills the pre-U1 delta logic WOULD have credited but the tape cannot
    # support. Never applied to inventory; recorded so the gap is measurable
    # rather than silently traded away for an under-count. These carry
    # `reason="unverified"` and exist only in this list.
    unverified: list[Fill] = field(default_factory=list)
    # token_id -> {price: size} as of the previous poll
    _last_book: dict[str, dict[float, float]] = field(default_factory=dict)
    # token_id -> timestamp as of the previous poll (for poll interval estimation)
    _last_ts: dict[str, float] = field(default_factory=dict)
    # Latency and propagation parameters (issue #27 Phase 1 components 2 & 3)
    net_oneway_ms: float = 3.93
    cancel_venue_ack_ms: float = 150.0
    post_venue_accept_ms: float = 81.0
    cancel_net_oneway_ms: Optional[float] = None
    # Venue minimum fill size (Polymarket min_size = 5 shares). A fill below
    # this cannot exist on the live venue -- it would be rounded or rejected.
    # Paper can credit fractional shares, so we drop sub-min fills here to keep
    # the simulation honest against live (this chiefly removes "race" dust,
    # which is a *fraction* of a cancel-window overlap and is sub-5 shares).
    min_fill_shares: float = 5.0

    def __post_init__(self):
        if self.cancel_net_oneway_ms is not None:
            self.net_oneway_ms = self.cancel_net_oneway_ms

    @property
    def tau_cancel_sec(self) -> float:
        """Total cancel acknowledgment latency window in seconds."""
        return (self.net_oneway_ms + self.cancel_venue_ack_ms) / 1000.0

    @property
    def tau_post_sec(self) -> float:
        """Total post arrival latency window in seconds."""
        return (self.net_oneway_ms + self.post_venue_accept_ms) / 1000.0

    # -- order management --------------------------------------------------
    def post(self, token_id: str, side: str, price: float, size: float,
             book_bids: dict[float, float], ts: float) -> RestingOrder:
        """Join the back of the queue at `price`."""
        o = RestingOrder(
            token_id=token_id, side=side, price=round(price, 4), size=size,
            queue_ahead=float(book_bids.get(round(price, 4), 0.0)), posted_ts=ts,
        )
        self.orders.append(o)
        return o

    def amend(self, order: RestingOrder, price: float,
              book_bids: dict[float, float], ts: float) -> RestingOrder:
        """Move a resting order to `price`, losing 100% of its queue position."""
        new_price = round(price, 4)
        if new_price == order.price:
            return order
        order.price = new_price
        order.queue_ahead = float(book_bids.get(new_price, 0.0))
        order.posted_ts = ts
        return order

    def cross(self, token_id: str, side: str, size: float,
              book_asks: dict[float, float], ts: float,
              max_price: float = 1.0) -> list[Fill]:
        """TAKE liquidity now, walking the ask book upwards. Not a resting order."""
        remaining = float(size)
        made: list[Fill] = []
        for price in sorted(book_asks):
            if remaining <= 1e-9 or price > max_price + 1e-9:
                break
            avail = float(book_asks.get(price, 0.0))
            if avail <= 1e-9:
                continue
            qty = min(remaining, avail)
            f = Fill(token_id=token_id, side=side, price=round(price, 4),
                     size=qty, ts=ts, queue_waited=0.0, reason="cross")
            self.fills.append(f)
            made.append(f)
            remaining -= qty
        return made

    def cancel(self, token_id: Optional[str] = None, ts: float = 0.0,
               reason: str = "") -> int:
        """Cancel open orders matching `token_id`, recording cancellation timestamp and reason."""
        n = 0
        for o in self.orders:
            if o.is_open and (token_id is None or o.token_id == token_id):
                o.cancel(ts=ts, reason=reason)
                n += 1
        return n

    def open_orders(self, token_id: Optional[str] = None) -> list[RestingOrder]:
        """Orders that are currently open (not cancelled) and have remaining size."""
        return [o for o in self.orders
                if o.is_open and (token_id is None or o.token_id == token_id)]

    def race_eligible_orders(self, token_id: Optional[str] = None,
                             ts: float = 0.0,
                             prev_ts: Optional[float] = None) -> list[tuple[RestingOrder, float]]:
        """Orders cancelled whose in-flight window overlapped with [prev_ts, ts].

        THE CANCELLATION RACE (Phase 1, component 2). In real execution, a cancel
        sent at t_c takes tau_cancel = net_oneway + venue_ack to acknowledge.
        Any taker trades arriving during that in-flight window fill our resting
        bid rather than getting cancelled.
        Returns list of (order, p_race).
        """
        tau = self.tau_cancel_sec
        if tau <= 0 or prev_ts is None:
            return []
        dt_poll = max(1e-4, ts - prev_ts)
        eligible: list[tuple[RestingOrder, float]] = []
        for o in self.orders:
            if not o.cancelled or o.cancelled_ts is None or o.remaining <= 1e-9:
                continue
            if token_id is not None and o.token_id != token_id:
                continue
            c_start = o.cancelled_ts
            c_end = c_start + tau
            # Overlap between [prev_ts, ts] and [c_start, c_end]
            overlap = min(ts, c_end) - max(prev_ts, c_start)
            if overlap > 1e-9:
                p_race = min(1.0, max(0.0, overlap / dt_poll))
                eligible.append((o, p_race))
        return eligible

    # -- the core ----------------------------------------------------------
    def on_book(self, token_id: str, bids: dict[float, float], ts: float,
                traded: Optional[dict[float, float]] = None) -> list[Fill]:
        """Feed a fresh bid-side snapshot; returns any fills it implies."""
        bids = {round(p, 4): float(s) for p, s in bids.items()}
        tape = (None if traded is None
                else {round(p, 4): float(v) for p, v in traded.items()})
        prev = self._last_book.get(token_id)
        prev_ts = self._last_ts.get(token_id)
        self._last_book[token_id] = bids
        self._last_ts[token_id] = ts
        if prev is None:
            return []          # need two snapshots to see a delta

        _live = [p for p, s in bids.items() if s > 1e-9]
        best_bid = max(_live) if _live else 0.0
        made: list[Fill] = []
        tau_post = self.tau_post_sec

        # 1. Normal crediting loop for open resting orders (with post latency tau_post)
        for o in self.open_orders(token_id):
            before = prev.get(o.price, 0.0)
            now = bids.get(o.price, 0.0)

            # Eligibility fraction f based on arrival timestamp o.posted_ts + tau_post
            arrival_ts = o.posted_ts + tau_post
            if tau_post <= 0 or prev_ts is None or prev_ts >= arrival_ts:
                f = 1.0
            elif ts <= arrival_ts:
                f = 0.0
            else:
                dt_poll = max(1e-4, ts - prev_ts)
                f = min(1.0, max(0.0, (ts - arrival_ts) / dt_poll))

            t_vol = tape.get(o.price, 0.0) if tape is not None else 0.0
            t_vol_eff = f * t_vol
            qty = min(o.remaining, max(0.0, t_vol_eff - o.queue_ahead))
            left = max(0.0, before - now)          # trades + cancels

            if tape is None:
                shadow, kind = self._delta_would_have_filled(
                    o, before, now, best_bid)
                if shadow > 1e-9:
                    o.shadow_filled += shadow
                    self.unverified.append(Fill(
                        token_id=o.token_id, side=o.side, price=o.price,
                        size=shadow, ts=ts, queue_waited=o.queue_ahead,
                        reason=kind))

            if tape is None:
                outcome = "tape_unavailable"
            elif f <= 1e-9:
                outcome = "not_yet_arrived"
            elif t_vol <= 1e-9:
                outcome = "no_trade_at_price"
            elif qty > 1e-9:
                outcome = "credited"
            else:
                outcome = "behind_queue"
            self.reconciliation.append(Recon(
                ts=ts, token_id=o.token_id, price=o.price, side=o.side,
                tape_volume=t_vol, queue_ahead=o.queue_ahead,
                remaining=o.remaining, credited=max(0.0, qty),
                outcome=outcome))

            if qty > 1e-9:
                made.append(self._fill(o, qty, ts, reason="tape"))
            if f > 1e-9:
                o.queue_ahead = max(0.0, o.queue_ahead - max(left, t_vol_eff))

        # 2. Race crediting loop for in-flight cancelled orders (issue #27 Phase 1 component 2)
        for o, p_race in self.race_eligible_orders(token_id, ts, prev_ts):
            before = prev.get(o.price, 0.0)
            now = bids.get(o.price, 0.0)

            t_vol = tape.get(o.price, 0.0) if tape is not None else 0.0
            qty_exposed = min(o.remaining, max(0.0, t_vol - o.queue_ahead))
            qty_race = p_race * qty_exposed
            left = max(0.0, before - now)

            if tape is None:
                outcome = "tape_unavailable"
            elif t_vol <= 1e-9:
                outcome = "no_trade_at_price"
            elif qty_race > 1e-9:
                outcome = "race_loss"
            else:
                outcome = "behind_queue"
            self.reconciliation.append(Recon(
                ts=ts, token_id=o.token_id, price=o.price, side=o.side,
                tape_volume=t_vol, queue_ahead=o.queue_ahead,
                remaining=o.remaining, credited=max(0.0, qty_race),
                outcome=outcome))

            if qty_race > 1e-9:
                made.append(self._fill(o, qty_race, ts, reason="race"))
            o.queue_ahead = max(0.0, o.queue_ahead - max(left, t_vol))

        return [f for f in made if f is not None and f.size > 1e-9]

    @staticmethod
    def _delta_would_have_filled(o: RestingOrder, before: float, now: float,
                                 best_bid: float) -> tuple[float, str]:
        """Shares the pre-U1 book-delta logic would have credited, verbatim.

        PURE. Reads `o` and never writes it -- the caller applies the queue
        advance itself, once, for both the measured and unmeasured cases.

        This is kept as an exact replica rather than deleted because the ratio
        between what it claims and what the tape confirms is the number the
        Phase A decision gate reads. Deleting it would make the old fill counts
        unexplainable; trusting it is what U1 exists to stop.

        `before > 1e-9` is REQUIRED. Without it this branch fired for any order
        resting ABOVE the best bid -- where, by definition, no size is queued at
        our price -- so `now == 0` and `best_bid < price` were both true on the
        very first poll and the whole order was granted as a fill against a book
        that had not moved a single share. Measured: post 120sh at 0.52 into a
        static {0.50: 300} book and the engine returned a 120sh fill, fill_rate
        1.0. That guard is why a static book records no candidate here either.

        Returns (shares, kind). The kind preserves the evidence-quality split
        the pre-U1 reasons carried: a swept level credited the whole remainder
        off one observation and cannot tell a mass cancel from a mass trade,
        while a shrinking queue at least observed consumption past our
        position. Both are unverified, but they are not equally weak, and the
        gate should be able to see which one dominates.
        """
        # Level cleared outright and the market moved below us: the old logic
        # credited our whole remainder, documented optimistic bias #1.
        if before > 1e-9 and now <= 1e-9 and best_bid < o.price - 1e-9:
            return o.shadow_remaining, "unverified_sweep"

        consumed = before - now
        if consumed <= 1e-9:
            return 0.0, ""      # level grew or held: people joined behind us
        # Queue ahead absorbed first -- computed, not applied.
        consumed -= min(o.queue_ahead, consumed)
        if consumed <= 1e-9:
            return 0.0, ""
        return min(o.shadow_remaining, consumed), "unverified_queue"

    def _fill(self, o: RestingOrder, qty: float, ts: float,
              reason: str = "queue") -> Optional[Fill]:
        qty = min(qty, o.remaining)
        if qty <= 1e-9:
            return None
        # Venue minimum fill size: Polymarket rejects sub-5-share fills. Paper
        # can credit fractional shares (e.g. race dust), so drop anything the
        # live venue could not actually execute. Keeps sim/live honest.
        if qty < self.min_fill_shares:
            return None
        o.filled += qty
        # A tape-backed fill happened in both universes, so it advances the
        # shadow too. Otherwise a verified fill would leave shadow_remaining
        # untouched and the next sweep would claim shares already credited.
        o.shadow_filled += qty
        f = Fill(token_id=o.token_id, side=o.side, price=o.price, size=qty,
                 ts=ts, quote_id=o.quote_id, queue_waited=o.queue_ahead,
                 reason=reason)
        self.fills.append(f)
        return f

    # -- reporting ---------------------------------------------------------
    def filled_shares(self, side: Optional[str] = None,
                      include_crossed: bool = True) -> float:
        return sum(f.size for f in self.fills
                   if (side is None or f.side == side)
                   and (include_crossed or f.reason != "cross"))

    def cost(self, side: Optional[str] = None) -> float:
        return sum(f.size * f.price for f in self.fills
                   if side is None or f.side == side)

    def avg_price(self, side: Optional[str] = None) -> float:
        sh = self.filled_shares(side)
        return (self.cost(side) / sh) if sh else 0.0

    def posted_shares(self) -> float:
        return sum(o.size for o in self.orders)

    def fill_rate(self) -> Optional[float]:
        """Filled / posted for RESTING orders. The number optimistic models
        quietly set to 1.0.

        Crossed shares are excluded: they were taken, not waited for, and
        counting them would let a hedge that swept the book report a fill rate
        above 1.0 while flattering the very metric it is supposed to expose.
        """
        p = self.posted_shares()
        return (self.filled_shares(include_crossed=False) / p) if p else None

    def unverified_shares(self) -> float:
        """Shares the old book-delta logic would have credited that the tape
        could not support. Never in inventory, never in `filled_shares`."""
        return sum(f.size for f in self.unverified)

    def verified_ratio(self) -> Optional[float]:
        """Tape-backed shares as a fraction of everything the engine observed.

        THE number the Phase A decision gate reads. On the 18.7h pre-U1 run the
        equivalent figure was roughly 2 fills in 282 -- if that holds, spread
        capture is mostly an artefact of counting cancels as trades and the
        strategy pivots to pure rent collection.

        None when nothing has been observed at all. A no-op run must not report
        a confident 1.0 (nothing was verified) or 0.0 (nothing failed
        verification); both would be read as a measurement.
        """
        verified = self.filled_shares(include_crossed=False)
        total = verified + self.unverified_shares()
        return (verified / total) if total > 1e-9 else None
