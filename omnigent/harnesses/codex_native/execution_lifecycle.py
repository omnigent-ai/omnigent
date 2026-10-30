"""Turn-scoped ownership of an externally managed Codex exec server."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Protocol

from omnigent.harnesses.codex_native.execution_environment import ManagedExecServerConfig
from omnigent.inner import _proc

_logger = logging.getLogger(__name__)
_PROBE_ATTEMPT_SECONDS = 15.0
_RETRY_SECONDS = 0.1
_STDERR_TAIL_BYTES = 8 * 1024

ReadinessProbe = Callable[[], Awaitable[None]]


class ManagedExecServerLifecycle(Protocol):
    """Lifecycle contract used by the app-server proxy."""

    async def acquire(self, probe: ReadinessProbe) -> str:
        """Acquire one active-turn lease after readiness is proven."""
        ...

    async def release(self, lease: str) -> None:
        """Release one active-turn lease."""
        ...

    async def close(self) -> None:
        """Release all leases and stop owned resources."""
        ...


class ManagedExecServerError(RuntimeError):
    """The configured exec-server command could not provide a usable environment."""


class ManagedExecServerController:
    """Share one managed command across active turns and stop it when all finish."""

    def __init__(
        self,
        config: ManagedExecServerConfig,
        *,
        termination_grace_seconds: float = 5.0,
    ) -> None:
        self._config = config
        self._termination_grace_seconds = termination_grace_seconds
        self._lock = asyncio.Lock()
        self._leases: set[str] = set()
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail = bytearray()

    @property
    def process_pid(self) -> int | None:
        """Return the live managed-command PID, or ``None`` while idle."""
        process = self._process
        return process.pid if process is not None and process.returncode is None else None

    async def acquire(self, probe: ReadinessProbe) -> str:
        """Start or reuse the command, prove readiness, and return one lease token."""
        async with self._lock:
            if self._process is None or self._process.returncode is not None:
                self._leases.clear()
                await self._stop_locked()
                await self._start_locked()
            try:
                await self._wait_until_ready_locked(probe)
            except BaseException:
                await self._stop_locked()
                raise
            lease = uuid.uuid4().hex
            self._leases.add(lease)
            return lease

    async def release(self, lease: str) -> None:
        """Release one active turn and stop the command after the final lease."""
        async with self._lock:
            if lease not in self._leases:
                return
            self._leases.remove(lease)
            if not self._leases:
                await self._stop_locked()

    async def close(self) -> None:
        """Drop all leases and tear down the owned process tree."""
        async with self._lock:
            self._leases.clear()
            await self._stop_locked()

    async def _start_locked(self) -> None:
        self._stderr_tail.clear()
        try:
            process = await asyncio.create_subprocess_exec(
                *self._config.command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                **_proc.spawn_kwargs(),
            )
        except OSError as exc:
            raise ManagedExecServerError(
                f"could not start managed exec-server command: {exc}"
            ) from exc
        self._process = process
        _proc.remember_process_group(process)
        assert process.stderr is not None
        self._stderr_task = asyncio.create_task(
            self._capture_stderr(process.stderr),
            name="codex-managed-exec-server-stderr",
        )

    async def _wait_until_ready_locked(self, probe: ReadinessProbe) -> None:
        loop = asyncio.get_running_loop()
        timeout = self._config.ready_timeout_seconds
        deadline = loop.time() + timeout
        last_error: Exception | None = None
        while True:
            process = self._process
            assert process is not None
            if process.returncode is not None:
                await self._finish_stderr_task()
                detail = self._stderr_detail()
                raise ManagedExecServerError(
                    f"managed exec-server command exited with status {process.returncode}{detail}"
                )
            remaining = deadline - loop.time()
            if remaining <= 0:
                detail = f": {last_error}" if last_error is not None else self._stderr_detail()
                raise ManagedExecServerError(
                    f"managed exec-server was not ready after {timeout:g} seconds{detail}"
                )
            try:
                await asyncio.wait_for(
                    probe(),
                    timeout=min(remaining, _PROBE_ATTEMPT_SECONDS),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - retry readiness failures to deadline
                last_error = exc
            else:
                return
            await asyncio.sleep(min(_RETRY_SECONDS, max(0.0, deadline - loop.time())))

    async def _capture_stderr(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(4096):
            self._stderr_tail.extend(chunk)
            if len(self._stderr_tail) > _STDERR_TAIL_BYTES:
                del self._stderr_tail[:-_STDERR_TAIL_BYTES]

    def _stderr_detail(self) -> str:
        text = self._stderr_tail.decode("utf-8", errors="replace").strip()
        return f": {text}" if text else ""

    async def _finish_stderr_task(self) -> None:
        task = self._stderr_task
        self._stderr_task = None
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=0.5)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _stop_locked(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            _proc.terminate_tree(process)
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=self._termination_grace_seconds,
                )
            except TimeoutError:
                _logger.warning("Managed exec-server command ignored graceful termination")
                _proc.kill_tree(process)
                await process.wait()
        elif process is not None:
            await process.wait()
        await self._finish_stderr_task()
