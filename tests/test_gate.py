"""Quality gate: widen before exiting.

A market priced 1c too aggressively and a market full of informed flow look
identical on one reading. Only the second stays negative after we back off,
and only the second is worth giving up the rent for.
"""
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.config import load as load_cfg  # noqa: E402
from strategy.gate import next_state, offset_for  # noqa: E402


def _c():
    return dataclasses.replace(load_cfg(), markout_min_sample=20,
                               markout_widen_threshold=-0.005,
                               markout_catastrophic_threshold=-0.020)


def test_thin_sample_never_moves_off_normal():
    """The expensive mistake is evicting a good market on 3 noisy fills."""
    s = {"verdict": "insufficient_sample", "mean_per_share": None, "n": 3}
    assert next_state("NORMAL", s, _c()) == "NORMAL"


def test_losing_market_widens_first():
    s = {"verdict": "losing", "mean_per_share": -0.02, "n": 25}
    assert next_state("NORMAL", s, _c()) == "WIDENED"


def test_still_losing_after_widening_exits():
    """Backed off and still picked off -- that is toxic flow, not mispricing."""
    s = {"verdict": "losing", "mean_per_share": -0.02, "n": 60}
    assert next_state("WIDENED", s, _c()) == "EXITED"


def test_widened_holds_until_a_second_full_sample():
    """One sample got us here; demand another before surrendering the rent."""
    s = {"verdict": "losing", "mean_per_share": -0.02, "n": 25}
    assert next_state("WIDENED", s, _c()) == "WIDENED"


def test_recovery_returns_to_normal():
    s = {"verdict": "earning", "mean_per_share": 0.01, "n": 60}
    assert next_state("WIDENED", s, _c()) == "NORMAL"


def test_exit_is_terminal():
    s = {"verdict": "earning", "mean_per_share": 0.01, "n": 99}
    assert next_state("EXITED", s, _c()) == "EXITED"


def test_a_small_loss_inside_the_threshold_does_not_widen():
    """-0.2c is inside the -0.5c threshold: still profitable against the ~1c
    paired edge, so widening would give up rent for nothing."""
    s = {"verdict": "losing", "mean_per_share": -0.002, "n": 40}
    assert next_state("NORMAL", s, _c()) == "NORMAL"


# --- catastrophic magnitude bypass ------------------------------------------

def test_catastrophic_loss_exits_straight_from_normal():
    """-3c/share is not ambiguity to be resolved by widening. Skip WIDENED."""
    s = {"verdict": "losing", "mean_per_share": -0.03, "n": 25}
    assert next_state("NORMAL", s, _c()) == "EXITED"


def test_catastrophic_loss_ignores_the_doubled_sample_requirement():
    """n=25 is one sample, not the two the WIDENED->EXITED rule demands. At
    this magnitude the second sample is another 20 fills bought at -3c."""
    s = {"verdict": "losing", "mean_per_share": -0.03, "n": 25}
    assert next_state("WIDENED", s, _c()) == "EXITED"


def test_a_merely_bad_loss_still_widens_first():
    """-1c clears the widen threshold but not the catastrophic one, so the
    graduated path is intact -- the bypass is a magnitude rule, not a rename
    of the old one."""
    s = {"verdict": "losing", "mean_per_share": -0.01, "n": 25}
    assert next_state("NORMAL", s, _c()) == "WIDENED"


def test_the_bypass_does_not_override_the_sample_minimum():
    """A catastrophic MEAN over 3 fills is still 3 fills. The bypass drops the
    sample DOUBLING; the noise guard that stops us evicting a sound market on
    a handful of readings is untouched."""
    s = {"verdict": "insufficient_sample", "mean_per_share": -0.05, "n": 3}
    assert next_state("NORMAL", s, _c()) == "NORMAL"


def test_widened_state_quotes_further_from_mid():
    assert offset_for("WIDENED", 0.020, 0.035) == 0.035
    assert offset_for("NORMAL", 0.020, 0.035) == 0.020
    assert offset_for("EXITED", 0.020, 0.035) == 0.020
