"""Fill-model tests.

The repo shipped with no tests at all, while its research log claimed "Seven
unit tests cover queue precedence, sweeps and overfill". The two regressions at
the bottom are for bugs that were live in the model that produced every maker
result recorded so far, so those numbers should not be trusted.
"""
import sys
from pathlib import Path

import pytest

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


def test_tape_fill_retains_originating_quote_id():
    eng = QueueFillEngine()
    order = eng.post("T", "UP", 0.50, 100, {0.50: 0.0}, 0.0)
    order.quote_id = 42
    eng.on_book("T", {0.50: 0.0}, 1.0)
    fills = eng.on_book("T", {0.50: 0.0}, 2.0,
                       traded={0.50: 25.0})
    assert len(fills) == 1
    assert fills[0].quote_id == 42


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


# --- reconciliation: making a zero provable ---------------------------------
#
# The 2026-08-01 run recorded 1,027 quotes, 0 fills and 29,986 empty tape
# reads, and could not say WHY the zero happened. "Nothing traded at our
# price" and "plenty traded but we were behind the queue" are the same zero in
# the fills table and completely different facts about the strategy. Every
# resting order now leaves one reconciliation row per snapshot saying which.


def test_a_tape_print_at_our_price_that_fills_us_is_marked_credited():
    eng = _engine_with(0.50, 120, {0.50: 0.0})   # first in queue
    eng.on_book("T", {0.50: 0.0, 0.49: 5.0}, 2.0, traded={0.50: 30.0})
    r = eng.reconciliation[-1]
    assert r.outcome == "credited"
    assert r.credited == 30.0
    assert r.tape_volume == 30.0
    assert r.queue_ahead == 0.0


def test_a_tape_print_we_sat_behind_is_marked_behind_queue():
    """The distinction the fills table cannot make. 200 shares traded at our
    exact price and we got none of them, because 300 were queued in front --
    that is a queue-position result, not an absence of trading."""
    eng = _engine_with(0.50, 120, {0.50: 300.0})
    eng.on_book("T", {0.50: 100.0}, 2.0, traded={0.50: 200.0})
    r = eng.reconciliation[-1]
    assert r.outcome == "behind_queue"
    assert r.credited == 0.0
    assert r.tape_volume == 200.0
    assert r.queue_ahead == 300.0


def test_a_read_tape_with_nothing_at_our_price_is_marked_no_trade():
    """The 2026-08-01 case: the tape was read, the market simply did not
    trade where we were resting."""
    eng = _engine_with(0.50, 120, {0.50: 300.0})
    eng.on_book("T", {0.50: 300.0}, 2.0, traded={})
    r = eng.reconciliation[-1]
    assert r.outcome == "no_trade_at_price"
    assert r.tape_volume == 0.0


def test_a_trade_away_from_our_price_is_not_counted_as_a_print_for_us():
    """Volume elsewhere in the book is not evidence about our price level."""
    eng = _engine_with(0.50, 120, {0.50: 300.0})
    eng.on_book("T", {0.50: 300.0}, 2.0, traded={0.45: 900.0})
    r = eng.reconciliation[-1]
    assert r.outcome == "no_trade_at_price"
    assert r.tape_volume == 0.0


def test_an_unreadable_tape_is_marked_separately_from_a_silent_one():
    """`None` is a gap in evidence, `{}` is evidence. Collapsing them would
    make an outage look like a quiet market."""
    eng = _engine_with(0.50, 120, {0.50: 300.0})
    eng.on_book("T", {0.50: 200.0}, 2.0, traded=None)
    assert eng.reconciliation[-1].outcome == "tape_unavailable"


def test_reconciliation_records_one_row_per_open_order_per_snapshot():
    eng = QueueFillEngine()
    eng.post("T", "UP", 0.50, 120, {0.50: 10.0}, 0.0)
    eng.post("T", "UP", 0.49, 120, {0.49: 10.0}, 0.0)
    eng.on_book("T", {0.50: 10.0, 0.49: 10.0}, 1.0, traded={})  # establishes prev
    before = len(eng.reconciliation)
    eng.on_book("T", {0.50: 10.0, 0.49: 10.0}, 2.0, traded={})
    assert len(eng.reconciliation) - before == 2


def test_reconciliation_does_not_disturb_the_fills_it_observes():
    """Pure observation. The engine must credit exactly what it credited
    before reconciliation existed, or the instrument is changing the reading."""
    eng = _engine_with(0.50, 120, {0.50: 300.0})
    fills = eng.on_book("T", {0.50: 100.0}, 2.0, traded={0.50: 400.0})
    assert sum(f.size for f in fills) == 100.0    # 400 traded - 300 ahead
    assert eng.open_orders()[0].queue_ahead == 0.0


def test_no_reconciliation_row_before_the_first_delta():
    """A single snapshot establishes `prev` and implies nothing, so it must
    not record a 'no trade' row that would dilute the rate."""
    eng = QueueFillEngine()
    eng.post("T", "UP", 0.50, 120, {0.50: 10.0}, 0.0)
    eng.on_book("T", {0.50: 10.0}, 1.0, traded={})
    assert eng.reconciliation == []


# --- the amendment penalty (Phase 1, component 1) ---------------------------
#
# A repriced order is a NEW order at the back of the new level's queue. The
# rule has no free parameter, so unlike the other two haircut components it is
# asserted outright rather than estimated -- and nothing here may assert on the
# fill-rate gap, or the test would re-encode the joint fit the spec forbids.

def test_amendment_resets_queue_to_the_new_level_depth():
    """P1 -> P2 joins the back of P2's queue. No seniority is inherited."""
    eng = QueueFillEngine()
    o = eng.post("T", "UP", 0.50, 120, {0.50: 300.0}, 0.0)
    eng.on_book("T", {0.50: 300.0}, 1.0, traded={})             # establishes prev
    eng.on_book("T", {0.50: 60.0}, 2.0, traded={0.50: 240.0})   # worked down to 60
    assert o.queue_ahead == 60.0

    eng.amend(o, 0.49, {0.50: 60.0, 0.49: 800.0}, 3.0)
    assert o.price == 0.49
    assert o.queue_ahead == 800.0        # NOT 60 -- the earned position is gone


def test_amendment_to_an_empty_level_still_starts_from_zero_not_seniority():
    """An empty new level means nothing is ahead of us -- which is a fact about
    the level, not a carried-over privilege. The distinction matters because
    both readings produce 0.0 here and only one of them is a rule."""
    eng = QueueFillEngine()
    o = eng.post("T", "UP", 0.50, 120, {0.50: 300.0}, 0.0)
    eng.amend(o, 0.51, {0.50: 300.0}, 1.0)
    assert o.price == 0.51
    assert o.queue_ahead == 0.0


def test_amendment_keeps_filled_shares_and_requeues_only_the_remainder():
    eng = QueueFillEngine()
    o = eng.post("T", "UP", 0.50, 120, {0.50: 100.0}, 0.0)
    eng.on_book("T", {0.50: 100.0}, 1.0, traded={})
    eng.on_book("T", {0.50: 100.0}, 2.0, traded={0.50: 150.0})   # 150 - 100 = 50
    assert o.filled == 50.0

    eng.amend(o, 0.48, {0.48: 400.0}, 3.0)
    assert o.filled == 50.0              # done is done
    assert o.remaining == 70.0           # only the rest goes back in the queue
    assert o.queue_ahead == 400.0


def test_same_price_amendment_is_not_an_amendment():
    """The sweep deliberately leaves an unchanged order alone because
    requoting loses queue position. A no-op call must not punish that path."""
    eng = QueueFillEngine()
    o = eng.post("T", "UP", 0.50, 120, {0.50: 300.0}, 0.0)
    eng.on_book("T", {0.50: 300.0}, 1.0, traded={})
    eng.on_book("T", {0.50: 60.0}, 2.0, traded={0.50: 240.0})
    assert o.queue_ahead == 60.0

    eng.amend(o, 0.50, {0.50: 60.0}, 3.0)
    assert o.queue_ahead == 60.0         # position held, not reset to 60 by luck
    assert o.posted_ts == 0.0            # untouched: no new order was created


def test_amended_order_fills_only_past_the_new_queue():
    """The penalty has to bite in the crediting path, not just in the field."""
    eng = QueueFillEngine()
    o = eng.post("T", "UP", 0.50, 120, {0.50: 0.0}, 0.0)
    assert o.queue_ahead == 0.0          # front of an empty level

    eng.amend(o, 0.49, {0.49: 500.0}, 1.0)
    eng.on_book("T", {0.49: 500.0}, 2.0, traded={})
    assert eng.on_book("T", {0.49: 500.0}, 3.0, traded={0.49: 400.0}) == []
    assert eng.filled_shares() == 0.0    # 400 traded, 500 ahead: nothing is ours


def test_cancel_and_repost_loses_position_the_same_way_an_amendment_does():
    """The path the sweep actually takes today. Locked so a future reprice
    cannot start inheriting seniority through the back door."""
    eng = QueueFillEngine()
    a = eng.post("T", "UP", 0.50, 120, {0.50: 300.0}, 0.0)
    eng.on_book("T", {0.50: 300.0}, 1.0, traded={})
    eng.on_book("T", {0.50: 60.0}, 2.0, traded={0.50: 240.0})
    assert a.queue_ahead == 60.0

    a.cancelled = True
    b = eng.post("T", "UP", 0.49, 120, {0.50: 60.0, 0.49: 800.0}, 3.0)
    assert b.queue_ahead == 800.0
    assert eng.open_orders() == [b]


# --- the cancellation race (Phase 1, component 2) --------------------------
#
# A cancel sent at t_c takes tau_cancel = net_oneway + venue_ack to acknowledge.
# In-flight trades landing during [t_c, t_c + tau_cancel] are credited as
# reason="race", outcome="race_loss" at expected-value p_race = min(1.0, tau / dt).

def test_cancel_sets_timestamp_and_reason():
    eng = QueueFillEngine()
    o = eng.post("T", "UP", 0.50, 100.0, {0.50: 0.0}, 0.0)
    assert not o.cancelled
    assert o.cancelled_ts is None
    assert o.cancel_reason == ""

    eng.cancel("T", ts=1.5, reason="stale_quote")
    assert o.cancelled
    assert o.cancelled_ts == 1.5
    assert o.cancel_reason == "stale_quote"
    assert eng.open_orders("T") == []


def test_trade_within_cancel_race_window_credits_race_loss():
    # tau = 100ms + 150ms = 250ms = 0.25s
    eng = QueueFillEngine(cancel_net_oneway_ms=100.0, cancel_venue_ack_ms=150.0)
    o = eng.post("T", "UP", 0.50, 100.0, {0.50: 0.0}, 0.0)
    eng.on_book("T", {0.50: 0.0}, 1.0, traded={})             # establishes prev at t=1.0

    eng.cancel("T", ts=1.0, reason="requote")                # cancelled at t=1.0

    # Snapshot arrives at t=2.1.
    # dt_poll = 2.1 - 1.0 = 1.1s. overlap = min(2.1, 1.25) - 1.0 = 0.25s.
    # p_race = 0.25 / 1.1 = 0.2272727...
    # Tape trades 80 shares. qty_exposed = 80. qty_race = 80 * (0.25 / 1.1)
    fills = eng.on_book("T", {0.50: 0.0}, 2.1, traded={0.50: 80.0})
    assert len(fills) == 1
    f = fills[0]
    expected_qty = 80.0 * (0.25 / 1.1)
    assert f.reason == "race"
    assert f.size == pytest.approx(expected_qty)
    assert o.filled == pytest.approx(expected_qty)
    assert eng.reconciliation[-1].outcome == "race_loss"
    assert eng.reconciliation[-1].credited == pytest.approx(expected_qty)


def test_trade_after_cancel_race_window_is_clean_cancel_no_fill():
    eng = QueueFillEngine(cancel_net_oneway_ms=100.0, cancel_venue_ack_ms=150.0)
    o = eng.post("T", "UP", 0.50, 100.0, {0.50: 0.0}, 0.0)
    eng.on_book("T", {0.50: 0.0}, 1.0, traded={})

    eng.cancel("T", ts=1.0, reason="requote")
    # Poll at t=1.5 processes the in-flight window [1.0, 1.25]
    eng.on_book("T", {0.50: 0.0}, 1.5, traded={})

    # Snapshot at t=2.5 (prev_ts=1.5 > 1.25 ack) -> order no longer race-eligible
    fills = eng.on_book("T", {0.50: 0.0}, 2.5, traded={0.50: 80.0})
    assert fills == []
    assert o.filled == 0.0
    assert eng.filled_shares() == 0.0


def test_cancelled_order_not_double_counted():
    """An order undergoing a race loss must not also be in open_orders."""
    eng = QueueFillEngine(cancel_net_oneway_ms=100.0, cancel_venue_ack_ms=150.0)
    o = eng.post("T", "UP", 0.50, 100.0, {0.50: 0.0}, 0.0)
    eng.on_book("T", {0.50: 0.0}, 1.0, traded={})
    eng.cancel("T", ts=1.0, reason="requote")

    assert eng.open_orders("T") == []
    assert len(eng.race_eligible_orders("T", ts=1.2, prev_ts=1.0)) == 1

    fills = eng.on_book("T", {0.50: 0.0}, 1.2, traded={0.50: 50.0})
    assert len(fills) == 1
    assert fills[0].reason == "race"


def test_zero_tau_cancels_with_zero_race_exposure():
    eng = QueueFillEngine(cancel_net_oneway_ms=0.0, cancel_venue_ack_ms=0.0)
    o = eng.post("T", "UP", 0.50, 100.0, {0.50: 0.0}, 0.0)
    eng.on_book("T", {0.50: 0.0}, 1.0, traded={})
    eng.cancel("T", ts=1.0, reason="requote")

    # At tau=0, snapshot at t=2.01 has overlap=0 -> race_eligible_orders is empty
    fills = eng.on_book("T", {0.50: 0.0}, 2.01, traded={0.50: 50.0})
    assert fills == []
    assert o.filled == 0.0


def test_sub_tau_poll_interval_sets_p_race_to_one():
    # tau = 0.25s. dt_poll = 0.20s <= tau -> p_race = 1.0
    eng = QueueFillEngine(cancel_net_oneway_ms=100.0, cancel_venue_ack_ms=150.0)
    o = eng.post("T", "UP", 0.50, 100.0, {0.50: 0.0}, 0.0)
    eng.on_book("T", {0.50: 0.0}, 1.0, traded={})
    eng.cancel("T", ts=1.0, reason="requote")

    fills = eng.on_book("T", {0.50: 0.0}, 1.2, traded={0.50: 40.0})
    assert len(fills) == 1
    assert fills[0].size == pytest.approx(40.0)    # p_race = 1.0 -> 100% credited
    assert fills[0].reason == "race"


# --- Post latency penalty tests (issue #27 Phase 1 component 3) -----------

def test_trade_before_arrival_yields_not_yet_arrived_and_zero_fill():
    # tau_post = 100ms net + 50ms accept = 150ms = 0.15s
    eng = QueueFillEngine(net_oneway_ms=100.0, post_venue_accept_ms=50.0)
    eng.on_book("T", {0.50: 100.0}, 1.0, traded={})      # prime at t=1.0
    o = eng.post("T", "UP", 0.50, 100.0, {0.50: 0.0}, ts=1.0)  # arrival_ts = 1.15

    # Snapshot at t=1.10 (ts <= 1.15 arrival) -> f = 0.0
    fills = eng.on_book("T", {0.50: 100.0}, 1.10, traded={0.50: 50.0})
    assert fills == []
    assert o.filled == 0.0
    assert o.queue_ahead == 0.0                         # queue_ahead unchanged
    assert eng.reconciliation[-1].outcome == "not_yet_arrived"
    assert eng.reconciliation[-1].credited == 0.0


def test_trade_overlapping_arrival_scales_effective_volume():
    # tau_post = 0.15s. posted at t=1.0 -> arrival_ts = 1.15
    eng = QueueFillEngine(net_oneway_ms=100.0, post_venue_accept_ms=50.0)
    eng.on_book("T", {0.50: 0.0}, 1.0, traded={})       # prev_ts = 1.0
    o = eng.post("T", "UP", 0.50, 100.0, {0.50: 0.0}, ts=1.0)

    # Snapshot at t=2.0 (dt_poll = 1.0s, overlap = 2.0 - 1.15 = 0.85s -> f = 0.85)
    fills = eng.on_book("T", {0.50: 0.0}, 2.0, traded={0.50: 100.0})
    assert len(fills) == 1
    assert fills[0].size == pytest.approx(85.0)
    assert fills[0].reason == "tape"                    # ordinary queue fill, not split
    assert o.filled == pytest.approx(85.0)
    assert eng.reconciliation[-1].outcome == "credited"
    assert eng.reconciliation[-1].credited == pytest.approx(85.0)


def test_subsequent_poll_has_f_equals_one():
    # tau_post = 0.15s. posted at t=1.0 -> arrival_ts = 1.15
    eng = QueueFillEngine(net_oneway_ms=100.0, post_venue_accept_ms=50.0)
    eng.on_book("T", {0.50: 0.0}, 1.0, traded={})
    o = eng.post("T", "UP", 0.50, 100.0, {0.50: 0.0}, ts=1.0)

    # First poll at t=2.0 (credits 85 shares, leaving 15)
    eng.on_book("T", {0.50: 0.0}, 2.0, traded={0.50: 100.0})
    assert o.remaining == pytest.approx(15.0)

    # Second poll at t=3.0 (prev_ts = 2.0 >= arrival_ts 1.15 -> f = 1.0)
    fills2 = eng.on_book("T", {0.50: 0.0}, 3.0, traded={0.50: 20.0})
    assert len(fills2) == 1
    assert fills2[0].size == pytest.approx(15.0)        # fills remaining 15 at 100% f
    assert o.remaining == 0.0


def test_partial_interval_behind_queue_remains_behind_queue():
    # tau_post = 0.15s. posted at t=1.0 -> arrival_ts = 1.15. queue_ahead = 100.
    eng = QueueFillEngine(net_oneway_ms=100.0, post_venue_accept_ms=50.0)
    eng.on_book("T", {0.50: 100.0}, 1.0, traded={})
    o = eng.post("T", "UP", 0.50, 100.0, {0.50: 100.0}, ts=1.0)

    # Snapshot at t=2.0: f = 0.85. t_vol = 100 -> t_vol_eff = 85.
    # 85 < 100 queue_ahead -> qty = 0, outcome = behind_queue
    fills = eng.on_book("T", {0.50: 100.0}, 2.0, traded={0.50: 100.0})
    assert fills == []
    assert o.filled == 0.0
    assert o.queue_ahead == pytest.approx(15.0)         # 100 - 85 = 15 ahead
    assert eng.reconciliation[-1].outcome == "behind_queue"


def test_zero_tau_post_gives_full_immediate_eligibility():
    eng = QueueFillEngine(net_oneway_ms=0.0, post_venue_accept_ms=0.0)
    eng.on_book("T", {0.50: 0.0}, 1.0, traded={})
    o = eng.post("T", "UP", 0.50, 100.0, {0.50: 0.0}, ts=1.0)

    # Snapshot at t=1.1 with tau_post=0 -> f = 1.0
    fills = eng.on_book("T", {0.50: 0.0}, 1.1, traded={0.50: 50.0})
    assert len(fills) == 1
    assert fills[0].size == pytest.approx(50.0)
    assert o.filled == pytest.approx(50.0)


def test_refactored_config_latency_windows():
    eng = QueueFillEngine(net_oneway_ms=100.0, cancel_venue_ack_ms=150.0, post_venue_accept_ms=50.0)
    assert eng.tau_cancel_sec == pytest.approx(0.25)
    assert eng.tau_post_sec == pytest.approx(0.15)


def test_small_dt_multi_interval_arrival_progression():
    """At 50ms poll cadence and tau_post=150ms, order spans multiple intervals before arriving."""
    # tau_post = 0.15s (150ms)
    eng = QueueFillEngine(net_oneway_ms=100.0, post_venue_accept_ms=50.0)
    eng.on_book("T", {0.50: 0.0}, 1.000, traded={})
    o = eng.post("T", "UP", 0.50, 100.0, {0.50: 0.0}, ts=1.000) # arrival at 1.150s

    # Poll 1 at t=1.050 (ts <= arrival) -> f = 0.0, not_yet_arrived
    f1 = eng.on_book("T", {0.50: 0.0}, 1.050, traded={0.50: 20.0})
    assert f1 == []
    assert o.filled == 0.0
    assert eng.reconciliation[-1].outcome == "not_yet_arrived"

    # Poll 2 at t=1.100 (ts <= arrival) -> f = 0.0, not_yet_arrived
    f2 = eng.on_book("T", {0.50: 0.0}, 1.100, traded={0.50: 20.0})
    assert f2 == []
    assert o.filled == 0.0
    assert eng.reconciliation[-1].outcome == "not_yet_arrived"

    # Poll 3 at t=1.200 (prev=1.100 < 1.150 < curr=1.200, dt=0.100s, eligible=(1.200-1.150)/0.100 = 0.50)
    f3 = eng.on_book("T", {0.50: 0.0}, 1.200, traded={0.50: 40.0})
    assert len(f3) == 1
    assert f3[0].size == pytest.approx(20.0) # 0.50 * 40 = 20
    assert o.filled == pytest.approx(20.0)
    assert eng.reconciliation[-1].outcome == "credited"

    # Poll 4 at t=1.250 (prev=1.200 >= arrival=1.150 -> f = 1.0)
    f4 = eng.on_book("T", {0.50: 0.0}, 1.250, traded={0.50: 30.0})
    assert len(f4) == 1
    assert f4[0].size == pytest.approx(30.0)
    assert o.filled == pytest.approx(50.0)
    assert eng.reconciliation[-1].outcome == "credited"


def test_small_dt_cumulative_volume_conservation():
    """Cumulative volume credited across multi-interval arrival matches single-interval path."""
    # Scenario: 200 shares trade uniformly over [1.0, 1.25] (0.8 sh/ms)
    # Order posted at t=1.000 with tau_post = 150ms -> arrival at t=1.150
    # Over [1.0, 1.25], 100ms is eligible (1.150 to 1.250) -> 100ms * 0.8 sh/ms = 80 shares.

    # 1. Multi-interval path: 5 intervals of 50ms
    eng_multi = QueueFillEngine(net_oneway_ms=100.0, post_venue_accept_ms=50.0)
    eng_multi.on_book("T", {0.50: 0.0}, 1.000, traded={})
    o_multi = eng_multi.post("T", "UP", 0.50, 100.0, {0.50: 0.0}, ts=1.000)

    eng_multi.on_book("T", {0.50: 0.0}, 1.050, traded={0.50: 40.0}) # f=0.0 -> 0
    eng_multi.on_book("T", {0.50: 0.0}, 1.100, traded={0.50: 40.0}) # f=0.0 -> 0
    eng_multi.on_book("T", {0.50: 0.0}, 1.150, traded={0.50: 40.0}) # ts=arrival -> f=0.0 -> 0
    eng_multi.on_book("T", {0.50: 0.0}, 1.200, traded={0.50: 40.0}) # prev=1.150 >= arrival -> f=1.0 -> 40
    eng_multi.on_book("T", {0.50: 0.0}, 1.250, traded={0.50: 40.0}) # prev=1.200 >= arrival -> f=1.0 -> 40
    assert o_multi.filled == pytest.approx(80.0)

    # 2. Single interval path: 1 interval of 250ms
    eng_single = QueueFillEngine(net_oneway_ms=100.0, post_venue_accept_ms=50.0)
    eng_single.on_book("T", {0.50: 0.0}, 1.000, traded={})
    o_single = eng_single.post("T", "UP", 0.50, 100.0, {0.50: 0.0}, ts=1.000)
    # interval [1.000, 1.250], dt=0.250, arrival=1.150 -> f = (1.250 - 1.150)/0.250 = 0.100/0.250 = 0.40
    eng_single.on_book("T", {0.50: 0.0}, 1.250, traded={0.50: 200.0}) # 0.40 * 200 = 80
    assert o_single.filled == pytest.approx(80.0)


def test_small_dt_zero_and_negative_interval_guard():
    """Duplicate timestamps (dt <= 0) must not cause division-by-zero or crash."""
    eng = QueueFillEngine(net_oneway_ms=100.0, post_venue_accept_ms=50.0)
    eng.on_book("T", {0.50: 0.0}, 1.000, traded={})
    o = eng.post("T", "UP", 0.50, 100.0, {0.50: 0.0}, ts=1.000)

    # Duplicate timestamp
    f_dup = eng.on_book("T", {0.50: 0.0}, 1.000, traded={0.50: 50.0})
    assert f_dup == []
    assert o.filled == 0.0

    # Rapid micro-burst dt=1ms
    f_burst = eng.on_book("T", {0.50: 0.0}, 1.001, traded={0.50: 50.0})
    assert f_burst == []
    assert o.filled == 0.0


# --- venue minimum fill size (harden-to-reality, Action 2) -----------------
# Polymarket rejects sub-5-share fills. Paper can credit fractional shares
# (e.g. race dust = p_race * exposed, a fraction of the cancel window), so
# QueueFillEngine drops any fill below `min_fill_shares`. These pin that.

def test_race_fill_below_venue_minimum_is_recorded():
    """A tape-confirmed partial fill below 5 shares is valid and recorded.
    The venue minimum is enforced at order submission, not fill recording."""
    # tau = 0.25s. Order of 4.0 shares, full race credit (p_race=1.0).
    eng = QueueFillEngine(cancel_net_oneway_ms=100.0, cancel_venue_ack_ms=150.0)
    o = eng.post("T", "UP", 0.50, 4.0, {0.50: 0.0}, 0.0)
    eng.on_book("T", {0.50: 0.0}, 1.0, traded={})       # establishes prev
    eng.cancel("T", ts=1.0, reason="requote")
    # Snapshot at t=1.2, dt_poll=0.2 <= tau -> p_race=1.0 -> qty_race=4.0 (< 5)
    fills = eng.on_book("T", {0.50: 0.0}, 1.2, traded={0.50: 40.0})
    assert len(fills) == 1, "sub-5-share tape-confirmed fill must be recorded"
    assert fills[0].reason == "race"
    assert fills[0].size == pytest.approx(4.0)
    assert o.filled == pytest.approx(4.0)
    assert eng.filled_shares() == pytest.approx(4.0)


def test_race_fill_at_venue_minimum_is_kept():
    """The boundary: exactly 5 shares (the venue minimum) is a real fill."""
    eng = QueueFillEngine(cancel_net_oneway_ms=100.0, cancel_venue_ack_ms=150.0)
    o = eng.post("T", "UP", 0.50, 5.0, {0.50: 0.0}, 0.0)
    eng.on_book("T", {0.50: 0.0}, 1.0, traded={})       # establishes prev
    eng.cancel("T", ts=1.0, reason="requote")
    fills = eng.on_book("T", {0.50: 0.0}, 1.2, traded={0.50: 50.0})
    assert len(fills) == 1
    assert fills[0].reason == "race"
    assert fills[0].size == pytest.approx(5.0)
    assert o.filled == pytest.approx(5.0)


def test_min_fill_size_default_is_venue_minimum():
    """The engine ships with the Polymarket minimum (5 shares) as the floor."""
    assert QueueFillEngine().min_fill_shares == 5.0


