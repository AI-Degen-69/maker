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
      'tape_unavailable'  -- tape could not be read; nothing is claimed.
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

    def cross(self, token_id: str, side: str, size: float,
              book_asks: dict[float, float], ts: float,
              max_price: float = 1.0) -> list[Fill]:
        """TAKE liquidity now, walking the ask book upwards. Not a resting order.

        The balance hedge near settlement is a CROSS, and it was being expressed
        as `engine.post()` at the best ask -- a passive bid. Under the fixed
        model that fills 0 of 150 shares even as the book trades straight
        through the level, because a bid sitting alone at the ask has nothing
        queued at its price, so no bid-side delta can ever be attributed to it.
        Before the phantom-fill fix it filled instantly and completely, which is
        why the hedge looked like it worked. It never did.

        Crossing is a different act and is modelled as one: consume real ask
        depth, at the real prices, in order, and stop when the book runs out.
        A partial cross is a REAL outcome -- if the depth is not there, the leg
        stays unhedged and the caller has to live with that.

        `max_price` caps how far up the book we are willing to walk, so a thin
        book cannot drag the hedge to 0.99 and turn a 1c edge into a 40c loss.

        These fills are tagged 'cross'. They are the only fills in this whole
        strategy that pay the Polymarket taker fee, so nothing downstream may
        treat them as maker fills.
        """
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

    def cancel(self, token_id: Optional[str] = None) -> int:
        n = 0
        for o in self.orders:
            if o.is_open and (token_id is None or o.token_id == token_id):
                o.cancelled = True
                n += 1
        return n

    def open_orders(self, token_id: Optional[str] = None) -> list[RestingOrder]:
        return [o for o in self.orders
                if o.is_open and (token_id is None or o.token_id == token_id)]

    # -- the core ----------------------------------------------------------
    def on_book(self, token_id: str, bids: dict[float, float], ts: float,
                traded: Optional[dict[float, float]] = None) -> list[Fill]:
        """Feed a fresh bid-side snapshot; returns any fills it implies.

        `traded` is {price: volume that actually TRADED at that price since the
        last snapshot}, from the tape. When supplied it replaces the guesswork
        entirely, because it settles the one thing the book cannot:

          book delta  = trades + cancellations   (we see only the sum)
          tape        = trades                   (measured)

        Both trades and cancels move us up the queue, but only trades can fill
        us. Without the tape a level that empties is credited to us in full --
        measured on the first recorded windows, that single branch produced
        100% of all simulated fills, so the whole fill rate rested on the one
        assumption nobody could check. With the tape, a level that vanishes on
        cancellations correctly fills us nothing.

        It also fixes the opposite error. Resting alone inside the spread
        produces no bid-side delta at all, so those fills used to be invisible;
        tape volume at our price is visible whether or not the book moved.

        Remaining bias, still optimistic: the tape does not say which side of
        the book a trade hit, so volume printed at our price while that price
        was on the ASK is counted as if it could have filled our bid. Over a
        ~2s poll interval on a 1-tick market that is a small over-count, and it
        is far smaller than the branch it replaces.
        """
        bids = {round(p, 4): float(s) for p, s in bids.items()}
        tape = (None if traded is None
                else {round(p, 4): float(v) for p, v in traded.items()})
        prev = self._last_book.get(token_id)
        self._last_book[token_id] = bids
        if prev is None:
            return []          # need two snapshots to see a delta

        # Only levels that still hold size count. `max(bids)` alone treated a
        # drained level ({0.50: 0.0}) as the best bid, so the "market moved
        # below us" test could never become true at the very price that had
        # just been swept -- the one case it exists to catch.
        _live = [p for p, s in bids.items() if s > 1e-9]
        best_bid = max(_live) if _live else 0.0
        made: list[Fill] = []

        for o in self.open_orders(token_id):
            before = prev.get(o.price, 0.0)
            now = bids.get(o.price, 0.0)

            # THE ONLY CREDITING PATH (U1). Trades hit the front of the queue,
            # so only volume beyond the shares ahead of us can be ours. Cancels
            # still advance our queue position -- they just never fill us.
            #
            # Absent tape is modelled as zero traded volume, NOT as licence to
            # infer fills from the book delta. The two cases differ in what we
            # know, not in what we may credit: `{}` means the tape was read and
            # nothing traded; `None` means we could not read it. Both credit
            # nothing. Only the second records an unverified candidate, because
            # only the second leaves a gap somebody might later close.
            #
            # Measured on the 18.7h run, the delta path this replaces produced
            # 246 of 282 fills against 2 tape-backed ones -- so the entire
            # fill rate, and every profit number derived from it, rested on the
            # one branch that could not be checked.
            t_vol = tape.get(o.price, 0.0) if tape is not None else 0.0
            qty = min(o.remaining, max(0.0, t_vol - o.queue_ahead))
            left = max(0.0, before - now)          # trades + cancels

            if tape is None:
                # Shadow accounting. Computed from queue state as it stands
                # BEFORE the advance below, so it sees exactly what the old
                # logic saw. Pure: it must never touch `o`, or measuring the
                # gap would change the fills we credit and the ratio would be
                # measuring itself.
                shadow, kind = self._delta_would_have_filled(
                    o, before, now, best_bid)
                if shadow > 1e-9:
                    o.shadow_filled += shadow
                    self.unverified.append(Fill(
                        token_id=o.token_id, side=o.side, price=o.price,
                        size=shadow, ts=ts, queue_waited=o.queue_ahead,
                        reason=kind))

            # Recorded BEFORE the queue advance, so `queue_ahead` is the
            # position the order actually held when this tape was applied --
            # after the advance it reads as though we had always been at the
            # front, which is the one number that makes 'behind_queue'
            # meaningful.
            if tape is None:
                outcome = "tape_unavailable"
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
