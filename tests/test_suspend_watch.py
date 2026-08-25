"""Unit tests for :mod:`omnigent.suspend_watch`.

The suspend watcher must fire exactly once when the wall clock jumps ahead of
the monotonic clock (a real system sleep), must never fire when both clocks
advance together (a merely-blocked event loop), and must survive a callback
that raises. Clocks and the sleeper are injected so the loop is driven
deterministically without touching real time or patching ``asyncio.sleep``
globally (which the ``no-global-asyncio-patch`` lint forbids in tests).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from omnigent.suspend_watch import (
    SUSPEND_GAP_THRESHOLD_S,
    SUSPEND_POLL_INTERVAL_S,
    watch_for_resume,
)


def _clamping_clock(values: list[float]) -> Callable[[], float]:
    """A clock stub that returns *values* in order, repeating the last.

    Clamping (rather than raising ``StopIteration``) keeps the watcher's
    ``while True`` loop from crashing on an over-read after the values that
    matter for the assertion have been consumed.

    :param values: Successive readings the clock should return.
    :returns: A zero-arg callable yielding each value then the final one.
    """
    index = {"i": 0}

    def read() -> float:
        value = values[min(index["i"], len(values) - 1)]
        index["i"] += 1
        return value

    return read


async def _run_watcher(
    *,
    wall: list[float],
    mono: list[float],
    stop_after_sleeps: int,
    on_resume: Callable[[float], None],
) -> None:
    """Drive :func:`watch_for_resume` for a fixed number of polls.

    The injected sleeper raises :class:`asyncio.CancelledError` on the
    *stop_after_sleeps*-th call to end the otherwise-infinite loop, matching
    the idiom used in ``tests/runner/transports/ws_tunnel/test_serve.py``.

    :param wall: Wall-clock readings (clamped).
    :param mono: Monotonic-clock readings (clamped).
    :param stop_after_sleeps: Sleep call on which to cancel the loop.
    :param on_resume: Callback forwarded to the watcher.
    :returns: None.
    """
    calls = {"n": 0}

    async def fake_sleep(_delay: float) -> None:
        calls["n"] += 1
        if calls["n"] >= stop_after_sleeps:
            raise asyncio.CancelledError

    with contextlib.suppress(asyncio.CancelledError):
        await watch_for_resume(
            on_resume,
            wall_clock=_clamping_clock(wall),
            mono_clock=_clamping_clock(mono),
            sleep=fake_sleep,
        )


async def test_fires_once_on_wall_monotonic_divergence() -> None:
    """A wall jump the monotonic clock did not share is reported once.

    Models a real sleep: the wall clock advanced 3600 s across one poll while
    the monotonic clock advanced only 5 s (it froze while suspended). The gap
    (~3595 s) exceeds the threshold, so ``on_resume`` fires exactly once with
    that duration.

    :returns: None.
    """
    fired: list[float] = []
    await _run_watcher(
        wall=[1000.0, 4600.0],
        mono=[0.0, 5.0],
        stop_after_sleeps=2,
        on_resume=lambda gap: fired.append(round(gap, 1)),
    )
    assert fired == [3595.0]


async def test_does_not_fire_when_clocks_advance_together() -> None:
    """A blocked event loop (both clocks jump equally) never fires.

    This is the load-bearing false-positive guard: a long synchronous call or
    GC pause advances the wall AND monotonic clocks by the same amount, so the
    divergence stays ~0 and no resume is reported — only genuinely suspended
    time (which the monotonic clock excludes) can trip the watcher.

    :returns: None.
    """
    fired: list[float] = []
    await _run_watcher(
        wall=[1000.0, 4600.0],
        mono=[0.0, 3600.0],
        stop_after_sleeps=2,
        on_resume=lambda gap: fired.append(gap),
    )
    assert fired == []


async def test_survives_raising_callback() -> None:
    """A callback that raises is swallowed so the watcher keeps running.

    ``on_resume`` aborts a socket and flags a reconnect; a bug there must not
    kill the watcher (which would silently disable wake detection for the rest
    of the process's life).

    :returns: None.
    """
    calls = {"n": 0}

    def raising(_gap: float) -> None:
        calls["n"] += 1
        raise RuntimeError("boom")

    # Loop runs two polls: the first diverges (fires -> raises -> swallowed),
    # the second is quiet, then the sleeper cancels. Reaching the cancel proves
    # the raise did not propagate out of the watcher.
    await _run_watcher(
        wall=[1000.0, 4600.0, 4605.0],
        mono=[0.0, 5.0, 10.0],
        stop_after_sleeps=3,
        on_resume=raising,
    )
    assert calls["n"] == 1


def test_default_constants_are_sane() -> None:
    """The shipped threshold sits above the poll interval and jitter.

    A threshold at or below the interval would risk firing on ordinary
    scheduling delay; keeping it well above documents the intended margin.

    :returns: None.
    """
    assert SUSPEND_GAP_THRESHOLD_S > SUSPEND_POLL_INTERVAL_S
    assert SUSPEND_POLL_INTERVAL_S > 0
