"""Report what occupies a thread pool while its queued jobs wait for a worker.

A full pool delays every ``asyncio.to_thread`` caller even though the event
loop stays responsive, so the event-loop watchdog never fires. This watchdog
submits a no-op probe to the pool; when the probe waits past a threshold, it
logs where each worker is, from frame locations only, never locals.
"""

from __future__ import annotations

import logging
import math
import os
import sys
import sysconfig
import threading
from collections import Counter
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path
from time import monotonic, sleep
from types import FrameType
from typing import Literal

import omnigent
from omnigent.debug_logging import CAPTURED_STACK_TRACE_FIELD, debug_event
from omnigent.util.event_loop_watchdog import _capture_stack

_logger = logging.getLogger(__name__)
_MAX_REPORT_STACK_BYTES = 32768
_MAX_SUMMARY_LOCATIONS = 5
# Parent of the omnigent package, so locations read "omnigent/x.py:12 in fn".
_PACKAGE_PARENT = str(Path(omnigent.__file__).resolve().parent.parent) + os.sep
_PACKAGE_ROOT = _PACKAGE_PARENT + "omnigent" + os.sep
_STDLIB_ROOT = str(Path(sysconfig.get_paths()["stdlib"]).resolve()) + os.sep


def _probe() -> None:
    """No-op job; it starts only once a worker is free."""


def _location(frame: FrameType) -> str:
    """Format a frame as ``"omnigent/x.py:12 in fn"``, or by basename outside the package."""
    filename = frame.f_code.co_filename
    if filename.startswith(_PACKAGE_PARENT):
        filename = filename[len(_PACKAGE_PARENT) :]
    else:
        filename = os.path.basename(filename)
    return f"{filename}:{frame.f_lineno} in {frame.f_code.co_name}"


def _worker_location(frame: FrameType | None) -> str:
    """Name the worker's innermost Omnigent frame, else innermost non-stdlib frame."""
    first_frame: FrameType | None = None
    first_outside_stdlib: FrameType | None = None
    try:
        while frame is not None:
            filename = frame.f_code.co_filename
            if first_frame is None:
                first_frame = frame
            if filename.startswith(_PACKAGE_ROOT):
                return _location(frame)
            if first_outside_stdlib is None and not filename.startswith(_STDLIB_ROOT):
                first_outside_stdlib = frame
            frame = frame.f_back
        chosen = first_outside_stdlib or first_frame
        return _location(chosen) if chosen is not None else "unknown"
    finally:
        del frame, first_frame, first_outside_stdlib


def _worker_stacks(workers: list[threading.Thread]) -> str:
    """Capture each worker's stack, keeping the whole report within a byte budget."""
    sections: list[str] = []
    remaining_bytes = _MAX_REPORT_STACK_BYTES
    for index, worker in enumerate(workers):
        section = f"Worker {worker.name}\n{_capture_stack(worker.ident or 0)}"
        section_bytes = len(section.encode("utf-8", errors="replace"))
        omitted = f"... {len(workers) - index} more workers omitted ...\n"
        # Each accepted section leaves room for a later, no longer, marker.
        if section_bytes + len(omitted) > remaining_bytes:
            sections.append(omitted)
            break
        sections.append(section)
        remaining_bytes -= section_bytes
    return "".join(sections)


def watch_thread_pool(
    executor: ThreadPoolExecutor,
    *,
    thread_name_prefix: str,
    process_role: Literal["server", "runner"] = "runner",
    stall_threshold_s: float = 10.0,
    probe_interval_s: float = 1.0,
    report_interval_s: float = 60.0,
) -> threading.Thread:
    """Start a daemon thread that reports when *executor* backs up.

    The thread exits once the executor shuts down and rejects its next probe.

    :param executor: The pool to watch, e.g. the runner's default executor.
    :param thread_name_prefix: The pool's worker name prefix, e.g. ``"runner"``.
    :param process_role: Role named in the report, e.g. ``"runner"``.
    :param stall_threshold_s: Probe wait that triggers a report, e.g. ``10.0``.
    :param probe_interval_s: Pause between probes, e.g. ``1.0``.
    :param report_interval_s: Minimum seconds between reports, e.g. ``60.0``.
    :returns: The started watchdog thread.
    """
    for value in (stall_threshold_s, probe_interval_s, report_interval_s):
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Thread pool watchdog intervals must be finite and positive")

    def report(waited_s: float) -> None:
        workers = [
            thread
            for thread in threading.enumerate()
            if thread.name.startswith(f"{thread_name_prefix}_")
        ]
        frames = sys._current_frames()
        try:
            locations = Counter(
                _worker_location(frames.get(worker.ident or 0)) for worker in workers
            )
        finally:
            del frames
        summary = ", ".join(
            f"{location} x{count}"
            for location, count in locations.most_common(_MAX_SUMMARY_LOCATIONS)
        )
        _logger.warning(
            "%s thread pool backed up: a queued job has waited %.1fs for one of %d workers; "
            "workers are in: %s",
            process_role.capitalize(),
            waited_s,
            len(workers),
            summary or "none",
            extra={
                **debug_event(
                    f"{process_role}_thread_pool_backed_up",
                    pid=os.getpid(),
                    waited_s=round(waited_s, 3),
                    threshold_s=stall_threshold_s,
                    workers=len(workers),
                    worker_locations=summary,
                    report_interval_s=report_interval_s,
                ),
                CAPTURED_STACK_TRACE_FIELD: _worker_stacks(workers),
            },
        )

    def watch() -> None:
        last_report = -math.inf
        while True:
            submitted_at = monotonic()
            try:
                probe = executor.submit(_probe)
            except RuntimeError:
                return  # The executor has shut down.
            while True:
                try:
                    probe.result(timeout=stall_threshold_s)
                    break
                except CancelledError:
                    return  # Shut down with cancel_futures.
                except TimeoutError:
                    now = monotonic()
                    if now - last_report < report_interval_s:
                        continue
                    last_report = now
                    try:
                        report(now - submitted_at)
                    except Exception:
                        _logger.exception(
                            "Could not report the backed-up %s thread pool", process_role
                        )
            sleep(probe_interval_s)

    thread = threading.Thread(
        target=watch, name=f"omnigent-{process_role}-pool-watchdog", daemon=True
    )
    thread.start()
    return thread
