"""Codex discovery failures retain diagnostics for the launch that failed."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omnigent.debug_logging import PRIMARY_SESSION_ID_ENV_VAR, record_to_row
from omnigent.harnesses.codex_native import forwarder
from omnigent.harnesses.codex_native.app_server import CodexNativeAppServer
from omnigent.harnesses.codex_native.bridge import (
    read_bridge_startup_error,
    read_bridge_state,
    write_bridge_startup_error,
)
from omnigent.inner.terminal import TerminalInstance
from omnigent.process_logging import RedactingLogFormatter
from omnigent.runner.native import orchestration

_STDERR_ENV = "OMNIGENT_HARNESS_STDERR_ENABLED"


@dataclass
class _Startup:
    app_server: CodexNativeAppServer
    event_client: SimpleNamespace
    bridge_dir: Path
    close_order: list[str]
    subagent_router: object
    turn_router: object
    shutdown_subagent: AsyncMock
    shutdown_turn: AsyncMock
    session_id: str = "child-codex-startup"


@pytest.fixture
def startup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Startup:
    """Provide an owned launch without starting subprocesses or network clients."""
    monkeypatch.delenv(_STDERR_ENV, raising=False)
    close_order: list[str] = []
    app_server = CodexNativeAppServer(
        codex_path="/test/codex",
        socket_path=tmp_path / "app-server.sock",
        codex_home=tmp_path / "codex-home",
        env={},
        config_overrides=[],
        cwd=tmp_path / "workspace",
        bridge_dir=tmp_path,
        proc=SimpleNamespace(pid=4242, returncode=None),  # type: ignore[arg-type]
        codex_cli_version=(0, 154, 0),
        recent_stderr=[],
    )

    async def close_client() -> None:
        close_order.append("client")

    async def close_app_server() -> None:
        close_order.append("app_server")
        app_server.proc = None
        app_server.stderr_task = None

    monkeypatch.setattr(app_server, "close", AsyncMock(side_effect=close_app_server))
    shutdown_subagent = AsyncMock(side_effect=lambda *_args: close_order.append("subagent"))
    shutdown_turn = AsyncMock(side_effect=lambda *_args: close_order.append("turn"))
    monkeypatch.setattr(orchestration, "_shutdown_session_router_async", shutdown_subagent)
    monkeypatch.setattr(orchestration, "_shutdown_session_turn_router_async", shutdown_turn)
    context = _Startup(
        app_server=app_server,
        event_client=SimpleNamespace(close=AsyncMock(side_effect=close_client)),
        bridge_dir=tmp_path,
        close_order=close_order,
        subagent_router=object(),
        turn_router=object(),
        shutdown_subagent=shutdown_subagent,
        shutdown_turn=shutdown_turn,
    )
    monkeypatch.setitem(orchestration._AUTO_CODEX_APP_SERVERS, context.session_id, app_server)
    return context


async def _discover(startup: _Startup, **kwargs: object) -> None:
    await orchestration._codex_discover_thread_and_forward(
        session_id=startup.session_id,
        bridge_dir=startup.bridge_dir,
        codex_ws_url="ws://127.0.0.1:9999",
        codex_home=startup.app_server.codex_home,
        workspace=str(startup.app_server.cwd),
        event_client=startup.event_client,  # type: ignore[arg-type]
        routing_summary="provider 'test' (model=gpt-test)",
        app_server=startup.app_server,
        subagent_router=startup.subagent_router,  # type: ignore[arg-type]
        turn_router=startup.turn_router,  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def _failure_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "codex_thread_start_failed"
    ]


@pytest.mark.parametrize(
    ("error", "configured_timeout", "login_required", "timeout_s", "reason", "cause"),
    [
        (TimeoutError(), None, False, 30.0, "timeout", "startup timed out after 30s"),
        (TimeoutError(), 120.0, False, 120.0, "timeout", "startup timed out after 120s"),
        (
            RuntimeError("event stream ended"),
            None,
            False,
            30.0,
            "event_stream_ended",
            "event stream ended before a thread was created",
        ),
        (
            RuntimeError("event stream ended"),
            120.0,
            True,
            None,
            "event_stream_ended",
            "event stream ended before a thread was created",
        ),
    ],
)
async def test_startup_failure_is_visible_at_error_and_belongs_to_child(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    startup: _Startup,
    error: Exception,
    configured_timeout: float | None,
    login_required: bool,
    timeout_s: float | None,
    reason: str,
    cause: str,
) -> None:
    monkeypatch.setenv(PRIMARY_SESSION_ID_ENV_VAR, "parent-runner-session")
    monkeypatch.setattr(forwarder, "wait_for_thread_started", AsyncMock(side_effect=error))

    with caplog.at_level(logging.ERROR, logger="omnigent.runner.app"):
        await _discover(
            startup,
            thread_start_timeout_seconds=configured_timeout,
            login_required=login_required,
        )

    records = _failure_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.ERROR
    attributes = record.attributes
    assert attributes["reason"] == reason
    assert attributes["timeout_s"] == timeout_s
    assert attributes["login_required"] is login_required
    assert attributes["elapsed_ms"] >= 0
    assert attributes["app_server_state"] == "running"
    assert attributes["app_server_pid"] == 4242
    assert attributes.get("app_server_returncode") is None
    assert attributes["codex_version"] == "0.154.0"

    row = record_to_row(record, source="runner")
    assert row["session_id"] == startup.session_id
    assert row["event_name"] == "codex_thread_start_failed"
    assert row["attributes"]["reason"] == reason
    assert row["attributes"]["app_server_pid"] == "4242"

    error_text = read_bridge_startup_error(startup.bridge_dir)
    assert error_text is not None
    assert cause in error_text
    assert type(error).__name__ in error_text
    assert "Launch routing: provider 'test' (model=gpt-test)" in error_text
    assert read_bridge_state(startup.bridge_dir) is None
    assert startup.close_order == ["client", "app_server", "subagent", "turn"]
    assert startup.session_id not in orchestration._AUTO_CODEX_APP_SERVERS
    startup.shutdown_subagent.assert_awaited_once_with(startup.session_id, startup.subagent_router)
    startup.shutdown_turn.assert_awaited_once_with(startup.session_id, startup.turn_router)


async def test_failure_snapshots_reader_and_process_before_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    startup: _Startup,
) -> None:
    async def broken_stderr_reader() -> None:
        raise ValueError("private stderr-reader exception detail")

    reader = asyncio.create_task(broken_stderr_reader())
    with pytest.raises(ValueError):
        await reader
    startup.app_server.stderr_task = reader
    startup.app_server.proc.returncode = 17
    monkeypatch.setattr(
        forwarder, "wait_for_thread_started", AsyncMock(side_effect=RuntimeError())
    )

    with caplog.at_level(logging.ERROR, logger="omnigent.runner.app"):
        await _discover(startup)

    [record] = _failure_records(caplog)
    assert record.attributes["app_server_state"] == "exited"
    assert record.attributes["app_server_pid"] == 4242
    assert record.attributes["app_server_returncode"] == 17
    assert record.attributes["stderr_reader_state"] == "failed"
    assert record.attributes["stderr_reader_error_type"] == "ValueError"
    assert "private stderr-reader exception detail" not in str(record_to_row(record, "runner"))
    assert startup.app_server.proc is None
    assert startup.app_server.stderr_task is None


async def test_failure_diagnostics_use_original_launch_not_registry_replacement(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    startup: _Startup,
) -> None:
    replacement = SimpleNamespace(proc=SimpleNamespace(pid=9999, returncode=9), close=AsyncMock())
    monkeypatch.setitem(orchestration._AUTO_CODEX_APP_SERVERS, startup.session_id, replacement)
    monkeypatch.setattr(
        forwarder, "wait_for_thread_started", AsyncMock(side_effect=TimeoutError())
    )

    with caplog.at_level(logging.ERROR, logger="omnigent.runner.app"):
        await _discover(startup)

    [record] = _failure_records(caplog)
    assert record.attributes["app_server_pid"] == 4242
    assert record.attributes["app_server_state"] == "running"
    assert record.attributes.get("app_server_returncode") is None
    replacement.close.assert_not_awaited()
    assert orchestration._AUTO_CODEX_APP_SERVERS[startup.session_id] is replacement
    assert startup.close_order == ["client", "app_server", "subagent", "turn"]
    assert read_bridge_startup_error(startup.bridge_dir) is None


def _exited_terminal(tmp_path: Path) -> TerminalInstance:
    instance = TerminalInstance(
        name="codex",
        session_key="main",
        socket_path=tmp_path / "terminal.sock",
        private_dir=tmp_path / "terminal",
        keep_alive_after_exit=True,
        running=False,
    )
    instance._remember_exit_status("1 2")
    instance._last_exit_snapshot = (
        "Authorization: Bearer "
        + "private-early-exit-token" * 1000
        + "\n"
        + "startup details\n" * 100
        + "\x1b[31merror: unexpected argument '--invalid' found\x1b[0m\n"
    )
    instance._remember_pane_snapshot("visible usage hint; initial error scrolled away")
    return instance


@pytest.mark.parametrize("capture", [None, "0", "1"])
@pytest.mark.parametrize("login_required", [False, True])
async def test_terminal_exit_fails_without_waiting_for_thread_timeout(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    startup: _Startup,
    capture: str | None,
    login_required: bool,
) -> None:
    if capture is not None:
        monkeypatch.setenv(_STDERR_ENV, capture)
    terminal = _exited_terminal(startup.bridge_dir)
    cancelled = asyncio.Event()

    async def thread_never_starts(*_args: object, **_kwargs: object) -> str:
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    monkeypatch.setattr(forwarder, "wait_for_thread_started", thread_never_starts)
    with caplog.at_level(logging.ERROR, logger="omnigent.runner.app"):
        await asyncio.wait_for(
            _discover(
                startup,
                terminal_instance=terminal,
                thread_start_timeout_seconds=120,
                login_required=login_required,
            ),
            timeout=1,
        )

    [record] = _failure_records(caplog)
    assert record.attributes["reason"] == "terminal_exited"
    assert record.attributes["terminal_exit_status"] == 2
    assert record.attributes["terminal_instance_id"] == terminal.diagnostic_id
    assert record.attributes["app_server_state"] == "running"
    assert record.attributes["app_server_pid"] == 4242
    assert cancelled.is_set()
    error = read_bridge_startup_error(startup.bridge_dir)
    assert error is not None and "terminal exited with status 2" in error
    local_line = RedactingLogFormatter(use_colors=False).format(record)
    row = record_to_row(record, "runner")
    if capture == "1":
        tail = record.attributes["terminal_last_output"]
        assert "unexpected argument '--invalid'" in tail
        assert "omitted" in tail
        assert len(tail) < 4096
        assert tail in local_line
        assert tail in error
    else:
        assert "terminal_last_output" not in record.attributes
        assert "unexpected argument" not in str(row) + local_line + error
    assert "private-early-exit-token" not in str(row) + local_line + error
    assert "\x1b" not in str(row) + local_line + error
    assert startup.close_order == ["client", "app_server", "subagent", "turn"]


async def test_stale_terminal_exit_does_not_stop_replacement_launch(
    monkeypatch: pytest.MonkeyPatch, startup: _Startup
) -> None:
    terminal = _exited_terminal(startup.bridge_dir)
    replacement = SimpleNamespace(close=AsyncMock())
    monkeypatch.setitem(orchestration._AUTO_CODEX_APP_SERVERS, startup.session_id, replacement)
    write_bridge_startup_error(startup.bridge_dir, "replacement launch marker")

    async def thread_never_starts(*_args: object, **_kwargs: object) -> str:
        await asyncio.Future()
        raise AssertionError("unreachable")

    monkeypatch.setattr(forwarder, "wait_for_thread_started", thread_never_starts)

    await asyncio.wait_for(_discover(startup, terminal_instance=terminal), timeout=1)

    assert orchestration._AUTO_CODEX_APP_SERVERS[startup.session_id] is replacement
    replacement.close.assert_not_awaited()
    assert startup.close_order == ["client", "app_server", "subagent", "turn"]
    assert read_bridge_startup_error(startup.bridge_dir) == "replacement launch marker"


async def test_thread_creation_wins_simultaneous_terminal_exit(tmp_path: Path) -> None:
    terminal = _exited_terminal(tmp_path)

    async def ready() -> str:
        return "usable-thread"

    assert await orchestration._wait_for_codex_thread_or_terminal_exit(ready(), terminal) == (
        "usable-thread"
    )


async def test_startup_race_cancellation_cancels_both_waiters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _exited_terminal(tmp_path)
    thread_started = asyncio.Event()
    probe_started = asyncio.Event()
    thread_cancelled = asyncio.Event()
    probe_cancelled = asyncio.Event()

    async def waiting_thread() -> str:
        thread_started.set()
        try:
            await asyncio.Future()
        finally:
            thread_cancelled.set()
        raise AssertionError("unreachable")

    async def waiting_probe() -> bool:
        probe_started.set()
        try:
            await asyncio.Future()
        finally:
            probe_cancelled.set()
        raise AssertionError("unreachable")

    monkeypatch.setattr(terminal, "is_alive", waiting_probe)
    task = asyncio.create_task(
        orchestration._wait_for_codex_thread_or_terminal_exit(waiting_thread(), terminal)
    )
    await asyncio.wait_for(asyncio.gather(thread_started.wait(), probe_started.wait()), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert thread_cancelled.is_set()
    assert probe_cancelled.is_set()


async def test_failure_emits_bounded_redacted_stderr_at_error_level(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    startup: _Startup,
) -> None:
    monkeypatch.setenv(_STDERR_ENV, "1")
    secret = "private-codex-startup-token"
    diagnostic = (
        "2026-09-21T12:00:00.000Z ERROR MCP connection failed: connection refused"
        + "; retry failed 失敗" * 250
    )
    body_diagnostic = "ERROR request body invalid: model field is missing"
    startup.app_server.recent_stderr = [diagnostic for _ in range(30)] + [
        body_diagnostic,
        f"2026-09-21T12:00:01.000Z ERROR MCP connection failed: Authorization: Bearer {secret}",
    ]
    monkeypatch.setattr(
        forwarder, "wait_for_thread_started", AsyncMock(side_effect=TimeoutError())
    )

    with caplog.at_level(logging.ERROR, logger="omnigent.runner.app"):
        await _discover(startup)

    [record] = _failure_records(caplog)
    tail = record.attributes["stderr_tail"]
    assert "MCP connection failed" in tail
    assert body_diagnostic in tail
    assert 4096 < len(tail.encode("utf-8")) <= 64 * 1024
    assert record.attributes["stderr_capture_enabled"] is True
    assert record.attributes["stderr_tail_available"] is True
    assert record.attributes["stderr_tail_truncated"] is True
    assert record.attributes["stderr_lines_omitted"] > 0
    assert record.attributes["stderr_bytes_omitted"] > 0
    assert secret not in str(record.attributes)
    assert secret not in str(record_to_row(record, "runner"))
    local_line = RedactingLogFormatter(use_colors=False).format(record)
    assert "Codex startup stderr:" in local_line
    assert "MCP connection failed" in local_line
    assert body_diagnostic in local_line
    assert secret not in local_line


@pytest.mark.parametrize("setting", [None, "0", "false"])
async def test_failure_omits_stderr_without_capture_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    startup: _Startup,
    setting: str | None,
) -> None:
    if setting is not None:
        monkeypatch.setenv(_STDERR_ENV, setting)
    diagnostic = "ERROR request body invalid: diagnostic disabled for this launch"
    startup.app_server.recent_stderr = [diagnostic]
    monkeypatch.setattr(
        forwarder, "wait_for_thread_started", AsyncMock(side_effect=TimeoutError())
    )

    with caplog.at_level(logging.ERROR, logger="omnigent.runner.app"):
        await _discover(startup)

    [record] = _failure_records(caplog)
    assert record.attributes["stderr_capture_enabled"] is False
    assert record.attributes["app_server_state"] == "running"
    assert record.attributes["app_server_pid"] == 4242
    assert record.attributes["stderr_reader_state"] == "not_started"
    capture_fields = {
        "stderr_tail",
        "stderr_tail_available",
        "stderr_tail_truncated",
        "stderr_lines_omitted",
        "stderr_bytes_omitted",
    }
    assert not capture_fields.intersection(record.attributes)
    row = record_to_row(record, "runner")
    assert not capture_fields.intersection(row["attributes"])
    assert diagnostic not in str(row)
    local_line = RedactingLogFormatter(use_colors=False).format(record)
    assert diagnostic not in local_line
    assert "Codex startup stderr:" not in local_line


async def test_diagnostics_error_preserves_original_failure_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    startup: _Startup,
) -> None:
    def broken_diagnostics(_app_server: CodexNativeAppServer | None) -> dict[str, object]:
        raise ValueError("private telemetry error: Authorization: Bearer helper-secret")

    discovery_error = TimeoutError("thread did not start")
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.diagnostics.collect_codex_startup_diagnostics",
        broken_diagnostics,
    )
    monkeypatch.setattr(
        forwarder, "wait_for_thread_started", AsyncMock(side_effect=discovery_error)
    )

    with caplog.at_level(logging.ERROR, logger="omnigent.runner.app"):
        await _discover(startup)

    [record] = _failure_records(caplog)
    assert record.attributes["diagnostics_error_type"] == "ValueError"
    assert record.attributes["reason"] == "timeout"
    assert record.exc_info[1] is discovery_error
    row = record_to_row(record, "runner")
    assert "TimeoutError: thread did not start" in row["stack_trace"]
    assert "private telemetry error" not in str(row)
    assert "helper-secret" not in str(row)
    error_text = read_bridge_startup_error(startup.bridge_dir)
    assert error_text is not None
    assert "startup timed out after 30s: TimeoutError" in error_text
    assert startup.close_order == ["client", "app_server", "subagent", "turn"]
    assert startup.session_id not in orchestration._AUTO_CODEX_APP_SERVERS


async def test_cancellation_cleans_up_without_reporting_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    startup: _Startup,
) -> None:
    monkeypatch.setattr(
        forwarder, "wait_for_thread_started", AsyncMock(side_effect=asyncio.CancelledError())
    )

    with caplog.at_level(logging.ERROR, logger="omnigent.runner.app"):
        with pytest.raises(asyncio.CancelledError):
            await _discover(startup)

    assert _failure_records(caplog) == []
    assert read_bridge_startup_error(startup.bridge_dir) is None
    assert read_bridge_state(startup.bridge_dir) is None
    assert startup.close_order == ["client", "app_server", "subagent", "turn"]


async def test_success_starts_forwarder_without_reporting_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    startup: _Startup,
) -> None:
    @asynccontextmanager
    async def open_server_client(
        *_args: object, **_kwargs: object
    ) -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(patch=AsyncMock(return_value=SimpleNamespace(status_code=200)))

    supervise = AsyncMock()
    monkeypatch.setattr(
        forwarder, "wait_for_thread_started", AsyncMock(return_value="thread-ready")
    )
    monkeypatch.setattr(forwarder, "supervise_forwarder", supervise)
    monkeypatch.setattr("omnigent.cli_auth.open_server_client", open_server_client)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:1")

    with caplog.at_level(logging.ERROR, logger="omnigent.runner.app"):
        await _discover(startup)

    assert _failure_records(caplog) == []
    assert read_bridge_startup_error(startup.bridge_dir) is None
    state = read_bridge_state(startup.bridge_dir)
    assert state is not None
    assert state.session_id == startup.session_id
    assert state.thread_id == "thread-ready"
    supervise.assert_awaited_once()
    assert supervise.await_args.kwargs["thread_id"] == "thread-ready"
    assert supervise.await_args.kwargs["client"] is startup.event_client
    assert startup.close_order == ["client", "app_server", "subagent", "turn"]
