"""Capture the serving event loop's stack while it is unresponsive.

Tunnel diagnostics measure scheduling delay after a coroutine resumes. This
heartbeat instead publishes progress under a lock for sampling by another thread
while the loop is blocked, independently of any connection's lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
import math
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from time import monotonic

from omnigent.debug_logging import CAPTURED_STACK_TRACE_FIELD, debug_event

_logger = logging.getLogger(__name__)
_MAX_STACK_FRAMES = 64
_MAX_STACK_BYTES = 8192


def _capture_stack(thread_id: int) -> str:
    """Return bounded frame locations, without reading source lines or locals."""
    header = "Stack (innermost frame first):\n"
    omission_marker = "  ... older frames omitted ...\n"
    remaining_bytes = _MAX_STACK_BYTES - len(header) - len(omission_marker)
    frame = sys._current_frames().get(thread_id)
    lines: list[str] = []
    try:
        while frame is not None and len(lines) < _MAX_STACK_FRAMES:
            line = (
                f'  File "{frame.f_code.co_filename}", line {frame.f_lineno}, '
                f"in {frame.f_code.co_name}\n"
            )
            line_bytes = len(line.encode("utf-8", errors="replace"))
            if line_bytes > remaining_bytes:
                break
            lines.append(line)
            remaining_bytes -= line_bytes
            frame = frame.f_back
        if frame is not None:
            lines.append(omission_marker)
    finally:
        del frame
    return header + "".join(lines)


@contextmanager
def monitor_event_loop(
    *,
    stall_threshold_s: float = 5.0,
    sample_interval_s: float = 1.0,
    report_interval_s: float = 60.0,
) -> Iterator[None]:
    """Watch the current loop from a dedicated thread until the context exits.

    A timer acknowledges progress; the watchdog needs neither the loop nor its
    executor to capture a stack. Reports are rate limited across all stalls in
    this context. A native extension holding the GIL can also delay this thread.
    """
    for value in (stall_threshold_s, sample_interval_s, report_interval_s):
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Watchdog intervals must be finite and positive")
    if sample_interval_s >= stall_threshold_s:
        raise ValueError("Watchdog sample interval must be shorter than the stall threshold")

    loop = asyncio.get_running_loop()
    loop_thread_id = threading.get_ident()
    stopped = threading.Event()
    lock = threading.Lock()
    last_heartbeat = monotonic()
    heartbeat_handle: asyncio.TimerHandle | None = None

    def heartbeat() -> None:
        nonlocal last_heartbeat, heartbeat_handle
        with lock:
            last_heartbeat = monotonic()
        heartbeat_handle = loop.call_later(sample_interval_s, heartbeat)

    def watch() -> None:
        last_report = -math.inf
        while not stopped.wait(sample_interval_s):
            now = monotonic()
            with lock:
                observed_heartbeat = last_heartbeat
            stalled_s = now - observed_heartbeat
            if stalled_s < stall_threshold_s or now - last_report < report_interval_s:
                continue
            try:
                stack = _capture_stack(loop_thread_id)
                with lock:
                    recovered = last_heartbeat != observed_heartbeat
                if recovered or stopped.is_set():
                    continue
                last_report = monotonic()
                _logger.warning(
                    "Server event loop has not advanced for %.3fs",
                    stalled_s,
                    extra={
                        **debug_event(
                            "server_event_loop_stalled",
                            stalled_s=round(stalled_s, 3),
                            threshold_s=stall_threshold_s,
                            loop_thread_id=loop_thread_id,
                            report_interval_s=report_interval_s,
                        ),
                        CAPTURED_STACK_TRACE_FIELD: stack,
                    },
                )
            except Exception:
                last_report = monotonic()
                _logger.exception("Could not capture the stalled server event loop")

    thread = threading.Thread(target=watch, name="omnigent-event-loop-watchdog", daemon=True)
    heartbeat()
    try:
        try:
            thread.start()
        except RuntimeError:
            stopped.set()
            if heartbeat_handle is not None:
                heartbeat_handle.cancel()
            _logger.exception(
                "Could not start the event-loop watchdog; serving without stack capture",
                extra=debug_event("server_event_loop_watchdog_failed"),
            )
        yield
    finally:
        stopped.set()
        if heartbeat_handle is not None:
            heartbeat_handle.cancel()
        # Don't wait for a watchdog stuck in its own log call.
        if thread.ident is not None:
            thread.join(timeout=0.1)
