"""Keep a Mac awake while an agent turn is in flight.

macOS idle sleep freezes the host and drops its tunnels; the server then
fails the running turn as ``runner_disconnected`` even though the agent would
have finished. While a turn runs, the runner holds a ``caffeinate -i``
assertion (idle sleep only: closing the lid or a manual sleep still sleeps).
``-w <runner pid>`` ties the assertion to this process, so a crashed runner
never leaves the machine pinned awake.

Set ``OMNIGENT_KEEP_AWAKE=0`` to opt out. Other platforms are a no-op.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
from collections.abc import Callable

from omnigent.debug_logging import debug_event

_logger = logging.getLogger(__name__)

KEEP_AWAKE_ENV = "OMNIGENT_KEEP_AWAKE"
_POLL_INTERVAL_S = 5.0
_STOP_TIMEOUT_S = 2.0


def keep_awake_enabled(*, platform: str = sys.platform) -> bool:
    """Whether the runner should hold a sleep assertion during turns.

    :param platform: ``sys.platform`` value, overridable for tests,
        e.g. ``"darwin"``.
    :returns: ``True`` on macOS unless ``OMNIGENT_KEEP_AWAKE`` is ``0``,
        ``false``, ``no`` or ``off``.
    """
    if platform != "darwin":
        return False
    raw = os.environ.get(KEEP_AWAKE_ENV, "").strip().lower()
    return raw not in ("0", "false", "no", "off")


class _Caffeinate:
    """One ``caffeinate`` child process, started and stopped on demand."""

    def __init__(self, binary: str) -> None:
        self._binary = binary
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def held(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            self._binary,
            "-i",
            "-w",
            str(os.getpid()),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=_STOP_TIMEOUT_S)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        # Drop the handle only once reaped, so a stop cancelled mid-wait can retry.
        self._proc = None


async def run_keep_awake_monitor(
    turn_in_flight: Callable[[], bool],
    *,
    runner_id: str,
    poll_interval_s: float = _POLL_INTERVAL_S,
    caffeinate_binary: str | None = None,
) -> None:
    """Hold a sleep assertion whenever *turn_in_flight* reports a running turn.

    Runs until cancelled; releases the assertion on the way out.

    :param turn_in_flight: Callback returning whether an agent turn is
        running on this runner.
    :param runner_id: Runner id for log correlation, e.g.
        ``"runner_token_0123"``.
    :param poll_interval_s: How often to re-check, e.g. ``5.0``.
    :param caffeinate_binary: Path to ``caffeinate``; ``None`` looks it up
        on ``PATH``.
    :returns: None.
    """
    binary = caffeinate_binary or shutil.which("caffeinate")
    if binary is None:
        _logger.info("keep-awake disabled: caffeinate not found")
        return
    assertion = _Caffeinate(binary)
    try:
        while True:
            busy = turn_in_flight()
            if busy and not assertion.held:
                try:
                    await assertion.start()
                except OSError as exc:
                    _logger.warning("keep-awake: could not start caffeinate: %s", exc)
                    return
                _logger.info(
                    "keep-awake: holding idle-sleep assertion while a turn runs",
                    extra=debug_event("runner_keep_awake", runner_id=runner_id, state="held"),
                )
            elif not busy and assertion.held:
                await assertion.stop()
                _logger.info(
                    "keep-awake: released idle-sleep assertion",
                    extra=debug_event("runner_keep_awake", runner_id=runner_id, state="released"),
                )
            await asyncio.sleep(poll_interval_s)
    finally:
        await assertion.stop()
