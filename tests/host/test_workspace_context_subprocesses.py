"""Subprocess ownership and cancellation races for draft workspace shells."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import shutil
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from omnigent.host.connect import HostProcess
from omnigent.host.frames import (
    HostWorkspaceContextRequestFrame,
    HostWorkspaceContextResultFrame,
    HostWorkspaceContextStreamFrame,
)
from omnigent.host.identity import HostIdentity
from omnigent.host.workspace_contexts import LEASE_SECONDS, WorkspaceContextManager
from omnigent.util.subprocess_ownership import create_subprocess_exec

pytestmark = pytest.mark.asyncio
_HAS_TMUX = shutil.which("tmux") is not None


def _make_host_process() -> HostProcess:
    """Build a real host manager without starting its network lifecycle."""

    return HostProcess(
        identity=HostIdentity(host_id="host_workspace_subprocesses", name="test-host"),
        server_url="http://localhost:8000",
    )


async def _discard_stream(_frame: HostWorkspaceContextStreamFrame) -> None:
    return None


async def _request(
    manager: WorkspaceContextManager,
    op: str,
    *,
    context_id: str = "",
    params: dict[str, Any] | None = None,
    send: Callable[[HostWorkspaceContextStreamFrame], Awaitable[None]] = _discard_stream,
    tunnel: object | None = None,
) -> HostWorkspaceContextResultFrame:
    """Send one owner-scoped request directly through the manager boundary."""

    return await manager.handle(
        HostWorkspaceContextRequestFrame(
            request_id=f"req-{op}",
            op=op,
            user_id="alice",
            context_id=context_id,
            params=params or {},
        ),
        send=send,
        tunnel=tunnel if tunnel is not None else object(),
    )


def _ok(result: HostWorkspaceContextResultFrame) -> dict[str, Any]:
    assert result.status == "ok", result
    assert result.payload is not None
    return result.payload


async def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Yield until a synchronous lifecycle predicate becomes true."""

    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout=timeout)


async def test_host_reaper_preserves_workspace_async_child_exit_status() -> None:
    """Orphan sweeps cannot steal a scoped asyncio child's nonzero status."""

    host = _make_host_process()
    process: asyncio.subprocess.Process | None = None
    try:
        with host._workspace_contexts.subprocess_ownership.scope():
            process = await create_subprocess_exec(
                asyncio.create_subprocess_exec,
                sys.executable,
                "-c",
                "import sys, time; time.sleep(0.2); sys.exit(37)",
            )
        assert process.pid in host._workspace_contexts.subprocess_ownership.pids

        sweeps = 0
        while process.returncode is None:
            host._reap_orphans_once()  # pyright: ignore[reportPrivateUsage]
            sweeps += 1
            await asyncio.sleep(0.005)

        assert sweeps > 1
        assert await process.wait() == 37
        await _wait_until(
            lambda: process.pid not in host._workspace_contexts.subprocess_ownership.pids
        )
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        await host._workspace_contexts.shutdown()


@pytest.mark.posix_only
async def test_host_reaper_reaps_orphan_while_workspace_child_is_alive() -> None:
    """An unrelated zombie is reaped without touching a live protected child."""

    host = _make_host_process()
    protected: asyncio.subprocess.Process | None = None
    orphan_pid: int | None = None
    try:
        with host._workspace_contexts.subprocess_ownership.scope():
            protected = await create_subprocess_exec(
                asyncio.create_subprocess_exec,
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
            )
        assert protected.pid in host._workspace_contexts.subprocess_ownership.pids

        orphan_pid = os.fork()
        if orphan_pid == 0:  # pragma: no cover - the child never returns to pytest
            os._exit(0)

        reaped = 0
        for _ in range(500):
            reaped += host._reap_orphans_once()  # pyright: ignore[reportPrivateUsage]
            try:
                os.kill(orphan_pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.01)

        assert reaped >= 1
        with pytest.raises(OSError) as exc_info:
            os.waitpid(orphan_pid, os.WNOHANG)
        assert exc_info.value.errno == errno.ECHILD
        orphan_pid = None
        assert protected.returncode is None
        assert protected.pid in host._workspace_contexts.subprocess_ownership.pids
    finally:
        if orphan_pid not in (None, 0):
            with contextlib.suppress(ChildProcessError):
                os.waitpid(orphan_pid, 0)
        if protected is not None and protected.returncode is None:
            protected.terminate()
            await protected.wait()
        await host._workspace_contexts.shutdown()


async def test_reaper_cancellation_does_not_cancel_context_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling an expiry sweep cannot strand its shielded context cleanup."""

    now = [0.0]
    manager = WorkspaceContextManager(clock=lambda: now[0])
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def delayed_cleanup(_context_id: str) -> None:
        cleanup_started.set()
        await release_cleanup.wait()
        cleanup_finished.set()

    monkeypatch.setattr(manager.registry, "cleanup_conversation", delayed_cleanup)
    try:
        context_id = str(
            _ok(await _request(manager, "create", params={"workspace": str(tmp_path)}))["id"]
        )
        now[0] = LEASE_SECONDS
        reap_task = asyncio.create_task(manager.reap_expired())
        await asyncio.wait_for(cleanup_started.wait(), timeout=2)

        reap_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reap_task
        assert not cleanup_finished.is_set()
        assert context_id not in manager._contexts  # pyright: ignore[reportPrivateUsage]

        release_cleanup.set()
        await asyncio.wait_for(cleanup_finished.wait(), timeout=2)
        await _wait_until(
            lambda: not manager._cleanup_tasks  # pyright: ignore[reportPrivateUsage]
        )
    finally:
        release_cleanup.set()
        await manager.shutdown()


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
async def test_repeated_disconnect_does_not_cancel_bridge_finalizer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeated tunnel disconnect cannot interrupt an awaiting bridge finalizer."""

    bridge_started = asyncio.Event()
    finalizer_started = asyncio.Event()
    release_finalizer = asyncio.Event()
    finalizer_finished = asyncio.Event()

    async def bridge_with_slow_finalizer(*_args: object, **_kwargs: object) -> None:
        bridge_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalizer_started.set()
            await release_finalizer.wait()
            finalizer_finished.set()

    monkeypatch.setattr(
        "omnigent.host.workspace_contexts.bridge_tmux_control_to_websocket",
        bridge_with_slow_finalizer,
    )
    manager = WorkspaceContextManager()
    tunnel = object()
    sent: list[HostWorkspaceContextStreamFrame] = []

    async def send(frame: HostWorkspaceContextStreamFrame) -> None:
        sent.append(frame)

    try:
        context_id = str(
            _ok(await _request(manager, "create", params={"workspace": str(tmp_path)}))["id"]
        )
        terminal = _ok(
            await _request(
                manager,
                "create_terminal",
                context_id=context_id,
                params={"terminal": "bash", "session_key": "one"},
            )
        )
        _ok(
            await _request(
                manager,
                "attach",
                context_id=context_id,
                params={"terminal_id": terminal["id"], "channel_id": "channel"},
                send=send,
                tunnel=tunnel,
            )
        )
        await asyncio.wait_for(bridge_started.wait(), timeout=2)

        first = asyncio.create_task(manager.disconnect(tunnel))
        await asyncio.wait_for(finalizer_started.wait(), timeout=2)
        second = asyncio.create_task(manager.disconnect(tunnel))
        await asyncio.sleep(0)
        assert not first.done()
        assert not second.done()
        assert not finalizer_finished.is_set()

        release_finalizer.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=2)
        assert finalizer_finished.is_set()
        assert manager._channels == {}  # pyright: ignore[reportPrivateUsage]
        assert not manager._contexts[context_id].channels  # pyright: ignore[reportPrivateUsage]
        assert [frame.close_code for frame in sent if frame.close_code is not None] == [1000]
    finally:
        release_finalizer.set()
        await manager.shutdown()


async def test_terminal_module_imports_without_registry_import_cycle() -> None:
    """The low-level terminal can be imported before the registry package."""
    import subprocess

    subprocess.run(
        [
            sys.executable,
            "-c",
            "from omnigent.inner.terminal import TerminalInstance; "
            "assert TerminalInstance.__name__ == 'TerminalInstance'",
        ],
        check=True,
    )
