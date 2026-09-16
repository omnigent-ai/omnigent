"""Scaled wall-clock budgets for tests that wait on asynchronous work."""

from __future__ import annotations

import os


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
