import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.supervisor import next_restart_delay  # noqa: E402


def test_first_crash_restarts_promptly():
    assert next_restart_delay(1) == pytest.approx(5.0)


def test_repeat_crashes_back_off():
    assert next_restart_delay(3) > next_restart_delay(1)


def test_backoff_is_capped():
    assert next_restart_delay(99) == pytest.approx(60.0)


def test_a_recovered_child_starts_from_the_bottom_again():
    # The caller resets the counter to 0 once a child survives; the delay in
    # that state must not exceed the first-crash delay.
    assert next_restart_delay(0) <= next_restart_delay(1)
