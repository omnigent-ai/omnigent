"""Cancellation-safe teardown for the native Codex app-server wrapper."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from omnigent.harnesses.codex_native import app_server
from omnigent.harnesses.codex_native.app_server import CodexNativeAppServer
from omnigent.process_logging import HARNESS_STDERR_ENABLED_ENV_VAR


@pytest.mark.skipif(os.name != "posix", reason="Requires POSIX SIGTERM semantics")
async def test_cancelled_close_contains_child_and_releases_owned_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation during wait still reaps the child and clears this owner."""

    class _OwnerLock:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        (
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); time.sleep(60)"
        ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    owner_lock = _OwnerLock()
    server = CodexNativeAppServer(
        codex_path=sys.executable,
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        env={},
        config_overrides=[],
        cwd=tmp_path,
        bridge_dir=tmp_path,
        session_id="close-session",
        recent_stderr=[],
    )
    server.proc = child
    server.process_registry_tag = "codex-native-close-tag"
    server.process_owner_lock = owner_lock  # type: ignore[assignment]
    monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, "1")
    unregister_calls: list[str] = []
    terminate_started = asyncio.Event()
    real_terminate = app_server._terminate_process_tree

    def terminate(process: asyncio.subprocess.Process) -> None:
        real_terminate(process)
        terminate_started.set()

    monkeypatch.setattr(app_server, "_terminate_process_tree", terminate)
    monkeypatch.setattr(
        app_server,
        "unregister_codex_native_process",
        lambda tag: unregister_calls.append(tag),
    )
    monkeypatch.setattr(app_server, "_APP_SERVER_TERMINATE_TIMEOUT_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(app_server, "_APP_SERVER_KILL_TIMEOUT_SECONDS", 1.0, raising=False)

    server.stderr_task = asyncio.create_task(server._stderr_loop())
    try:
        assert child.stdout is not None
        assert await asyncio.wait_for(child.stdout.readline(), timeout=10.0) == b"ready\n"
        for _ in range(100):
            if server._stderr_diagnostics is not None:
                break
            await asyncio.sleep(0.01)
        diagnostics = server._stderr_diagnostics
        assert diagnostics is not None

        closing = asyncio.create_task(server.close())
        await asyncio.wait_for(terminate_started.wait(), timeout=2.0)
        assert child.returncode is None
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, timeout=5.0)

        assert child.returncode == -signal.SIGKILL
        assert await asyncio.wait_for(child.wait(), timeout=1.0) == child.returncode
        assert server.proc is None
        assert server.stderr_task is None
        assert server._stderr_diagnostics is None
        assert not diagnostics._thread.is_alive()
        assert server.process_registry_tag is None
        assert server.process_owner_lock is None
        assert owner_lock.closed
        assert unregister_calls == ["codex-native-close-tag"]
    finally:
        if server.stderr_task is not None:
            server.stderr_task.cancel()
            await asyncio.gather(server.stderr_task, return_exceptions=True)
        if child.returncode is None:
            child.kill()
            await child.wait()
        await server.close()


async def test_close_retains_registry_owner_until_process_is_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreaped child keeps its registry tag and owner lock for retry."""

    class _Process:
        pid = 12345
        returncode: int | None = None

        async def wait(self) -> None:
            await asyncio.Event().wait()

    class _OwnerLock:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    process = _Process()
    owner_lock = _OwnerLock()
    server = CodexNativeAppServer(
        codex_path=sys.executable,
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        env={},
        config_overrides=[],
        cwd=tmp_path,
        bridge_dir=tmp_path,
        session_id="retry-session",
    )
    server.proc = process  # type: ignore[assignment]
    server.process_registry_tag = "codex-native-retry-tag"
    server.process_owner_lock = owner_lock  # type: ignore[assignment]
    unregister_calls: list[str] = []
    monkeypatch.setattr(app_server, "_terminate_process_tree", lambda _process: None)
    monkeypatch.setattr(app_server, "_kill_process_tree", lambda _process: None)
    monkeypatch.setattr(
        app_server,
        "unregister_codex_native_process",
        lambda tag: unregister_calls.append(tag),
    )
    monkeypatch.setattr(app_server, "_APP_SERVER_TERMINATE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(app_server, "_APP_SERVER_KILL_TIMEOUT_SECONDS", 0.01)

    await server.close()
    assert not server._cleaned
    assert server.proc is process
    assert server.process_registry_tag == "codex-native-retry-tag"
    assert server.process_owner_lock is owner_lock
    assert not owner_lock.closed
    assert unregister_calls == []

    process.returncode = -9
    await server.close()
    assert server._cleaned
    assert server.proc is None
    assert server.process_registry_tag is None
    assert server.process_owner_lock is None
    assert owner_lock.closed
    assert unregister_calls == ["codex-native-retry-tag"]


async def test_start_retryable_after_pre_resource_validation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A validation failure before spawn does not poison the next start."""
    server = CodexNativeAppServer(
        codex_path=sys.executable,
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        env={},
        config_overrides=[],
        cwd=tmp_path,
        bridge_dir=tmp_path,
    )
    monkeypatch.setattr(
        app_server, "_codex_home_config_source_from_env", lambda: server.codex_home
    )
    with pytest.raises(ValueError, match="could not isolate"):
        await server.start()
    assert server._cleaned
    assert server.proc is None
    assert server.process_owner_lock is None

    monkeypatch.setattr(
        app_server.CodexNativeAppServer,
        "_start_impl",
        AsyncMock(return_value=None),
    )
    await server.start()
    assert not server._cleaned
    await server.close()


async def test_start_remains_blocked_when_cleanup_retains_owned_process(
    tmp_path: Path,
) -> None:
    """An incomplete cleanup keeps ownership and prevents unsafe restart."""
    server = CodexNativeAppServer(
        codex_path=sys.executable,
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        env={},
        config_overrides=[],
        cwd=tmp_path,
        bridge_dir=tmp_path,
    )
    server._cleaned = False
    server.proc = object()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="already started or still owned"):
        await server.start()
