"""
Poll a condition until it holds, failing the test once a deadline passes.

Use :func:`poll_until` from synchronous tests and :func:`poll_until_async`
from coroutines, where blocking sleeps would stall the event loop.
:func:`wait_for_terminal_screen` waits on a runner terminal's rendered screen.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnigent.inner.terminal import TerminalInstance


def poll_until(
    predicate: Callable[[], object],
    *,
    timeout_s: float = 30.0,
    interval_s: float = 0.5,
) -> None:
    """
    Return once *predicate* returns a truthy value.

    :param predicate: Condition to check, e.g. ``lambda: marker.exists()``.
    :param timeout_s: Seconds to keep checking before failing.
    :param interval_s: Seconds to sleep between checks.
    :raises AssertionError: If the condition does not hold within *timeout_s*.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    if predicate():
        return
    raise AssertionError(f"condition not met within {timeout_s:g}s")


async def poll_until_async(
    predicate: Callable[[], object],
    *,
    timeout_s: float = 15.0,
    interval_s: float = 0.05,
) -> None:
    """
    Wait without blocking the event loop until *predicate* returns a truthy value.

    :param predicate: Condition to check, e.g. ``lambda: len(requests) == 2``.
    :param timeout_s: Seconds to keep checking before failing.
    :param interval_s: Seconds to sleep between checks.
    :raises AssertionError: If the condition does not hold within *timeout_s*.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval_s)
    if predicate():
        return
    raise AssertionError(f"condition not met within {timeout_s:g}s")


async def wait_for_terminal_screen(
    terminal: TerminalInstance,
    predicate: Callable[[str], bool],
    *,
    description: str,
    timeout_s: float = 30.0,
    interval_s: float = 0.1,
) -> str:
    """
    Wait until a terminal's rendered screen satisfies *predicate*.

    :param terminal: Running terminal to read, e.g. a native Claude pane.
    :param predicate: Check over the screen text, e.g. ``lambda s: "❯" in s``.
    :param description: What is awaited, named in the timeout failure.
    :param timeout_s: Seconds to keep reading before failing.
    :param interval_s: Seconds to sleep between reads.
    :returns: The first screen text that satisfied *predicate*.
    :raises AssertionError: On timeout, with the last screen for diagnosis.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        result = await terminal.read()
        screen = str(result.get("screen", result))
        if predicate(screen):
            return screen
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(interval_s)
    raise AssertionError(f"Timed out waiting for {description}; last terminal screen:\n{screen}")
