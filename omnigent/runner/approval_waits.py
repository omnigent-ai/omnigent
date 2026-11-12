"""Nonblocking approval-marker observations for synchronous runner busy checks."""

from __future__ import annotations

import asyncio
import logging
import time

from omnigent.runner.slow_io import run_slow_io

_logger = logging.getLogger(__name__)
_POLL_INTERVAL_S = 1.0
_MAX_OBSERVATION_AGE_S = 2.0


class NativeApprovalWaits:
    """Keep approval storage off heartbeat, idle and parent-death callbacks.

    Pending, missing and expired observations count as busy. Only the runner
    event loop starts or retires probes; background callers read snapshots.
    """

    def __init__(self) -> None:
        self._probes: dict[str, tuple[object, asyncio.Task[None]]] = {}
        self._observations: dict[str, tuple[object, float, bool]] = {}
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None

    def is_busy(self, session_id: str, instance: object) -> bool:
        """Return fresh approval evidence for this pane, or busy while unknown."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and self._loop is None:
            self._loop = loop
        if loop is not None and loop is self._loop and not self._closed:
            probe = self._probes.get(session_id)
            if probe is None or probe[0] is not instance:
                self._forget(session_id)
                self._probes[session_id] = (
                    instance,
                    loop.create_task(self._poll(session_id, instance)),
                )
        observed = self._observations.get(session_id)
        if observed is None or observed[0] is not instance:
            return True
        return observed[2] or time.monotonic() - observed[1] >= _MAX_OBSERVATION_AGE_S

    async def _poll(self, session_id: str, instance: object) -> None:
        from omnigent.harnesses.claude_native.bridge import approval_wait_is_fresh

        while True:
            self._observations.pop(session_id, None)
            try:
                busy = await run_slow_io(approval_wait_is_fresh, session_id)
            except Exception:
                busy = True
                _logger.exception("Native approval observation failed for %s", session_id)
            self._observations[session_id] = (instance, time.monotonic(), busy)
            await asyncio.sleep(_POLL_INTERVAL_S)

    def retain(self, session_ids: set[str]) -> None:
        """Retire observations for removed panes when called on the runner loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if loop is not self._loop:
            return
        for session_id in self._probes.keys() - session_ids:
            self._forget(session_id)

    def _forget(self, session_id: str) -> None:
        probe = self._probes.pop(session_id, None)
        if probe is not None:
            probe[1].cancel()
        self._observations.pop(session_id, None)

    async def close(self) -> None:
        """Stop probing without waiting for blocked filesystem workers."""
        self._closed = True
        tasks = [probe[1] for probe in self._probes.values()]
        for session_id in list(self._probes):
            self._forget(session_id)
        await asyncio.gather(*tasks, return_exceptions=True)
