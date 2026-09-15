"""Codex process lifecycle diagnostics retain ownership without logging payloads."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.debug_logging import PRIMARY_SESSION_ID_ENV_VAR, record_to_row
from omnigent.harnesses.codex_native import app_server as module
from omnigent.runner.identity import RUNNER_ID_ENV_VAR


class _Process:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.stderr = asyncio.StreamReader()
        self.exited = asyncio.Event()

    def finish(self, returncode: int) -> None:
        self.returncode = returncode
        self.stderr.feed_eof()
        self.exited.set()

    async def wait(self) -> int:
        await self.exited.wait()
        assert self.returncode is not None
        return self.returncode


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> module.CodexNativeAppServer:
    monkeypatch.setenv(RUNNER_ID_ENV_VAR, "runner_lifecycle_test")
    monkeypatch.setenv(PRIMARY_SESSION_ID_ENV_VAR, "parent_session")
    monkeypatch.setattr(module, "_codex_cli_version", AsyncMock(return_value=(0, 155, 0)))
    monkeypatch.setattr(module, "_codex_home_config_source_from_env", lambda: tmp_path / "source")
    for name in (
        "_populate_codex_home_config",
        "_inject_mcp_server_config",
        "_sync_codex_developer_instructions",
        "_write_codex_policy_hooks_file",
        "write_policy_hook_config",
        "reconcile_codex_native_process_registry",
        "register_codex_native_process",
        "unregister_codex_native_process",
    ):
        monkeypatch.setattr(module, name, Mock())
    monkeypatch.setattr(
        module, "materialize_codex_provider_config", lambda _home, overrides: overrides
    )
    monkeypatch.setattr(
        module, "acquire_codex_native_process_owner_lock", Mock(return_value=Mock())
    )
    monkeypatch.setattr(module, "_process_group_id", lambda proc: proc.pid)
    monkeypatch.setattr(module.CodexNativeAppServer, "_wait_until_ready", AsyncMock())
    monkeypatch.setattr(module.CodexNativeAppServer, "_trust_policy_hooks", AsyncMock())
    monkeypatch.setattr(module, "_terminate_process_tree", lambda proc: proc.finish(-15))
    monkeypatch.setattr(module, "_kill_process_tree", lambda proc: proc.finish(-9))
    processes: list[_Process] = []

    async def spawn(*_args: object, **_kwargs: object) -> _Process:
        proc = _Process(pid=40000 + len(processes))
        processes.append(proc)
        return proc

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    return module.CodexNativeAppServer(
        codex_path="/private/codex-binary",
        socket_path=tmp_path / "private-codex.sock",
        codex_home=tmp_path / "private-codex-home",
        env={"OPENAI_API_KEY": "secret-env-sentinel"},
        config_overrides=['private_setting="secret-config-sentinel"'],
        cwd=tmp_path,
        bridge_dir=tmp_path / "private-bridge",
        developer_instructions="secret-instructions-sentinel",
        ap_server_url="https://private-server.example.invalid",
        ap_auth_headers={"Authorization": "Bearer secret-auth-sentinel"},
        session_id="child_session",
    )


def _rows(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        record_to_row(record, "runner")
        for record in caplog.records
        if getattr(record, "event_name", None) == "codex_native_lifecycle"
    ]


def _events(caplog: pytest.LogCaptureFixture, phase: str) -> list[dict[str, str]]:
    return [
        attrs
        for row in _rows(caplog)
        if (attrs := cast("dict[str, str]", row["attributes"]))["phase"] == phase
    ]


async def test_lifecycle_records_explicit_owner_and_safe_process_identity(
    server: module.CodexNativeAppServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=module.__name__)
    assert server.diagnostic_attributes()["app_server_state"] == "not_started"

    await server.start()
    instance_id = server.process_registry_tag
    proc = server.proc
    assert proc is not None
    assert server.diagnostic_attributes()["app_server_state"] == "running"
    assert server.diagnostic_attributes()["app_server_pid"] == proc.pid
    assert _events(caplog, "spawned")[0]["app_server_instance_id"] == instance_id
    await server.close(reason="session_teardown")

    rows = _rows(caplog)
    assert [cast("dict[str, str]", row["attributes"])["phase"] for row in rows] == [
        "starting",
        "spawned",
        "ready",
        "teardown_requested",
        "process_exited",
        "closed",
    ]
    assert all(row["session_id"] == "child_session" for row in rows)
    assert all(row["level"] == "INFO" and row["stack_trace"] is None for row in rows)
    for row in rows:
        attrs = cast("dict[str, str]", row["attributes"])
        assert attrs["app_server_instance_id"] == instance_id
        assert attrs["runner_id"] == "runner_lifecycle_test"
    exit_event = _events(caplog, "process_exited")[0]
    assert exit_event["app_server_pid"] == str(proc.pid)
    assert exit_event["app_server_returncode"] == "-15"
    assert exit_event["app_server_exit_signal"] == "15"
    assert exit_event["app_server_exit_expected"] == "True"
    assert exit_event["teardown_reason"] == "session_teardown"
    assert exit_event["codex_cli_version"] == "0.155.0"
    assert int(exit_event["app_server_lifetime_ms"]) >= 0
    assert server.process_registry_tag is None
    assert server.diagnostic_attributes()["app_server_instance_id"] == instance_id
    assert server.diagnostic_attributes()["app_server_pid"] == proc.pid
    assert server.diagnostic_attributes()["app_server_returncode"] == -15
    assert server.diagnostic_attributes()["app_server_state"] == "closed"
    serialized = json.dumps(rows)
    for private_value in (
        "secret-env-sentinel",
        "secret-config-sentinel",
        "secret-instructions-sentinel",
        "secret-auth-sentinel",
        "private-server.example.invalid",
        "private-codex.sock",
        "private-codex-home",
        "private-bridge",
        "/private/codex-binary",
    ):
        assert private_value not in serialized


@pytest.mark.parametrize("returncode", [0, 17, -9])
async def test_unrequested_exit_is_observed_without_close(
    server: module.CodexNativeAppServer,
    caplog: pytest.LogCaptureFixture,
    returncode: int,
) -> None:
    caplog.set_level(logging.INFO, logger=module.__name__)
    await server.start()
    proc = cast("_Process", server.proc)
    lifecycle = server._lifecycle
    assert lifecycle is not None and lifecycle.exit_task is not None
    proc.finish(returncode)
    await asyncio.wait_for(lifecycle.exit_task, timeout=1)

    try:
        events = _events(caplog, "process_exited")
        assert len(events) == 1
        assert events[0]["app_server_returncode"] == str(returncode)
        assert events[0]["app_server_exit_expected"] == "False"
        assert events[0].get("app_server_exit_signal") == (
            str(-returncode) if returncode < 0 else None
        )
        assert "teardown_reason" not in events[0]
        assert server.proc is proc
        assert server.diagnostic_attributes()["app_server_state"] == "exited"
    finally:
        await server.close(reason="terminal_deleted")
    assert len(_events(caplog, "process_exited")) == 1
    assert server.diagnostic_attributes()["app_server_exit_expected"] is False


async def test_close_cannot_reclassify_exit_before_observer_runs(
    server: module.CodexNativeAppServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=module.__name__)
    await server.start()
    cast("_Process", server.proc).finish(23)
    await server.close()

    assert _events(caplog, "process_exited")[0]["app_server_exit_expected"] == "False"
    phases = [cast("dict[str, str]", row["attributes"])["phase"] for row in _rows(caplog)]
    assert phases.index("process_exited") < phases.index("teardown_requested")
    assert len(_events(caplog, "process_exited")) == 1


async def test_exit_observer_does_not_wait_for_inherited_stderr_pipe(
    server: module.CodexNativeAppServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=module.__name__)
    await server.start()
    lifecycle = server._lifecycle
    assert lifecycle is not None and lifecycle.exit_task is not None
    await asyncio.sleep(0)
    proc = cast("_Process", server.proc)
    proc.returncode = 31
    await asyncio.wait_for(lifecycle.exit_task, timeout=1)
    try:
        assert not proc.exited.is_set()
        assert not proc.stderr.at_eof()
        assert _events(caplog, "process_exited")[0]["app_server_returncode"] == "31"
    finally:
        await server.close()


async def test_observer_reports_a_real_subprocess_exit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=module.__name__)
    server = module.CodexNativeAppServer(
        codex_path=sys.executable,
        socket_path=tmp_path / "socket",
        codex_home=tmp_path / "codex-home",
        env={},
        config_overrides=[],
        cwd=tmp_path,
        bridge_dir=tmp_path / "bridge",
        session_id="real_process_owner",
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys; sys.exit(19)",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    lifecycle = module._CodexAppServerLifecycle(
        instance_id="codex-native-real-process",
        session_id=server.session_id,
        runner_id=None,
        proc=proc,
        pid=proc.pid,
        spawned_at=time.monotonic(),
    )
    try:
        await asyncio.wait_for(server._observe_process_exit(lifecycle, proc), timeout=5)
        event = _events(caplog, "process_exited")[0]
        assert event["app_server_returncode"] == "19"
        assert event["app_server_pid"] == str(proc.pid)
        assert event["app_server_exit_expected"] == "False"
        assert _rows(caplog)[0]["session_id"] == "real_process_owner"
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


async def test_teardown_intent_is_first_wins_and_close_is_idempotent(
    server: module.CodexNativeAppServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=module.__name__)
    await server.start()
    lifecycle = server._lifecycle
    assert lifecycle is not None
    observer = lifecycle.exit_task
    stderr = server.stderr_task
    owner_lock = server.process_owner_lock
    server.record_teardown_reason("thread_discovery_timeout")
    server.record_teardown_reason("forwarder_cancelled")
    assert server.diagnostic_attributes()["app_server_state"] == "running"
    assert _events(caplog, "teardown_requested")[0]["reason"] == "thread_discovery_timeout"
    await server.close()
    await server.close(reason="session_teardown")
    server.record_teardown_reason("different_reason")

    assert len(_events(caplog, "teardown_requested")) == 1
    assert len(_events(caplog, "process_exited")) == 1
    assert len(_events(caplog, "closed")) == 1
    assert server.diagnostic_attributes()["teardown_reason"] == "thread_discovery_timeout"
    assert observer is not None and observer.done()
    assert stderr is not None and stderr.done()
    assert lifecycle.exit_task is None
    assert owner_lock is not None
    cast("Mock", owner_lock.close).assert_called_once()


async def test_freeform_teardown_reason_cannot_expose_sensitive_details(
    server: module.CodexNativeAppServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=module.__name__)
    await server.start()
    await server.close(reason="request failed at https://example.invalid/private?secret=sentinel")
    assert _events(caplog, "teardown_requested")[0]["reason"] == "unspecified"
    assert "sentinel" not in json.dumps(_rows(caplog))


@pytest.mark.parametrize("failure_at", ["version", "spawn"])
async def test_startup_failure_before_spawn_records_only_exception_type(
    server: module.CodexNativeAppServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_at: str,
) -> None:
    caplog.set_level(logging.INFO, logger=module.__name__)
    error = OSError("sensitive startup details /private/failure-sentinel")
    if failure_at == "version":
        monkeypatch.setattr(module, "_codex_cli_version", AsyncMock(side_effect=error))
    else:
        monkeypatch.setattr(module.asyncio, "create_subprocess_exec", AsyncMock(side_effect=error))

    with pytest.raises(OSError) as raised:
        await server.start()

    assert raised.value is error
    assert _events(caplog, "startup_failed")[0]["error_type"] == "OSError"
    assert "failure-sentinel" not in json.dumps(_rows(caplog))
    assert server.proc is None and server.process_owner_lock is None
    assert server._lifecycle is not None and server._lifecycle.exit_task is None
    assert not _events(caplog, "process_exited")
    await server.close()


@pytest.mark.parametrize("failure_at", ["registration", "stderr_task"])
async def test_early_post_spawn_failure_preserves_existing_cleanup_boundary(
    server: module.CodexNativeAppServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_at: str,
) -> None:
    caplog.set_level(logging.INFO, logger=module.__name__)
    error = RuntimeError("early post-spawn setup failed")
    if failure_at == "registration":
        monkeypatch.setattr(module, "register_codex_native_process", Mock(side_effect=error))
    else:

        def fail_create_task(
            coroutine: Coroutine[object, object, None], *, name: str
        ) -> asyncio.Task[None]:
            assert name == "codex-native-app-server-stderr"
            coroutine.close()
            raise error

        monkeypatch.setattr(module.asyncio, "create_task", fail_create_task)

    try:
        with pytest.raises(RuntimeError) as raised:
            await server.start()

        assert raised.value is error
        assert server.proc is not None and server.proc.returncode is None
        assert server.process_owner_lock is not None
        cast("Mock", server.process_owner_lock.close).assert_not_called()
        assert server.stderr_task is None
        assert server._lifecycle is not None and server._lifecycle.exit_task is None
        assert [cast("dict[str, str]", row["attributes"])["phase"] for row in _rows(caplog)] == [
            "starting",
            "spawned",
            "startup_failed",
        ]
    finally:
        await server.close()


async def test_readiness_failure_logs_cause_before_cleanup_and_reraises(
    server: module.CodexNativeAppServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=module.__name__)
    error = RuntimeError("readiness failure with sensitive context")
    monkeypatch.setattr(server, "_wait_until_ready", AsyncMock(side_effect=error))
    with pytest.raises(RuntimeError) as raised:
        await server.start()

    assert raised.value is error
    assert [cast("dict[str, str]", row["attributes"])["phase"] for row in _rows(caplog)] == [
        "starting",
        "spawned",
        "startup_failed",
        "teardown_requested",
        "process_exited",
        "closed",
    ]
    assert _events(caplog, "teardown_requested")[0]["reason"] == "startup_failed"
    assert server.proc is None and server.stderr_task is None
    assert server._lifecycle is not None and server._lifecycle.exit_task is None
    assert server.diagnostic_attributes()["app_server_returncode"] == -15


@pytest.mark.parametrize("cancel_at", ["_wait_until_ready", "_trust_policy_hooks"])
async def test_startup_cancellation_cleans_up_observer_and_preserves_cancellation(
    server: module.CodexNativeAppServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    cancel_at: str,
) -> None:
    caplog.set_level(logging.INFO, logger=module.__name__)
    entered = asyncio.Event()

    async def blocked() -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(server, cancel_at, blocked)
    task = asyncio.create_task(server.start())
    await asyncio.wait_for(entered.wait(), timeout=1)
    lifecycle = server._lifecycle
    assert lifecycle is not None
    observer = lifecycle.exit_task
    stderr = server.stderr_task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _events(caplog, "startup_failed")[0]["error_type"] == "CancelledError"
    assert _events(caplog, "teardown_requested")[0]["reason"] == "startup_cancelled"
    assert observer is not None and observer.done()
    assert stderr is not None and stderr.done()
    assert lifecycle.exit_task is None
    assert server.proc is None
    assert server.diagnostic_attributes()["app_server_state"] == "closed"


async def test_restart_gets_fresh_identity_and_diagnostics(
    server: module.CodexNativeAppServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=module.__name__)
    await server.start()
    first_id = server.process_registry_tag
    first_pid = server.diagnostic_attributes()["app_server_pid"]
    await server.close(reason="thread_discovery_timeout")
    server.session_id = "different_child_session"
    monkeypatch.setenv(RUNNER_ID_ENV_VAR, "different_runner")

    await server.start()
    try:
        attrs = server.diagnostic_attributes()
        assert attrs["app_server_instance_id"] != first_id
        assert attrs["app_server_pid"] != first_pid
        assert attrs["app_server_state"] == "running"
        assert attrs["app_server_returncode"] is None
        assert attrs["teardown_reason"] is None
        assert attrs["app_server_exit_expected"] is None
        assert attrs["runner_id"] == "different_runner"
        assert _rows(caplog)[-1]["session_id"] == "different_child_session"
    finally:
        await server.close()


async def test_late_exit_observer_never_uses_successor_identity(
    server: module.CodexNativeAppServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=module.__name__)
    await server.start()
    old_proc = _Process(39000)
    old_lifecycle = module._CodexAppServerLifecycle(
        instance_id="codex-native-earlier-instance",
        session_id="earlier_owner",
        runner_id="earlier_runner",
        proc=cast("asyncio.subprocess.Process", old_proc),
        pid=old_proc.pid,
        spawned_at=time.monotonic(),
    )
    observer = asyncio.create_task(
        server._observe_process_exit(old_lifecycle, cast("asyncio.subprocess.Process", old_proc))
    )
    old_proc.finish(27)
    await asyncio.wait_for(observer, timeout=1)
    try:
        row = _rows(caplog)[-1]
        attrs = cast("dict[str, str]", row["attributes"])
        assert row["session_id"] == "earlier_owner"
        assert attrs["app_server_instance_id"] == "codex-native-earlier-instance"
        assert attrs["runner_id"] == "earlier_runner"
        assert attrs["app_server_pid"] == "39000"
        assert attrs["app_server_returncode"] == "27"
        assert server.diagnostic_attributes()["app_server_state"] == "running"
        assert server.diagnostic_attributes()["app_server_returncode"] is None
    finally:
        await server.close()
