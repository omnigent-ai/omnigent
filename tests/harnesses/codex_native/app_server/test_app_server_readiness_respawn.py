"""Readiness respawn tests for Codex app server.

Tests the respawn-on-silent-startup behavior: when the app-server process
never starts listening (times out with process alive), _wait_until_ready
raises CodexAppServerSilentStartTimeout, and start() catches it to respawn
once on a fresh port.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerSilentStartTimeout,
)
from tests.harnesses.codex_native.app_server._support import _test_app_server

# Patch timeout for fast tests
_FAST_READY_TIMEOUT = 0.01


async def test_readiness_raises_silent_startup_timeout_when_process_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Timeout with alive process raises CodexAppServerSilentStartTimeout."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    server = _test_app_server(
        tmp_path,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )

    # Patch timeout to be tiny for fast test
    monkeypatch.setattr(
        codex_native_app_server,
        "_APP_SERVER_READY_TIMEOUT_SECONDS",
        _FAST_READY_TIMEOUT,
    )

    # Mock process that stays alive but never connects
    server.proc = AsyncMock()
    server.proc.returncode = None  # Alive
    server.proc.pid = 1234
    server.recent_stderr = ["sample", "stderr", "lines"]

    with pytest.raises(CodexAppServerSilentStartTimeout) as excinfo:
        await server._wait_until_ready()

    # Verify exception contains stderr bytes info
    assert excinfo.value.stderr_bytes > 0


async def test_readiness_raises_runtime_error_when_process_exits_early(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Timeout with exited process raises RuntimeError, not silent startup exception."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    server = _test_app_server(
        tmp_path,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )

    monkeypatch.setattr(
        codex_native_app_server,
        "_APP_SERVER_READY_TIMEOUT_SECONDS",
        _FAST_READY_TIMEOUT,
    )

    # Mock process that exits
    server.proc = AsyncMock()
    server.proc.returncode = 1  # Exited
    server.recent_stderr = ["Exit error"]

    # Should raise generic RuntimeError, not CodexAppServerSilentStartTimeout
    with pytest.raises(RuntimeError, match="exited early"):
        await server._wait_until_ready()


async def test_readiness_distinguishes_process_exits_from_silent_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout tells a live, silent process (respawnable) from one that exited."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    server = _test_app_server(
        tmp_path,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )

    monkeypatch.setattr(
        codex_native_app_server,
        "_APP_SERVER_READY_TIMEOUT_SECONDS",
        _FAST_READY_TIMEOUT,
    )

    # Case 1: Process alive -> CodexAppServerSilentStartTimeout
    server.proc = AsyncMock()
    server.proc.returncode = None
    server.recent_stderr = []

    exc_type_1 = None
    try:
        await server._wait_until_ready()
    except Exception as e:
        exc_type_1 = type(e)

    assert exc_type_1 is CodexAppServerSilentStartTimeout, (
        f"Expected CodexAppServerSilentStartTimeout, got {exc_type_1}"
    )

    # Case 2: Process exited -> RuntimeError
    server.proc = AsyncMock()
    server.proc.returncode = 1
    server.recent_stderr = []

    exc_type_2 = None
    try:
        await server._wait_until_ready()
    except Exception as e:
        exc_type_2 = type(e)

    assert exc_type_2 is RuntimeError and exc_type_2 is not CodexAppServerSilentStartTimeout
