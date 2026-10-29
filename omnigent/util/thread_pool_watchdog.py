"""Report what occupies a thread pool while its jobs wait for a free worker.

A full pool delays every ``asyncio.to_thread`` caller even though the event
loop stays responsive, so the event-loop watchdog never fires. This executor
records which job each worker runs and how long queued jobs have waited, and a
watchdog thread logs that picture, with each worker's stack, once the oldest
queued job has waited past a threshold. Only code locations are recorded,
never job arguments.
"""

from __future__ import annotations

import contextvars
import functools
import itertools
import logging
import math
import os
import threading
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from time import monotonic
from typing import Literal, ParamSpec, TypeVar

from omnigent.debug_logging import CAPTURED_STACK_TRACE_FIELD, debug_event
from omnigent.util.event_loop_watchdog import _capture_stack

_logger = logging.getLogger(__name__)
_MAX_SUMMARY_LABELS = 5
_MAX_REPORT_STACK_BYTES = 32768

_P = ParamSpec("_P")
_T = TypeVar("_T")


@dataclass(frozen=True)
class PoolJob:
    """One submitted job: where its code lives and when it entered its state."""

    label: str
    since: float


@dataclass(frozen=True)
class ThreadPoolSnapshot:
    """Point-in-time view of a pool's queued and running jobs.

    :param max_workers: Worker limit, e.g. ``8``.
    :param queued: Jobs waiting for a worker; ``since`` is the submit time.
    :param running: Running jobs keyed by worker thread id; ``since`` is the start time.
    """

    max_workers: int
    queued: tuple[PoolJob, ...]
    running: dict[int, PoolJob]


def _job_label(fn: Callable[..., object]) -> str:
    """Name the code a job runs, e.g. ``"omnigent.runtime.fs:Registry.list_changed_files"``.

    Unwraps the ``functools.partial(context.run, func, ...)`` that
    ``asyncio.to_thread`` submits so the label names *func*, not ``Context.run``.
    """
    while isinstance(fn, functools.partial):
        inner = fn.func
        if isinstance(getattr(inner, "__self__", None), contextvars.Context) and fn.args:
            fn = fn.args[0]
        else:
            fn = inner
    target = getattr(fn, "__func__", fn)
    qualname = getattr(target, "__qualname__", None) or type(target).__qualname__
    module = getattr(target, "__module__", None)
    return f"{module}:{qualname}" if module else qualname


def _summarize(jobs: Iterable[PoolJob], now: float) -> str:
    """Count jobs per label with the longest age, e.g. ``"a:f x6 (max 95.2s)"``."""
    counts: Counter[str] = Counter()
    longest: dict[str, float] = {}
    for job in jobs:
        counts[job.label] += 1
        longest[job.label] = max(longest.get(job.label, 0.0), now - job.since)
    parts = [
        f"{label} x{count} (max {longest[label]:.1f}s)"
        for label, count in counts.most_common(_MAX_SUMMARY_LABELS)
    ]
    if len(counts) > _MAX_SUMMARY_LABELS:
        parts.append(f"+{len(counts) - _MAX_SUMMARY_LABELS} more")
    return ", ".join(parts) or "none"


class MonitoredThreadPoolExecutor(ThreadPoolExecutor):
    """``ThreadPoolExecutor`` that logs its occupants when jobs wait too long.

    :param max_workers: Worker limit, e.g. ``8``.
    :param thread_name_prefix: Worker thread name prefix, e.g. ``"runner"``.
    :param process_role: Role named in the report, e.g. ``"runner"``.
    :param stall_threshold_s: Queue wait that triggers a report, e.g. ``10.0``.
    :param sample_interval_s: Seconds between checks, e.g. ``1.0``.
    :param report_interval_s: Minimum seconds between reports, e.g. ``60.0``.
    """

    def __init__(
        self,
        max_workers: int,
        *,
        thread_name_prefix: str = "",
        process_role: Literal["server", "runner"] = "runner",
        stall_threshold_s: float = 10.0,
        sample_interval_s: float = 1.0,
        report_interval_s: float = 60.0,
    ) -> None:
        for value in (stall_threshold_s, sample_interval_s, report_interval_s):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Thread pool watchdog intervals must be finite and positive")
        if sample_interval_s >= stall_threshold_s:
            raise ValueError(
                "Thread pool watchdog sample interval must be shorter than the stall threshold"
            )
        super().__init__(max_workers=max_workers, thread_name_prefix=thread_name_prefix)
        self._monitor_max_workers = max_workers
        self._process_role = process_role
        self._stall_threshold_s = stall_threshold_s
        self._sample_interval_s = sample_interval_s
        self._report_interval_s = report_interval_s
        self._jobs_lock = threading.Lock()
        self._job_ids = itertools.count()
        self._queued_jobs: dict[int, PoolJob] = {}
        self._running_jobs: dict[int, PoolJob] = {}
        self._watch_stopped = threading.Event()
        self._watch_thread: threading.Thread | None = None

    def submit(self, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> Future[_T]:
        """Submit *fn* like ``ThreadPoolExecutor.submit``, recording its queue time."""
        job_id = next(self._job_ids)
        job = PoolJob(label=_job_label(fn), since=monotonic())

        def run() -> _T:
            worker_id = threading.get_ident()
            with self._jobs_lock:
                self._queued_jobs.pop(job_id, None)
                self._running_jobs[worker_id] = PoolJob(label=job.label, since=monotonic())
            try:
                return fn(*args, **kwargs)
            finally:
                with self._jobs_lock:
                    self._running_jobs.pop(worker_id, None)

        with self._jobs_lock:
            self._queued_jobs[job_id] = job
        try:
            future = super().submit(run)
        except BaseException:
            with self._jobs_lock:
                self._queued_jobs.pop(job_id, None)
            raise
        # A job cancelled while queued never runs; drop it from the queue view.
        future.add_done_callback(lambda _future: self._forget_queued(job_id))
        self._ensure_watching()
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        """Stop the watchdog, then shut the pool down like ``ThreadPoolExecutor``."""
        self._watch_stopped.set()
        super().shutdown(wait=wait, cancel_futures=cancel_futures)

    def snapshot(self) -> ThreadPoolSnapshot:
        """Return the current queued and running jobs."""
        with self._jobs_lock:
            return ThreadPoolSnapshot(
                max_workers=self._monitor_max_workers,
                queued=tuple(self._queued_jobs.values()),
                running=dict(self._running_jobs),
            )

    def _forget_queued(self, job_id: int) -> None:
        with self._jobs_lock:
            self._queued_jobs.pop(job_id, None)

    def _ensure_watching(self) -> None:
        """Start the watchdog thread on first use, so an idle pool costs no thread."""
        if self._watch_thread is not None or self._watch_stopped.is_set():
            return
        with self._jobs_lock:
            if self._watch_thread is not None:
                return
            thread = threading.Thread(
                target=self._watch,
                name=f"omnigent-{self._process_role}-pool-watchdog",
                daemon=True,
            )
            self._watch_thread = thread
        try:
            thread.start()
        except RuntimeError:
            _logger.exception(
                "Could not start the thread pool watchdog; running without pool reports",
                extra=debug_event(
                    f"{self._process_role}_thread_pool_watchdog_failed", pid=os.getpid()
                ),
            )

    def _watch(self) -> None:
        last_report = -math.inf
        while not self._watch_stopped.wait(self._sample_interval_s):
            snapshot = self.snapshot()
            if not snapshot.queued:
                continue
            now = monotonic()
            oldest_wait_s = now - min(job.since for job in snapshot.queued)
            if (
                oldest_wait_s < self._stall_threshold_s
                or now - last_report < self._report_interval_s
            ):
                continue
            last_report = now
            try:
                self._report(snapshot, oldest_wait_s, now)
            except Exception:
                _logger.exception(
                    "Could not report the backed-up %s thread pool", self._process_role
                )

    def _report(self, snapshot: ThreadPoolSnapshot, oldest_wait_s: float, now: float) -> None:
        running_summary = _summarize(snapshot.running.values(), now)
        queued_summary = _summarize(snapshot.queued, now)
        _logger.warning(
            "%s thread pool backed up: %d/%d workers busy, %d queued, oldest waited %.1fs; "
            "running: %s; queued: %s",
            self._process_role.capitalize(),
            len(snapshot.running),
            snapshot.max_workers,
            len(snapshot.queued),
            oldest_wait_s,
            running_summary,
            queued_summary,
            extra={
                **debug_event(
                    f"{self._process_role}_thread_pool_backed_up",
                    pid=os.getpid(),
                    max_workers=snapshot.max_workers,
                    busy_workers=len(snapshot.running),
                    queued_jobs=len(snapshot.queued),
                    oldest_queued_s=round(oldest_wait_s, 3),
                    threshold_s=self._stall_threshold_s,
                    report_interval_s=self._report_interval_s,
                    running=running_summary,
                    queued=queued_summary,
                ),
                CAPTURED_STACK_TRACE_FIELD: _worker_stacks(snapshot.running, now),
            },
        )


def _worker_stacks(running: dict[int, PoolJob], now: float) -> str:
    """Capture each busy worker's stack, longest-running first, within a byte budget."""
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    sections: list[str] = []
    remaining_bytes = _MAX_REPORT_STACK_BYTES
    ordered = sorted(running.items(), key=lambda item: item[1].since)
    for index, (worker_id, job) in enumerate(ordered):
        section = (
            f"Worker {names.get(worker_id, worker_id)} running {job.label} "
            f"for {now - job.since:.1f}s\n{_capture_stack(worker_id)}"
        )
        section_bytes = len(section.encode("utf-8", errors="replace"))
        if section_bytes > remaining_bytes:
            sections.append(f"... {len(ordered) - index} more workers omitted ...\n")
            break
        sections.append(section)
        remaining_bytes -= section_bytes
    return "".join(sections)
