"""Per-call execution budgets that exclude bounded human approval waits."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic


class ToolCallDeadline:
    """Track active time across the relay thread and its owning async task."""

    def __init__(self, seconds: float) -> None:
        """Start an execution budget of ``seconds``, excluding approval waits."""
        self._seconds = seconds
        self._started = monotonic()
        self._paused_total = 0.0
        self._paused_at: float | None = None
        self._pause_limit = 0.0
        self._lock = threading.Lock()

    def remaining(self) -> float:
        """Return the remaining active budget, including overdue approval time."""
        with self._lock:
            now = monotonic()
            paused = self._paused_total
            if self._paused_at is not None:
                paused += min(now - self._paused_at, self._pause_limit)
            return max(0.0, self._seconds - (now - self._started - paused))

    @contextmanager
    def pause(self, seconds: float) -> Iterator[None]:
        """Exclude at most the approval's own deadline from active elapsed time."""
        with self._lock:
            owns_pause = self._paused_at is None
            if owns_pause:
                self._paused_at = monotonic()
                self._pause_limit = max(0.0, seconds)
        try:
            yield
        finally:
            if owns_pause:
                with self._lock:
                    if self._paused_at is not None:
                        self._paused_total += min(monotonic() - self._paused_at, self._pause_limit)
                    self._paused_at = None


_current_deadline: ContextVar[tuple[ToolCallDeadline, asyncio.Task[object] | None] | None] = (
    ContextVar("tool_call_deadline", default=None)
)


@contextmanager
def bind_tool_call_deadline(deadline: ToolCallDeadline) -> Iterator[None]:
    """Bind a relay budget to the current task without affecting sibling calls."""
    token = _current_deadline.set((deadline, asyncio.current_task()))
    try:
        yield
    finally:
        _current_deadline.reset(token)


@contextmanager
def pause_tool_call_deadline(seconds: float) -> Iterator[None]:
    """Pause only the task that owns this call, for a bounded approval wait."""
    current = _current_deadline.get()
    if current is None or current[1] is not asyncio.current_task():
        yield
        return
    with current[0].pause(seconds):
        yield
