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


def test_queue_clearing_then_sweep_is_observed_but_not_credited():
    """Was: the queue clears, the level is swept, and we credit the lot.

    U1 keeps the arithmetic and withdraws the credit. Without tape this is
    documented optimistic bias #1 -- a sweep cannot tell a mass cancel from a
    mass trade -- so the 120 is recorded as an unverified candidate and never
    reaches inventory.
    """
    eng = _engine_with(0.50, 120, {0.50: 300.0})
    assert eng.on_book("T", {0.50: 100.0}, 2.0) == []   # 200 traded, all ahead of us
    assert eng.on_book("T", {0.50: 0.0, 0.49: 50.0}, 3.0) == []
    assert eng.filled_shares() == 0.0
    assert eng.unverified_shares() == 120.0


def test_level_growing_never_fills_us():
    eng = _engine_with(0.50, 120, {0.50: 100.0})
    assert eng.on_book("T", {0.50: 400.0}, 2.0) == []   # joiners behind us
    assert eng.open_orders()[0].queue_ahead == 100.0


def test_shadow_candidate_never_exceeds_order_size():
    """A 9999-share level vanishing cannot imply more than we ever posted.
    The cap survives the move to shadow accounting -- an unbounded candidate
    would corrupt the verified ratio just as an unbounded fill corrupted
    inventory."""
    eng = _engine_with(0.50, 100, {0.50: 0.0})
    eng._last_book["T"] = {0.50: 9999.0}
    assert eng.on_book("T", {0.50: 0.0, 0.49: 1.0}, 3.0) == []
    assert eng.filled_shares() == 0.0
    assert eng.unverified_shares() == 100.0


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
    """The correction that matters. An emptied level used to hand us the whole
    order; the tape shows nothing traded, so nothing filled.

    Both calls now credit zero -- absent tape is no longer a licence to guess.
    The book-only call still *observes* the 120 the old model would have
    credited, as an unverified candidate.
    """
    eng = _engine_with(0.50, 120, {0.50: 300.0})
    book_only = eng.on_book("T", {0.50: 0.0, 0.49: 40.0}, 2.0)
    assert book_only == []                                # no tape, no credit
    assert eng.unverified_shares() == 120.0               # what the old model claimed

    eng2 = _engine_with(0.50, 120, {0.50: 300.0})
    with_tape = eng2.on_book("T", {0.50: 0.0, 0.49: 40.0}, 2.0, traded={})
    assert with_tape == []
    assert eng2.fill_rate() == 0.0


# --- U1: the tape gate is load-bearing --------------------------------------
#
# Every test below fails against the pre-U1 engine, which fell through to the
# cancel-ambiguous delta path whenever `traded` was None. Measured on the
# 18.7h run, that path produced 246 of 282 fills while only 2 were tape-backed,
# so the entire fill rate rested on the one branch nobody could verify.

def test_absent_tape_credits_nothing_and_records_the_candidate():
    """REGRESSION (U1). `traded=None` means the tape could not be read, not
    that the book delta may be trusted. Nothing is credited, and the shares the
    old model would have handed us are recorded so the ratio is measurable."""
    eng = _engine_with(0.50, 120, {0.50: 0.0})   # first in queue, nothing ahead
    eng._last_book["T"] = {0.50: 80.0}           # level shrank by 80
    assert eng.on_book("T", {0.50: 0.0, 0.49: 5.0}, 2.0) == []
    assert eng.filled_shares() == 0.0
    assert eng.unverified_shares() > 0.0


def test_empty_tape_and_absent_tape_both_credit_zero():
    """`{}` (tape read, nothing traded) and `None` (tape unreadable) must agree
    on the credit. They differ only in that one is evidence and the other is a
    gap -- which is why the gap is counted separately rather than dropped."""
    shrink = {0.50: 0.0, 0.49: 40.0}
    no_tape = _engine_with(0.50, 120, {0.50: 300.0})
    no_tape.on_book("T", shrink, 2.0)
    empty_tape = _engine_with(0.50, 120, {0.50: 300.0})
    empty_tape.on_book("T", shrink, 2.0, traded={})

    assert no_tape.filled_shares() == empty_tape.filled_shares() == 0.0
    assert no_tape.unverified_shares() > 0.0      # a gap we could not verify
    assert empty_tape.unverified_shares() == 0.0  # measured: nothing traded


def test_partial_tape_credits_only_the_supported_quantity():
    """Tape volume below the observed shrink splits the difference: the covered
    part is a fill, the rest is a candidate we cannot stand behind."""
    eng = _engine_with(0.50, 120, {0.50: 0.0})    # first in queue, nothing ahead
    fills = eng.on_book("T", {0.50: 0.0, 0.49: 5.0}, 2.0, traded={0.50: 30.0})
    assert sum(f.size for f in fills) == 30.0
    assert all(f.reason == "tape" for f in fills)


def test_static_book_records_no_unverified_candidate():
    """The `before > 1e-9` guard still holds. Resting above the best bid on a
    book that never moved is not an unverifiable fill -- it is no fill at all,
    and must not inflate the unverified count."""
    static = {0.50: 300.0, 0.49: 500.0}
    eng = _engine_with(0.52, 120, static)
    assert eng.on_book("T", static, 2.0) == []
    assert eng.unverified_shares() == 0.0


def test_first_snapshot_records_no_unverified_candidate():
    """Priming needs two snapshots. The first establishes `prev` and cannot
    imply anything -- credited or otherwise."""
    eng = QueueFillEngine()
    eng.post("T", "UP", 0.50, 120, {0.50: 300.0}, 0.0)
    assert eng.on_book("T", {0.50: 0.0}, 1.0) == []
    assert eng.unverified_shares() == 0.0
    assert eng.filled_shares() == 0.0


def test_verified_ratio_is_readable():
    """The number the Phase A decision gate reads. Undefined with no
    observations at all, rather than a misleading 1.0 or 0.0."""
    empty = QueueFillEngine()
    assert empty.verified_ratio() is None

    eng = _engine_with(0.50, 100, {0.50: 0.0})
    eng.on_book("T", {0.50: 0.0}, 2.0, traded={0.50: 40.0})   # 40 credited
    assert eng.verified_ratio() == 1.0

    eng2 = _engine_with(0.50, 100, {0.50: 300.0})
    eng2.on_book("T", {0.50: 0.0, 0.49: 9.0}, 2.0)            # unverified only
    assert eng2.verified_ratio() == 0.0


def test_shadow_accounting_does_not_disturb_queue_position():
    """The unverified count is an observation, not a second engine. Computing
    it must never advance our queue -- otherwise measuring the gap would change
    the fills we credit, and the ratio would measure itself."""
    eng = _engine_with(0.50, 120, {0.50: 300.0})
    eng.on_book("T", {0.50: 200.0}, 2.0)                      # no tape
    queue_after_shadow = eng.open_orders()[0].queue_ahead

    eng2 = _engine_with(0.50, 120, {0.50: 300.0})
    eng2.on_book("T", {0.50: 200.0}, 2.0, traded={})          # measured, zero
    assert eng2.open_orders()[0].queue_ahead == queue_after_shadow


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

    U1 resolves the split by refusing both: neither a shrinking queue nor a
    swept level is evidence on its own, so both land in the unverified list
    under one reason. The quantities are unchanged -- 90 from the queue, then
    the 30 remaining on the sweep -- which is what keeps the ratio meaningful.
    """
    eng = _engine_with(0.50, 120, {0.50: 100.0})     # 100 queued ahead of us
    assert eng.on_book("T", {0.50: 250.0}, 2.0) == []   # joiners land behind us
    assert eng.on_book("T", {0.50: 60.0}, 3.0) == []  # 190 gone: 100 ahead, 90 ours
    assert eng.unverified_shares() == 90.0
    assert eng.on_book("T", {0.50: 0.0, 0.49: 10.0}, 4.0) == []   # level swept
    assert eng.unverified_shares() == 120.0          # 90 + the 30 remaining
    # Both unverified, but the split is preserved: the queue delta observed
    # consumption, the sweep only observed absence.
    assert [f.reason for f in eng.unverified] == [
        "unverified_queue", "unverified_sweep"]
    assert eng.filled_shares() == 0.0


def test_drained_level_is_not_counted_as_the_best_bid():
    """REGRESSION: `max(bids)` ignored size, so {0.50: 0.0} still read as a bid.

    That kept "the market moved below us" permanently false at the exact level
    that had just been swept -- the one situation the branch exists to catch.

    The `_live` size filter that fixed it still runs, and still feeds the
    sweep test -- it just decides the size of an unverified candidate now
    rather than the size of a credit. Losing it would silently shrink every
    candidate and flatter the verified ratio.
    """
    eng = _engine_with(0.50, 120, {0.50: 0.0})          # we are first in queue
    eng._last_book["T"] = {0.50: 60.0}                  # 60 shares join our level
    assert eng.on_book("T", {0.50: 0.0, 0.49: 80.0}, 3.0) == []   # level swept
    # Buggy best_bid (0.50, the drained level) would keep the sweep test false
    # and record only the 60 observed shares. Correct best bid is 0.49: the
    # market moved below us, so the whole remainder is the candidate.
    assert eng.unverified_shares() == 120.0
