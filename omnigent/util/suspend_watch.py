"""Detect a system suspend/resume (laptop sleep) so live connections can
reconnect promptly instead of waiting out the WebSocket keepalive.

When a laptop's lid closes the OS freezes the whole process and drops the
network. Long-lived WebSockets (the host control channel in
:mod:`omnigent.host.connect`, every runner tunnel in
:mod:`omnigent.runner.transports.ws_tunnel.serve`) become half-open sockets
the peer has already dropped. Nothing notices until the ``websockets``
keepalive ping times out — up to ~120 s (``ping_interval`` 30 s +
``ping_timeout`` 90 s) — during which the host and its sessions look offline
to the server and the desktop app.

:func:`watch_for_resume` gives an event loop a cheap way to react the instant
it wakes: it polls a short interval and compares how far the realtime (wall)
clock advanced against the monotonic clock. The monotonic clock freezes while
the machine is asleep (macOS ``mach_absolute_time`` and Linux
``CLOCK_MONOTONIC`` both exclude sleep) while the realtime clock keeps
counting, so a resume shows up as a large divergence between the two across a
single poll. A merely-blocked event loop (a long synchronous call, a GC pause)
advances *both* clocks equally, so the divergence stays ~0 — this never
false-fires on CPU stalls, only on a real suspend.

Deliberately uses :func:`time.monotonic`, never the event loop's
``loop.time()``: uvloop's ``loop.time()`` is backed by libuv's clock, which
*includes* sleep on macOS (see the same trap documented in
``omnigent/runtime/harnesses/process_manager.py``), which would zero out the
divergence and silently disable detection. :func:`time.monotonic` is
process-wide and loop-independent.

Windows note: ``time.monotonic()`` on Windows counts suspended time, so the
divergence stays ~0 there and this watcher never fires — the connection falls
back to the keepalive-timeout behavior it has today (no regression).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

_logger = logging.getLogger(__name__)

# Poll cadence. Detection latency after a wake is at most one interval: the
# in-flight sleep's deadline is on the frozen monotonic clock, so it elapses
# shortly after the machine resumes. 5 s keeps wake reconnects snappy at
# negligible cost — the host already runs a 2 s orphan-reaper and a 5 s
# harness-readiness loop, so this adds no meaningful wakeups.
SUSPEND_POLL_INTERVAL_S = 5.0

# Minimum wall-minus-monotonic divergence, in seconds, that counts as a resume.
# Comfortably above scheduler jitter and clock-read skew. Because the signal is
# the *divergence* (not raw wall time), a blocked event loop can never reach it
# no matter how long it blocks — only real suspended time diverges the clocks.
# A lid-close shorter than this won't fire, but such a short sleep rarely drops
# the socket (the keepalive budget is 90 s) so the connection stays healthy.
SUSPEND_GAP_THRESHOLD_S = 15.0


async def watch_for_resume(
    on_resume: Callable[[float], None],
    *,
    interval_s: float = SUSPEND_POLL_INTERVAL_S,
    threshold_s: float = SUSPEND_GAP_THRESHOLD_S,
    wall_clock: Callable[[], float] = time.time,
    mono_clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Call ``on_resume(gap_s)`` once each time the machine resumes from sleep.

    Loops forever polling ``interval_s`` at a time; whenever the wall clock
    advanced more than ``threshold_s`` beyond the monotonic clock across one
    poll, the machine slept and just woke, and ``on_resume`` is invoked with
    the approximate sleep duration in seconds. Cancel the task to stop.

    ``on_resume`` runs on the event loop and must be sync, non-blocking, and
    must not raise — callers use it to abort a now-dead socket and flag a
    prompt reconnect. Any exception it raises is logged and swallowed so the
    watcher survives.

    :param on_resume: Sync callback invoked once per detected resume with the
        approximate seconds spent asleep.
    :param interval_s: Poll cadence; also the worst-case detection latency
        after a wake.
    :param threshold_s: Minimum wall-minus-monotonic divergence that counts as
        a resume.
    :param wall_clock: Realtime clock reader (advances during sleep).
        Injectable for tests.
    :param mono_clock: Monotonic clock reader (freezes during sleep on
        macOS/Linux). Injectable for tests. Defaults to :func:`time.monotonic`
        — never ``loop.time()`` (see the module docstring).
    :param sleep: Awaitable sleeper; injectable so tests can drive the loop
        deterministically without patching :func:`asyncio.sleep` globally.
    :returns: Never returns normally; cancel the task to stop it.
    """
    while True:
        wall_before = wall_clock()
        mono_before = mono_clock()
        await sleep(interval_s)
        # Re-sample per iteration (not against a fixed baseline): a resume must
        # fire exactly once, on the poll that spanned the sleep. The next poll
        # sees gap ~0 again.
        gap = (wall_clock() - wall_before) - (mono_clock() - mono_before)
        if gap >= threshold_s:
            _logger.info("Resumed from suspend (~%.0fs asleep); notifying", gap)
            try:
                on_resume(gap)
            except Exception:
                _logger.exception("suspend on_resume callback failed")
