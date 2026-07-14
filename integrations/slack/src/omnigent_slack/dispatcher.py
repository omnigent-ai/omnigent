from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from omnigent_slack.models import SlackTurn, ThreadKey

TurnWorker = Callable[[SlackTurn], Awaitable[None]]


class ThreadTurnDispatcher:
    def __init__(self, worker: TurnWorker, idle_timeout_seconds: float = 60.0) -> None:
        self._worker = worker
        self._idle_timeout_seconds = idle_timeout_seconds
        self._queues: dict[ThreadKey, asyncio.Queue[SlackTurn]] = {}
        self._tasks: dict[ThreadKey, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._logger = logging.getLogger(__name__)

    async def enqueue(self, turn: SlackTurn) -> None:
        async with self._lock:
            queue = self._queues.get(turn.key)
            if queue is None:
                queue = asyncio.Queue()
                self._queues[turn.key] = queue
                self._tasks[turn.key] = asyncio.create_task(self._run_queue(turn.key, queue))
                self._logger.debug("Created turn queue for %s", turn.key.display())
            await queue.put(turn)
            self._logger.info(
                "Queued Slack turn thread=%s queue_size=%s create_if_missing=%s",
                turn.key.display(),
                queue.qsize(),
                turn.create_if_missing,
            )

    async def shutdown(self) -> None:
        async with self._lock:
            tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_queue(self, key: ThreadKey, queue: asyncio.Queue[SlackTurn]) -> None:
        try:
            while True:
                try:
                    turn = await asyncio.wait_for(queue.get(), timeout=self._idle_timeout_seconds)
                except TimeoutError:
                    self._logger.debug("Closing idle turn queue for %s", key.display())
                    return
                try:
                    self._logger.info("Running queued Slack turn thread=%s", key.display())
                    await self._worker(turn)
                except Exception:
                    self._logger.exception("Slack turn failed for %s", key.display())
                finally:
                    queue.task_done()
        finally:
            async with self._lock:
                if self._queues.get(key) is queue:
                    if queue.empty():
                        self._queues.pop(key, None)
                        self._tasks.pop(key, None)
                    else:
                        # A turn slipped in after the idle timeout fired but
                        # before this teardown reacquired the lock. The queue
                        # stays registered, so no future enqueue would spawn a
                        # worker — re-arm one here to keep draining it.
                        self._tasks[key] = asyncio.create_task(self._run_queue(key, queue))
