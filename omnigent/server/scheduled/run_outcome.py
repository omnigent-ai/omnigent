"""Record an automation run's outcome without racing the fire that delivered its prompt.

A fire sends the prompt and only then records its run: a legacy timer inserts
the run row, and persistent dispatch links a continued session to the run and
marks it delivered. The agent's turn starts as soon as the prompt is accepted,
so an outcome recorded in that gap would find no run (a first firing) or the
previous one (a continued session). The fire marks the session in flight for
that span, and recording waits for it to settle first.

The registry is per process. A fire handled by another server replica is not
seen; the gap it leaves is the time to write one run row.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

from omnigent.entities import ScheduledTaskRun
from omnigent.stores.scheduled_task_store import ScheduledTaskStore

# Longest an outcome waits for a fire to record the run that delivered its prompt.
PROMPT_SETTLE_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.05


class RunStillStartingError(Exception):
    """A fire for the session is still recording its run after the settle timeout."""


class PromptsInFlight:
    """Sessions whose fired prompt is sent, or about to be, but whose run is not recorded."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    @contextmanager
    def track(self, session_id: str) -> Iterator[None]:
        """Mark *session_id* in flight from before dispatch until its run is recorded."""
        self._counts[session_id] = self._counts.get(session_id, 0) + 1
        try:
            yield
        finally:
            remaining = self._counts[session_id] - 1
            if remaining:
                self._counts[session_id] = remaining
            else:
                del self._counts[session_id]

    def in_flight(self, session_id: str) -> bool:
        """Whether a fire for *session_id* has not recorded its run yet."""
        return session_id in self._counts

    async def wait_settled(self, session_id: str, timeout_s: float) -> bool:
        """Wait until no fire for *session_id* is in flight; ``False`` on timeout."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while self.in_flight(session_id):
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(_POLL_INTERVAL_S)
        return True


PROMPTS_IN_FLIGHT = PromptsInFlight()


async def record_outcome_once_delivered(
    store: ScheduledTaskStore,
    session_id: str,
    *,
    success: bool,
    emoji: str,
    text: str,
    recorded_at: int,
    timeout_s: float | None = None,
) -> ScheduledTaskRun | None:
    """Record the outcome once any fire into *session_id* has recorded its run.

    :returns: The run that took the outcome, or ``None`` when the session has
        no delivered run (see :meth:`ScheduledTaskStore.record_run_outcome`).
    :param timeout_s: Longest wait for an in-flight fire; defaults to
        :data:`PROMPT_SETTLE_TIMEOUT_S`.
    :raises RunStillStartingError: If a fire is still recording its run after
        the wait; recording then would pick the wrong run.
    :raises ValueError: If the emoji or text is invalid.
    """
    wait_s = PROMPT_SETTLE_TIMEOUT_S if timeout_s is None else timeout_s
    if not await PROMPTS_IN_FLIGHT.wait_settled(session_id, wait_s):
        raise RunStillStartingError(session_id)
    return await asyncio.to_thread(
        store.record_run_outcome,
        session_id,
        success=success,
        emoji=emoji,
        text=text,
        recorded_at=recorded_at,
    )
