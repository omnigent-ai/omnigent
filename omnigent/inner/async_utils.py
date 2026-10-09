"""Async helpers that avoid default-executor shutdown edge cases."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import queue as sync_queue
import threading
import time
from collections.abc import Callable
from typing import Any

_logger = logging.getLogger(__name__)


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


async def run_sync_cleanup(  # type: ignore[explicit-any]
    fn: Callable[..., Any],
    /,
    *args: Any,
    component: str,
    session_id: str | None = None,
    slow_threshold_s: float = 0.25,
    **kwargs: Any,
) -> Any:
    """Run owned synchronous cleanup off-loop and finish before cancellation.

    Cleanup owns an external resource after its registry entry is removed. A
    caller cancellation must therefore wait for the worker to finish rather
    than abandon the resource. The worker receives the caller's context so
    cleanup logs and scoped state retain their session attribution.

    :param fn: Blocking cleanup callable, e.g. ``OSEnvironment.close``.
    :param args: Positional arguments forwarded to *fn*.
    :param component: Stable cleanup component name for diagnostics.
    :param session_id: Owning session id, when known.
    :param slow_threshold_s: Log a structured diagnostic at or above this
        duration.
    :param kwargs: Keyword arguments forwarded to *fn*.
    :returns: The callable's return value.
    :raises asyncio.CancelledError: After cleanup finishes when the caller was
        cancelled, including when cancellation was requested repeatedly.
    :raises BaseException: The callable's exception when cleanup was not
        cancelled.
    """
    context = contextvars.copy_context()
    started = time.monotonic()
    result_queue: sync_queue.Queue[tuple[str, Any]] = sync_queue.Queue()

    def _runner() -> None:
        try:
            result = context.run(fn, *args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - forward cleanup failures
            result_queue.put(("error", exc))
            return
        result_queue.put(("result", result))

    # Start before the first await. A loop-wide shutdown can cancel every
    # asyncio task before it gets scheduled, but it must not prevent owned
    # cleanup from ever starting.
    thread = threading.Thread(target=_runner, name=f"sync-cleanup-{component}", daemon=True)
    thread.start()
    cancelled = False
    result: Any = None
    worker_error: BaseException | None = None
    while True:
        try:
            kind, payload = result_queue.get_nowait()
        except sync_queue.Empty:
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                cancelled = True
            continue
        if kind == "error":
            worker_error = payload
        else:
            result = payload
        break

    duration_s = time.monotonic() - started
    if duration_s >= slow_threshold_s:
        attributes = {
            "component": component,
            "duration_ms": round(duration_s * 1000, 3),
            "cancelled": cancelled,
        }
        extra: dict[str, object] = {
            "event_name": "sync_cleanup_slow",
            "attributes": attributes,
        }
        if session_id is not None:
            extra["session_id"] = session_id
        _logger.warning("Synchronous cleanup completed slowly", extra=extra)

    if worker_error is not None:
        if cancelled:
            _logger.error(
                "Synchronous cleanup failed after cancellation",
                extra={
                    "event_name": "sync_cleanup_failed",
                    "attributes": {
                        "component": component,
                        "duration_ms": round(duration_s * 1000, 3),
                        "error_type": type(worker_error).__name__,
                    },
                    **({"session_id": session_id} if session_id is not None else {}),
                },
            )
            raise asyncio.CancelledError
        raise worker_error
    if cancelled:
        raise asyncio.CancelledError
    return result
