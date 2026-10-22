"""Scaled wall-clock budgets for tests that wait on asynchronous work."""

from __future__ import annotations

import os
import time


def _read_timeout_scale() -> float:
    """Read OMNIGENT_TEST_TIMEOUT_SCALE, ignoring an unusable value."""
    try:
        scale = float(os.environ.get("OMNIGENT_TEST_TIMEOUT_SCALE", "1"))
    except ValueError:
        return 1.0
    return scale if scale > 0 else 1.0


_TIMEOUT_SCALE = _read_timeout_scale()


def budget(seconds: float) -> float:
    """
    Scale a test wall-clock budget by ``OMNIGENT_TEST_TIMEOUT_SCALE``.

    These budgets are hang guards, not latency assertions: they exist so a
    never-arriving message fails one test instead of hanging the suite. A
    shared CI runner under ``-n 4 --dist=worksteal`` can stall an event loop
    well past a 1s budget, which turns a passing test red for reasons that
    have nothing to do with the code under test. CI sets the scale so those
    guards stay loose there while local runs keep failing fast.

    Use it for any wait whose duration is incidental. Do *not* use it to
    paper over a genuine latency assertion — assert on ordering or observable
    state instead.
    """
    return seconds * _TIMEOUT_SCALE


class Deadline:
    """One scaled budget shared across a bounded sequence of awaits.

    A per-await :func:`budget` multiplies: 40 receives at ``budget(3.0)``
    reach 480s under CI's 4x scale, past the lane's own ``--timeout=300``, so
    the hang guard would surface as a suite timeout that kills the xdist
    worker instead of the named assertion the loop ends with. Take one
    deadline for the whole exchange and hand each await what is left.
    """

    def __init__(self, seconds: float) -> None:
        """
        :param seconds: Unscaled budget for the whole exchange, scaled once.
        """
        self._end = time.monotonic() + budget(seconds)

    def remaining(self) -> float:
        """Seconds left, floored at 0 so an expired deadline fails fast."""
        return max(0.0, self._end - time.monotonic())

    def next_wait(self, seconds: float) -> float:
        """
        Return a per-await budget clipped to what the deadline has left.

        Keeps a per-await timeout meaningful — a loop that treats a timeout as
        "the channel went quiet" still gets that signal — while the deadline
        caps what the whole loop can consume.

        :param seconds: Unscaled per-await budget.
        """
        return min(budget(seconds), self.remaining())
