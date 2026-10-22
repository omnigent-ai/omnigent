"""Best-effort machine-global cleanup owned by the host."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Awaitable, Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from omnigent.debug_logging import debug_event

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock.
    fcntl = None  # type: ignore[assignment]

_logger = logging.getLogger(__name__)

_DEFAULT_STARTUP_DELAY_S = 3.0
_DEFAULT_LOCK_RETRY_S = 0.5

MaintenanceStage = tuple[str, Callable[[], Awaitable[object]]]
_LockOutcome = Literal["acquired", "busy", "failed"]
_RunOutcome = Literal["completed", "busy", "failed"]


async def _run_sync_stage(call: Callable[[], object]) -> object:
    """Do not release the janitor lock while its worker thread still runs."""
    task = asyncio.create_task(asyncio.to_thread(call))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await task
        raise


@contextmanager
def _maintenance_lock(path: Path) -> Iterator[_LockOutcome]:
    """Yield the outcome of the non-blocking cleanup lock attempt."""
    if fcntl is None:
        yield "acquired"
        return
    fd: int | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if fd is not None:
            os.close(fd)
        yield "busy"
        return
    except OSError:
        _logger.warning("host maintenance lock acquisition failed", exc_info=True)
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        yield "failed"
        return
    try:
        yield "acquired"
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)


class HostMaintenanceJanitor:
    """Coalesce host lifecycle triggers into background cleanup passes."""

    def __init__(
        self,
        *,
        stages: Sequence[MaintenanceStage],
        lock_path: Path,
        startup_delay_s: float = _DEFAULT_STARTUP_DELAY_S,
        lock_retry_s: float = _DEFAULT_LOCK_RETRY_S,
        stage_skip_reasons: Mapping[str, Collection[str]] | None = None,
    ) -> None:
        self._stages = tuple(stages)
        self._lock_path = lock_path
        self._startup_delay_s = startup_delay_s
        self._lock_retry_s = lock_retry_s
        self._stage_skip_reasons = {
            stage_name: frozenset(reasons)
            for stage_name, reasons in (stage_skip_reasons or {}).items()
        }
        self._pending_reasons: set[str] = set()
        self._startup_task: asyncio.Task[None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._closing = False

    @classmethod
    def for_host(
        cls,
        *,
        harness_tmp_parent: Path | None = None,
    ) -> HostMaintenanceJanitor:
        """Build the machine-global cleanup stages for a host daemon."""
        from omnigent.process_logging import data_dir
        from omnigent.runtime.harnesses.paths import (
            absolute_harness_tmp_parent,
            resolve_harness_tmp_parent,
        )

        resolved_harness_tmp_parent = (
            absolute_harness_tmp_parent(harness_tmp_parent)
            if harness_tmp_parent is not None
            else resolve_harness_tmp_parent()
        )

        async def _reap_harness_processes() -> None:
            from omnigent.runtime.harnesses.process_manager import (
                sweep_orphaned_harness_processes,
            )

            await sweep_orphaned_harness_processes(tmp_parent=resolved_harness_tmp_parent)

        async def _reconcile_codex_processes() -> object:
            from omnigent.harnesses.codex_native.process_registry import (
                reconcile_codex_native_process_registry,
            )

            return await _run_sync_stage(reconcile_codex_native_process_registry)

        async def _reap_terminals() -> object:
            from omnigent.inner.terminal import reap_orphaned_terminals

            return await _run_sync_stage(reap_orphaned_terminals)

        async def _reap_native_bridge_dirs() -> object:
            from omnigent.native.native_bridge_common import reap_orphaned_native_bridge_dirs

            return await _run_sync_stage(reap_orphaned_native_bridge_dirs)

        return cls(
            stages=(
                ("harness_process_orphans", _reap_harness_processes),
                ("codex_process_registry", _reconcile_codex_processes),
                ("terminal_orphans", _reap_terminals),
                ("native_bridge_orphans", _reap_native_bridge_dirs),
            ),
            lock_path=data_dir().resolve() / "locks" / "host-maintenance.lock",
            stage_skip_reasons={"native_bridge_orphans": {"runner_superseded"}},
        )

    def start(self) -> None:
        """Schedule one delayed startup pass without waiting for it."""
        if self._closing or self._started:
            return
        self._started = True
        self._startup_task = asyncio.create_task(
            self._trigger_after_startup_delay(),
            name="host-global-maintenance-startup-delay",
        )

    def trigger(self, reason: str) -> None:
        """Request cleanup after a host-observed lifecycle event."""
        if self._closing:
            return
        self._pending_reasons.add(reason)
        self._ensure_drain_task()

    def _ensure_drain_task(self) -> None:
        """Start the drain task when pending lifecycle work has no owner."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._drain_pending(),
                name="host-global-maintenance-janitor",
            )

    async def shutdown(self) -> None:
        """Cancel delayed and active work during host shutdown."""
        self._closing = True
        startup_task = self._startup_task
        if startup_task is not None:
            if not startup_task.done():
                startup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await startup_task
            self._startup_task = None
        task = self._task
        if task is None:
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._task = None

    async def _trigger_after_startup_delay(self) -> None:
        try:
            # A prior host's runners poll parent death every 0.5s and allow 2s
            # for graceful exit. Wait past that bounded window before deciding
            # which process-manager directories are truly orphaned.
            await asyncio.sleep(self._startup_delay_s)
            self.trigger("host_startup")
        finally:
            self._startup_task = None

    async def _drain_pending(self) -> None:
        try:
            while self._pending_reasons and not self._closing:
                reasons = sorted(self._pending_reasons)
                self._pending_reasons.clear()
                outcome = await self._run_once(reasons)
                if outcome == "busy":
                    self._pending_reasons.update(reasons)
                    await asyncio.sleep(self._lock_retry_s)
        finally:
            self._task = None
            if self._pending_reasons and not self._closing:
                self._ensure_drain_task()

    async def _run_once(self, reasons: Sequence[str]) -> _RunOutcome:
        with _maintenance_lock(self._lock_path) as lock_outcome:
            if lock_outcome == "busy":
                _logger.info(
                    "host global maintenance deferred; another host owns the sweep",
                    extra=debug_event("host_maintenance_skipped", reasons=list(reasons)),
                )
                return "busy"
            if lock_outcome == "failed":
                return "failed"
            for stage_name, stage in self._stages:
                skipped_reasons = self._stage_skip_reasons.get(stage_name, frozenset())
                if skipped_reasons.intersection(reasons):
                    _logger.info(
                        "host global maintenance stage skipped: stage=%s reasons=%s",
                        stage_name,
                        list(reasons),
                        extra=debug_event(
                            "host_maintenance_stage",
                            reasons=list(reasons),
                            stage=stage_name,
                            status="skipped",
                        ),
                    )
                    continue
                started_at = time.monotonic()
                try:
                    cleaned_items = await stage()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logger.exception(
                        "host global maintenance stage failed: stage=%s",
                        stage_name,
                        extra=debug_event(
                            "host_maintenance_stage",
                            reasons=list(reasons),
                            stage=stage_name,
                            status="failed",
                            elapsed_ms=int((time.monotonic() - started_at) * 1000),
                        ),
                    )
                    continue
                _logger.info(
                    "host global maintenance stage completed: stage=%s cleaned_items=%s",
                    stage_name,
                    cleaned_items,
                    extra=debug_event(
                        "host_maintenance_stage",
                        reasons=list(reasons),
                        stage=stage_name,
                        status="completed",
                        elapsed_ms=int((time.monotonic() - started_at) * 1000),
                    ),
                )
        return "completed"
