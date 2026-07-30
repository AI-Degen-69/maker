"""Fill-model tests.

The repo shipped with no tests at all, while its research log claimed "Seven
unit tests cover queue precedence, sweeps and overfill". The two regressions at
the bottom are for bugs that were live in the model that produced every maker
result recorded so far, so those numbers should not be trusted.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.fills import QueueFillEngine


def _engine_with(price, size, bids, ts=0.0):
    eng = QueueFillEngine()
    eng.post("T", "UP", price, size, bids, ts)
    eng.on_book("T", bids, ts + 1)      # first snapshot only establishes `prev`
    return eng


# --- queue mechanics --------------------------------------------------------

def test_needs_two_snapshots_before_any_fill():
    eng = QueueFillEngine()
    eng.post("T", "UP", 0.50, 100, {0.50: 10.0}, 0.0)
    assert eng.on_book("T", {0.50: 0.0}, 1.0) == []     # first snapshot: no delta


def test_queue_ahead_absorbs_before_we_fill():
    eng = _engine_with(0.50, 120, {0.50: 300.0})
    assert eng.on_book("T", {0.50: 100.0}, 2.0) == []   # 200 traded, all ahead of us
    assert eng.open_orders()[0].queue_ahead == 100.0


def test_fills_only_after_queue_clears():
    eng = _engine_with(0.50, 120, {0.50: 300.0})
    assert eng.on_book("T", {0.50: 100.0}, 2.0) == []   # 200 traded, all ahead of us
    fills = eng.on_book("T", {0.50: 0.0, 0.49: 50.0}, 3.0)
    # level swept and the market moved below us -> our remainder trades.
    # NOTE this is documented optimistic bias #1: a sweep may have been
    # cancellations, and we still credit the whole remainder as filled.
    assert sum(f.size for f in fills) == 120.0


def test_level_growing_never_fills_us():
    eng = _engine_with(0.50, 120, {0.50: 100.0})
    assert eng.on_book("T", {0.50: 400.0}, 2.0) == []   # joiners behind us
    assert eng.open_orders()[0].queue_ahead == 100.0


def test_never_overfills_beyond_order_size():
    eng = _engine_with(0.50, 100, {0.50: 0.0})
    eng._last_book["T"] = {0.50: 9999.0}
    fills = eng.on_book("T", {0.50: 0.0, 0.49: 1.0}, 3.0)
    assert sum(f.size for f in fills) == 100.0
    assert eng.filled_shares() == 100.0


def test_cancelled_order_stops_filling():
    eng = _engine_with(0.50, 120, {0.50: 0.0})
    eng.cancel("T")
    eng._last_book["T"] = {0.50: 500.0}
    assert eng.on_book("T", {0.50: 0.0, 0.49: 1.0}, 3.0) == []


# --- regressions ------------------------------------------------------------

def test_quote_above_best_bid_does_not_fill_on_a_static_book():
    """REGRESSION: the model granted a full fill against a book that never moved.

    Resting inside the spread means no size is queued at our price, so the old
    "level cleared outright" test (now == 0 and best_bid < price) was true on
    the first poll and handed us the entire order. Posting 120sh at 0.52 into a
    static {0.50: 300} book returned a 120sh fill at rate 1.0 -- inventing the
    edge, and worst where the spread was widest.
    """
    static = {0.50: 300.0, 0.49: 500.0}
    eng = _engine_with(0.52, 120, static)
    assert eng.on_book("T", static, 2.0) == []
    assert eng.fill_rate() == 0.0


# --- tape-confirmed fills ---------------------------------------------------

def test_a_level_that_vanishes_on_cancels_fills_us_nothing():
    """The correction that matters. Book-only, an emptied level hands us the
    whole order; the tape shows nothing traded, so nothing filled."""
    eng = _engine_with(0.50, 120, {0.50: 300.0})
    book_only = eng.on_book("T", {0.50: 0.0, 0.49: 40.0}, 2.0)
    assert sum(f.size for f in book_only) == 120.0        # the optimistic model

    eng2 = _engine_with(0.50, 120, {0.50: 300.0})
    with_tape = eng2.on_book("T", {0.50: 0.0, 0.49: 40.0}, 2.0, traded={})
    assert with_tape == []
    assert eng2.fill_rate() == 0.0


def test_tape_fills_only_past_the_queue_ahead():
    eng = _engine_with(0.50, 120, {0.50: 100.0})          # 100 ahead of us
    f1 = eng.on_book("T", {0.50: 40.0}, 2.0, traded={0.50: 60.0})
    assert f1 == []                                       # all 60 hit the queue
    assert eng.open_orders()[0].queue_ahead == 40.0
    f2 = eng.on_book("T", {0.50: 0.0, 0.49: 5.0}, 3.0, traded={0.50: 90.0})
    # 90 traded, 40 still ahead -> 50 is ours
    assert sum(f.size for f in f2) == 50.0
    assert all(f.reason == "tape" for f in f2)


def test_tape_sees_a_fill_the_book_cannot_when_we_rest_inside_the_spread():
    """Resting alone at our price produces no bid-side delta at all, so the
    book-only model is blind here -- it is the pessimistic bias in the
    docstring. Tape volume at our price is visible either way."""
    static = {0.50: 300.0, 0.49: 500.0}
    eng = _engine_with(0.52, 120, static)                 # inside the spread
    assert eng.on_book("T", static, 2.0) == []            # book-only: nothing
    eng2 = _engine_with(0.52, 120, static)
    fills = eng2.on_book("T", static, 2.0, traded={0.52: 80.0})
    assert sum(f.size for f in fills) == 80.0


def test_tape_never_overfills_the_order():
    eng = _engine_with(0.50, 120, {0.50: 0.0})            # first in queue
    fills = eng.on_book("T", {0.50: 0.0}, 2.0, traded={0.50: 5000.0})
    assert sum(f.size for f in fills) == 120.0


# --- crossing (the balance hedge) -------------------------------------------

def test_a_bid_resting_at_the_ask_never_fills():
    """REGRESSION: the balance hedge was `post()` at the best ask.

    Under the fixed model that is a passive bid alone at its price, so no
    bid-side delta can be attributed to it -- 0 of 150 shares even as the book
    trades straight down through the level. Before the phantom-fill fix it
    filled instantly and in full, which is the only reason the hedge ever
    looked like it worked.
    """
    eng = QueueFillEngine()
    eng.post("T", "UP", 0.51, 150, {0.50: 300.0, 0.49: 200.0}, 0.0)
    for t, bids in enumerate([{0.50: 300.0}, {0.50: 250.0},
                              {0.50: 0.0, 0.49: 180.0}, {0.49: 100.0}], start=1):
        eng.on_book("T", bids, float(t))
    assert eng.filled_shares() == 0.0


def test_cross_takes_real_depth_at_real_prices():
    eng = QueueFillEngine()
    fills = eng.cross("T", "UP", 150, {0.51: 100.0, 0.52: 80.0}, 1.0)
    assert [(f.price, f.size) for f in fills] == [(0.51, 100.0), (0.52, 50.0)]
    assert all(f.reason == "cross" for f in fills)
    assert eng.filled_shares() == 150.0


def test_cross_is_partial_when_the_book_is_thin():
    """A hedge that cannot be filled is a real outcome, not an error."""
    eng = QueueFillEngine()
    fills = eng.cross("T", "UP", 150, {0.51: 40.0}, 1.0)
    assert sum(f.size for f in fills) == 40.0


def test_cross_stops_at_max_price():
    """A thin book must not drag the hedge up to a guaranteed loss."""
    eng = QueueFillEngine()
    fills = eng.cross("T", "UP", 150, {0.51: 50.0, 0.60: 500.0}, 1.0,
                      max_price=0.55)
    assert sum(f.size for f in fills) == 50.0


def test_crossed_shares_are_excluded_from_fill_rate():
    """Fill rate answers 'do resting orders get filled?'. Taking is not that."""
    eng = QueueFillEngine()
    eng.post("T", "UP", 0.50, 100, {0.50: 999.0}, 0.0)
    eng.cross("T", "UP", 100, {0.51: 500.0}, 1.0)
    assert eng.filled_shares() == 100.0                       # the crossed lot
    assert eng.filled_shares(include_crossed=False) == 0.0
    assert eng.fill_rate() == 0.0                             # not 1.0


def test_fill_reason_separates_observed_queue_from_swept_remainder():
    """A swept level and a shrinking queue are not equally good evidence.

    The sweep branch credits our entire remainder off one observation and
    cannot tell a mass cancel from a mass trade, so any fill rate has to be
    reportable with it split out.
    """
    eng = _engine_with(0.50, 120, {0.50: 100.0})     # 100 queued ahead of us
    assert eng.on_book("T", {0.50: 250.0}, 2.0) == []   # joiners land behind us
    q = eng.on_book("T", {0.50: 60.0}, 3.0)          # 190 gone: 100 ahead, 90 ours
    assert [f.reason for f in q] == ["queue"]
    assert sum(f.size for f in q) == 90.0
    s = eng.on_book("T", {0.50: 0.0, 0.49: 10.0}, 4.0)  # level swept
    assert [f.reason for f in s] == ["sweep"]
    assert sum(f.size for f in s) == 30.0            # the rest, in one credit


def test_drained_level_is_not_counted_as_the_best_bid():
    """REGRESSION: `max(bids)` ignored size, so {0.50: 0.0} still read as a bid.

    That kept "the market moved below us" permanently false at the exact level
    that had just been swept -- the one situation the branch exists to catch.
    """
    eng = _engine_with(0.50, 120, {0.50: 0.0})          # we are first in queue
    eng._last_book["T"] = {0.50: 60.0}                  # 60 shares join our level
    fills = eng.on_book("T", {0.50: 0.0, 0.49: 80.0}, 3.0)   # level swept
    # Old behaviour: best_bid read as 0.50 (the drained level), so the sweep
    # branch never fired and only the 60 observed shares filled. Correct
    # behaviour: best bid is 0.49, the market moved below us, remainder trades.
    assert sum(f.size for f in fills) == 120.0
