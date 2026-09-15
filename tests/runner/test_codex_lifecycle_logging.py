"""Runner lifecycle records distinguish discovery, forwarding, and intentional teardown."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.debug_logging import PRIMARY_SESSION_ID_ENV_VAR, record_to_row
from omnigent.harnesses.codex_native import forwarder
from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    CodexNativeAppServer,
)
from omnigent.harnesses.codex_native.bridge import read_bridge_startup_error
from omnigent.runner import _entry
from omnigent.runner.identity import RUNNER_ID_ENV_VAR
from omnigent.runner.native import orchestration
from tests.runner.helpers import CodexAppServerDiagnosticsMixin

_SESSION_ID = "child-session"
_RUNNER_ID = "runner_token_0123456789abcdef0123456789abcdef"
_THREAD_ID = "019e8720-98d7-7b23-ac0a-bfb0eb02e0c9"


class _AppServer(CodexAppServerDiagnosticsMixin):
    def __init__(self, instance_id: str, pid: int = 12345) -> None:
        self.instance_id = instance_id
        self.pid = pid
        self.closed = False

    def diagnostic_attributes(self) -> dict[str, object]:
        return {
            "harness": "codex-native",
            "runner_id": _RUNNER_ID,
            "app_server_instance_id": self.instance_id,
            "app_server_pid": self.pid,
            "app_server_state": "closed" if self.closed else "running",
            "teardown_reason": self.teardown_reason,
        }

    async def close(self, *, reason: str = "caller_requested") -> None:
        self.record_teardown_reason(reason)
        self.closed = True


@pytest.fixture
def server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> _AppServer:
    caplog.set_level(logging.INFO, logger="omnigent.runner.app")
    monkeypatch.setenv(PRIMARY_SESSION_ID_ENV_VAR, "parent-session")
    monkeypatch.setenv(RUNNER_ID_ENV_VAR, _RUNNER_ID)
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://server.invalid")
    monkeypatch.setattr(_entry, "_make_auth_token_factory", lambda: None)
    monkeypatch.setattr(orchestration, "_AUTO_FORWARDER_TASKS", {})
    monkeypatch.setattr(orchestration, "_CODEX_FORWARDER_OWNERS", {})
    monkeypatch.setattr(orchestration, "_shutdown_session_router_async", AsyncMock())
    monkeypatch.setattr(orchestration, "_shutdown_session_turn_router_async", AsyncMock())
    instance = _AppServer("codex-native-original")
    monkeypatch.setattr(orchestration, "_AUTO_CODEX_APP_SERVERS", {_SESSION_ID: instance})
    monkeypatch.setattr(forwarder, "wait_for_thread_started", AsyncMock(return_value=_THREAD_ID))
    monkeypatch.setattr(forwarder, "supervise_forwarder", AsyncMock())
    (tmp_path / "bridge").mkdir()

    @asynccontextmanager
    async def open_client(*_args: object, **_kwargs: object):
        async with httpx.AsyncClient(
            base_url="http://server.invalid",
            transport=httpx.MockTransport(lambda _: httpx.Response(204)),
        ) as client:
            yield client

    monkeypatch.setattr("omnigent.cli_auth.open_server_client", open_client)
    return instance


async def _run_forwarder(
    tmp_path: Path,
    client: CodexAppServerClient,
    *,
    mode: str = "fresh",
    owner: _AppServer | None = None,
    login_required: bool = False,
) -> None:
    if mode == "fresh":
        await orchestration._codex_discover_thread_and_forward(
            session_id=_SESSION_ID,
            bridge_dir=tmp_path / "bridge",
            codex_ws_url="ws://private-endpoint.invalid",
            codex_home=tmp_path / "private-codex-home",
            workspace=str(tmp_path / "private-workspace"),
            event_client=client,
            routing_summary="private-routing-sentinel",
            login_required=login_required,
            app_server=cast("CodexNativeAppServer | None", owner),
        )
    else:
        await orchestration._codex_forward_known_thread(
            session_id=_SESSION_ID,
            bridge_dir=tmp_path / "bridge",
            codex_ws_url="ws://private-endpoint.invalid",
            thread_id=_THREAD_ID,
            client=client,
            app_server=cast("CodexNativeAppServer | None", owner),
        )


def _rows(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        record_to_row(record, "runner")
        for record in caplog.records
        if getattr(record, "event_name", None) == "codex_native_lifecycle"
    ]


def _event(caplog: pytest.LogCaptureFixture, phase: str) -> dict[str, str]:
    matching = [
        attrs
        for row in _rows(caplog)
        if (attrs := cast("dict[str, str]", row["attributes"]))["phase"] == phase
    ]
    assert len(matching) == 1
    return matching[0]


def _assert_safe_owned_rows(caplog: pytest.LogCaptureFixture) -> None:
    rows = _rows(caplog)
    assert rows
    assert all(row["session_id"] == _SESSION_ID for row in rows)
    for row in rows:
        attrs = cast("dict[str, str]", row["attributes"])
        assert attrs["runner_id"] == _RUNNER_ID
        assert attrs["app_server_instance_id"] == "codex-native-original"
        assert attrs["app_server_pid"] == "12345"
        assert "private-" not in json.dumps(attrs)
        if row["level"] == "INFO":
            assert row["stack_trace"] is None
            assert "private-" not in json.dumps(row)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (TimeoutError("private-timeout-detail"), "thread_discovery_timeout"),
        (RuntimeError("private-stream-detail"), "thread_stream_ended"),
    ],
)
async def test_discovery_failures_have_distinct_reasons_and_explicit_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    server: _AppServer,
    error: Exception,
    reason: str,
) -> None:
    monkeypatch.setattr(forwarder, "wait_for_thread_started", AsyncMock(side_effect=error))
    client = AsyncMock(spec=CodexAppServerClient)

    await _run_forwarder(tmp_path, client)

    failed = _event(caplog, "thread_discovery_failed")
    assert failed["reason"] == reason
    assert failed["error_type"] == type(error).__name__
    assert int(failed["elapsed_ms"]) >= 0
    stopped = _event(caplog, "forwarder_stopped")
    assert stopped["reason"] == reason
    assert stopped["forwarder_stage"] == "thread_discovery"
    assert server.closed and server.teardown_reason == reason
    client.close.assert_awaited_once()
    assert read_bridge_startup_error(tmp_path / "bridge") is not None
    # The original ERROR remains one ERROR; diagnostic breadcrumbs are INFO.
    assert sum(row["level"] == "ERROR" for row in _rows(caplog)) == 1
    _assert_safe_owned_rows(caplog)


async def test_unexpected_discovery_failure_is_preserved_and_classified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    server: _AppServer,
) -> None:
    error = ValueError("private-exception-detail")
    monkeypatch.setattr(forwarder, "wait_for_thread_started", AsyncMock(side_effect=error))
    client = AsyncMock(spec=CodexAppServerClient)

    with pytest.raises(ValueError) as raised:
        await _run_forwarder(tmp_path, client)

    assert raised.value is error
    stopped = _event(caplog, "forwarder_stopped")
    assert stopped["reason"] == "forwarder_failed"
    assert stopped["error_type"] == "ValueError"
    assert stopped["forwarder_stage"] == "thread_discovery"
    assert server.closed
    _assert_safe_owned_rows(caplog)


async def test_login_gated_discovery_is_distinct_from_a_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    server: _AppServer,
) -> None:
    wait = AsyncMock(return_value=_THREAD_ID)
    monkeypatch.setattr(forwarder, "wait_for_thread_started", wait)
    client = AsyncMock(spec=CodexAppServerClient)

    await _run_forwarder(tmp_path, client, login_required=True)

    wait.assert_awaited_once_with(client, timeout=None)
    started = _event(caplog, "thread_discovery_started")
    assert started["login_required"] == "True"
    assert started["discovery_has_deadline"] == "False"
    assert _event(caplog, "thread_discovered")["codex_thread_id"] == _THREAD_ID
    assert read_bridge_startup_error(tmp_path / "bridge") is None
    assert server.closed
    _assert_safe_owned_rows(caplog)


@pytest.mark.parametrize("mode", ["fresh", "resume"])
@pytest.mark.parametrize("outcome", ["return", "error", "cancel"])
async def test_forwarder_termination_preserves_behavior_and_classifies_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    server: _AppServer,
    mode: str,
    outcome: str,
) -> None:
    error = (
        ValueError("private-forwarder-detail")
        if outcome == "error"
        else asyncio.CancelledError()
        if outcome == "cancel"
        else None
    )
    monkeypatch.setattr(forwarder, "supervise_forwarder", AsyncMock(side_effect=error))
    client = AsyncMock(spec=CodexAppServerClient)

    if error is None:
        await _run_forwarder(tmp_path, client, mode=mode)
    else:
        with pytest.raises(type(error)) as raised:
            await _run_forwarder(tmp_path, client, mode=mode)
        assert raised.value is error

    reason = {
        "return": "forwarder_returned",
        "error": "forwarder_failed",
        "cancel": "forwarder_cancelled",
    }[outcome]
    assert _event(caplog, "forwarder_started")["forwarder_mode"] == mode
    stopped = _event(caplog, "forwarder_stopped")
    assert stopped["reason"] == reason
    assert stopped["forwarder_stage"] == "forwarding"
    assert stopped["codex_thread_id"] == _THREAD_ID
    assert stopped.get("error_type") == (type(error).__name__ if error else None)
    assert _event(caplog, "forwarder_cleanup")["cleanup_target_matches_owner"] == "True"
    assert server.closed and server.teardown_reason == reason
    client.close.assert_awaited_once()
    assert _SESSION_ID not in orchestration._AUTO_CODEX_APP_SERVERS
    _assert_safe_owned_rows(caplog)


@pytest.mark.parametrize("mode", ["fresh", "resume"])
async def test_caller_teardown_reason_is_recorded_before_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    server: _AppServer,
    mode: str,
) -> None:
    waiting = asyncio.Event()
    cancellation_reasons: list[str | None] = []

    async def park(*_args: object, **_kwargs: object) -> str:
        waiting.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_reasons.append(server.teardown_reason)
            raise
        raise AssertionError("parked wait must be cancelled")

    monkeypatch.setattr(
        forwarder, "wait_for_thread_started" if mode == "fresh" else "supervise_forwarder", park
    )
    task = asyncio.create_task(
        _run_forwarder(tmp_path, AsyncMock(spec=CodexAppServerClient), mode=mode)
    )
    orchestration._AUTO_FORWARDER_TASKS[_SESSION_ID] = task
    try:
        await asyncio.wait_for(waiting.wait(), timeout=2)
        await orchestration._cancel_auto_forwarder_task(_SESSION_ID, reason="session_deleted")
        assert task.cancelled()
        assert cancellation_reasons == ["session_deleted"]
        assert server.closed and server.teardown_reason == "session_deleted"
        stopped = _event(caplog, "forwarder_stopped")
        assert stopped["reason"] == "forwarder_cancelled"
        assert stopped["teardown_reason"] == "session_deleted"
        _assert_safe_owned_rows(caplog)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("mode", ["fresh", "resume"])
async def test_cleanup_reports_actual_target_without_reassigning_process_identity(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    server: _AppServer,
    mode: str,
) -> None:
    successor = _AppServer("codex-native-successor", pid=23456)
    orchestration._AUTO_CODEX_APP_SERVERS[_SESSION_ID] = cast("CodexNativeAppServer", successor)

    await _run_forwarder(tmp_path, AsyncMock(spec=CodexAppServerClient), mode=mode, owner=server)

    cleanup = _event(caplog, "forwarder_cleanup")
    assert cleanup["cleanup_target_matches_owner"] == "False"
    assert cleanup["cleanup_app_server_instance_id"] == "codex-native-successor"
    assert cleanup["cleanup_app_server_pid"] == "23456"
    # Instrument the existing session-keyed cleanup without changing its target.
    assert successor.closed and not server.closed
    _assert_safe_owned_rows(caplog)


@pytest.mark.parametrize("replace_task", [False, True])
async def test_task_owner_keeps_cancellation_intent_when_session_entry_is_replaced(
    server: _AppServer, replace_task: bool
) -> None:
    entered = asyncio.Event()
    cancellation_reasons: list[str | None] = []

    async def incumbent() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_reasons.append(server.teardown_reason)
            raise

    old_task = asyncio.create_task(incumbent())
    tasks = [old_task]
    orchestration._register_auto_forwarder_task(
        _SESSION_ID, old_task, app_server=cast("CodexNativeAppServer", server)
    )
    successor = _AppServer("codex-native-successor", pid=23456)
    orchestration._AUTO_CODEX_APP_SERVERS[_SESSION_ID] = cast("CodexNativeAppServer", successor)
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if replace_task:
            new_task = asyncio.create_task(asyncio.Event().wait())
            tasks.append(new_task)
            orchestration._register_auto_forwarder_task(
                _SESSION_ID, new_task, app_server=cast("CodexNativeAppServer", successor)
            )
            await asyncio.gather(old_task, return_exceptions=True)
            assert cancellation_reasons == ["forwarder_replaced"]
            assert orchestration._AUTO_FORWARDER_TASKS[_SESSION_ID] is new_task
            assert len(orchestration._CODEX_FORWARDER_OWNERS) == 1
            assert orchestration._CODEX_FORWARDER_OWNERS[new_task] is successor
        else:
            await orchestration._cancel_auto_forwarder_task(_SESSION_ID, reason="session_deleted")
            assert cancellation_reasons == ["session_deleted"]
            assert old_task not in orchestration._CODEX_FORWARDER_OWNERS
        assert successor.teardown_reason is None
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert not orchestration._CODEX_FORWARDER_OWNERS
