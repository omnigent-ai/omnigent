"""Async helpers that avoid default-executor shutdown edge cases."""

from __future__ import annotations

import asyncio
import queue as sync_queue
import threading
from collections.abc import Callable
from typing import Any


async def run_sync_on_thread(  # type: ignore[explicit-any]
    fn: Callable[..., Any],
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run blocking work on a dedicated thread without using asyncio's executor.

    This keeps short-lived event loops from hanging during default-executor
    shutdown, which can happen in tests that repeatedly create and close loops.

    Signatures and return types vary across call sites (sync SDK methods,
    arbitrary tool bodies, file I/O); the boundary stays open and each caller
    narrows the result itself.
    """
    result_queue: sync_queue.Queue[  # type: ignore[explicit-any]
        tuple[str, Any]
    ] = sync_queue.Queue()

    def _runner() -> None:
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 — thread worker forwards all exceptions (incl. KeyboardInterrupt/SystemExit) to the caller thread
            result_queue.put(("error", exc))
            return
        result_queue.put(("result", result))

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    try:
        while True:
            try:
                kind, payload = result_queue.get_nowait()
            except sync_queue.Empty:
                await asyncio.sleep(0.001)
                continue
            if kind == "error":
                raise payload
            return payload
    finally:
        thread.join(timeout=0)
