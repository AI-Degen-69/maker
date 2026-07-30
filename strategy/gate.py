"""Quality gate: widen before exiting.

A market priced one cent too aggressively and a market full of informed flow
look identical on a single reading. Only the second stays negative after we
back off, and only the second is worth giving up the rent for -- so the
response is graduated rather than a single kill switch.

Widening keeps us inside the 4.5c reward window, which means a WIDENED market
still earns. EXITED is the only state that forfeits income, and it is reached
only after the market has been given a second full sample to recover.
"""
from __future__ import annotations

NORMAL, WIDENED, EXITED = "NORMAL", "WIDENED", "EXITED"


def offset_for(state: str, base: float, widened: float) -> float:
    """How far under mid to quote, given the market's gate state."""
    return widened if state == WIDENED else base


def next_state(state: str, stats: dict, cfg) -> str:
    """Advance the state machine on one markout reading.

    Deliberately conservative in two places:

      * `insufficient_sample` never moves the state. On a thin, long-dated
        book a handful of fills is noise, and evicting a sound market on noise
        costs real rent for no reason.
      * Leaving WIDENED for EXITED demands twice `markout_min_sample`. One
        sample got us into WIDENED; surrendering the income needs more
        evidence than that.

    Both concessions are arguments about SMALL losses, and neither survives a
    catastrophic one -- see the magnitude bypass below.

    EXITED is terminal. A market that kept picking us off after we had already
    backed off has earned a permanent seat out, and re-entering on a noisy
    recovery reading is how a gate turns into an oscillator.
    """
    if state == EXITED:
        return EXITED
    if stats.get("verdict") == "insufficient_sample":
        return state
    mean = stats.get("mean_per_share")
    if mean is None:
        return state

    # MAGNITUDE BYPASS. Graduation is a response to AMBIGUITY: a small negative
    # markout could be one cent of mispricing, so we widen and look again. At
    # -2c/share there is no ambiguity left to resolve -- four times the widen
    # threshold and more than a full taker fee, a loss no offset inside the
    # 4.5c reward window can quote its way out of. Both concessions above then
    # become actively expensive: WIDENED keeps us in the book, and the second
    # full sample the WIDENED->EXITED rule demands is another
    # `markout_min_sample` fills bought at that rate. Exit now, from whichever
    # state we are in, skipping WIDENED entirely.
    #
    # This bypasses the sample DOUBLING, not the sample MINIMUM: the
    # insufficient_sample guard above still stands, so a handful of bad fills
    # on a thin book cannot trigger it.
    if mean < cfg.markout_catastrophic_threshold:
        return EXITED

    losing = mean < cfg.markout_widen_threshold
    if state == NORMAL:
        return WIDENED if losing else NORMAL
    if losing and stats.get("n", 0) >= 2 * cfg.markout_min_sample:
        return EXITED
    return NORMAL if not losing else WIDENED
