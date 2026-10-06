"""Ordered delivery and deterministic cleanup for runner session streams."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncGenerator
from typing import TypeVar

from starlette.responses import StreamingResponse
from starlette.types import Send

_T = TypeVar("_T")


class SessionEventQueue(asyncio.Queue[_T]):
    """An unbounded FIFO with one stream reader and restorable in-flight items."""

    _queue: deque[_T]

    def __init__(self) -> None:
        super().__init__()
        self.reader_lock = asyncio.Lock()

    def put_back_front(self, item: _T) -> None:
        """Restore the same unfinished item before any newer events."""
        # No await: awakened getters run after the item has moved to the front.
        self.put_nowait(item)
        self._queue.rotate(1)
        self.task_done()


class SessionStreamResponse(StreamingResponse):
    """Close the event iterator before another response can consume its queue."""

    def __init__(self, events: AsyncGenerator[bytes, None]) -> None:
        super().__init__(
            events,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        self._events = events

    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            await self._events.aclose()
