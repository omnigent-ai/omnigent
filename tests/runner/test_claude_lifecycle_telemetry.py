"""Hook-to-runner exit telemetry preserves attribution without changing outcomes."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Response

from omnigent.debug_logging import current_session_id_scope, record_to_row
from omnigent.harnesses.claude_native import bridge, hook, lifecycle
from omnigent.inner.terminal import TerminalInstance
from omnigent.runner import create_runner_app
from omnigent.runner import resource_registry as resource_registry_mod
from omnigent.runner.app import _session_event_queues_ref
from omnigent.runner.native.interrupt import NativeInterruptRunner
from omnigent.runner.resource_registry import (
    CLAUDE_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
    TerminalExitEvent,
    TerminalLifecycle,
)
from omnigent.terminals import TerminalRegistry
from tests.runner.conftest import (
    _drain_session_event_queue,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient, make_test_terminal_instance

_SESSION = "synthetic-lifecycle-session"


@dataclass
class _Launch:
    app: FastAPI
    resources: SessionResourceRegistry
    terminals: TerminalRegistry
    instance: TerminalInstance
    directory: Path
    callbacks: dict[str, object]


@pytest.fixture
async def launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    monkeypatch.setenv("OMNIGENT_HARNESS_STDERR_ENABLED", "0")
    directory = bridge.prepare_bridge_dir(_SESSION, workspace=tmp_path)
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    instance.command = "isaac"
    instance.args = bridge.augment_claude_args((), bridge_dir=directory)
    instance.lifecycle_trace.session_id = _SESSION
    for key, value in instance.lifecycle_trace.launch_environment(instance.diagnostic_id).items():
        monkeypatch.setenv(key, value)
    callbacks: dict[str, object] = {}

    def capture(on_idle=None, **kwargs):
        callbacks.update(on_idle=on_idle, **kwargs)

    monkeypatch.setattr(instance, "start_idle_watcher_thread", capture)
    monkeypatch.setattr(instance, "_tmux", AsyncMock())
    terminals = TerminalRegistry()
    terminals._by_conversation[_SESSION] = {("claude", "main"): instance}
    process_manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    process_manager._sessions.add(_SESSION)
    app = create_runner_app(
        terminal_registry=terminals,
        process_manager=process_manager,
        server_client=NullServerClient(),
    )
    resources = app.state.session_resource_registry
    monkeypatch.setattr(resources, "_build_claude_native_status_poller", lambda **_kw: None)
    await resources.observe_required_terminal(
        _SESSION, "claude", "main", instance, resource_role=CLAUDE_NATIVE_TERMINAL_ROLE
    )
    yield _Launch(app, resources, terminals, instance, directory, callbacks)
    _session_event_queues_ref.pop(_SESSION, None)
    await resources.cleanup_session(_SESSION)
    await asyncio.sleep(0)


def _hook(
    launch: _Launch, monkeypatch: pytest.MonkeyPatch, name: str, at: float, **payload
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(hook, "time", SimpleNamespace(**{**vars(time), "time": lambda: at}))
        patch.setattr(
            sys,
            "stdin",
            io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": name,
                        "session_id": "claude-synthetic",
                        **payload,
                    }
                )
            ),
        )
        assert hook.main(["--bridge-dir", str(launch.directory)]) == 0


def _rows(caplog: pytest.LogCaptureFixture, event_name: str) -> list[dict[str, object]]:
    return [
        record_to_row(record, source="runner")
        for record in caplog.records
        if getattr(record, "event_name", None) == event_name
    ]


@pytest.mark.parametrize("reason", ["prompt_input_exit", "logout", None])
async def test_reason_survives_cleanup_and_sink_serialization_without_reclassifying_exit(
    launch: _Launch,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    reason: str | None,
) -> None:
    caplog.set_level(logging.INFO)
    _hook(launch, monkeypatch, "SessionStart", 100.0, source="startup")
    _hook(launch, monkeypatch, "UserPromptSubmit", 101.0, prompt="private synthetic prompt")
    _hook(launch, monkeypatch, "Stop", 102.0)
    launch.resources.note_session_turn_started(_SESSION)
    launch.resources.note_external_session_status(_SESSION, "idle")
    launch.callbacks["on_activity"]()
    _hook(
        launch, monkeypatch, "SessionEnd", 103.25, reason=reason, token="private-synthetic-token"
    )
    instance = launch.instance
    instance._remember_exit_status("1 0")
    instance._remember_pane_snapshot("\n" * 100 + "Pane is dead")
    original_close = instance.close

    async def cleanup() -> None:
        await original_close()
        # Exit export must own a snapshot before cleanup loses its source files.
        for path in launch.directory.glob("lifecycle-*.json"):
            path.unlink()

    monkeypatch.setattr(instance, "close", cleanup)
    instance.running = False
    with current_session_id_scope("synthetic-parent"):
        launch.callbacks["on_exit"]()
    await asyncio.wait_for(launch.resources.wait_for_terminal_exit_cleanup(), timeout=2)

    [row] = _rows(caplog, "required_terminal_exited")
    attributes = row["attributes"]
    assert row["session_id"] == _SESSION
    assert attributes["terminal_instance_id"] == instance.diagnostic_id
    assert attributes["terminal_launch_id"] == instance.lifecycle_trace.launch_id
    assert attributes["terminal_launch_session_id"] == _SESSION
    assert attributes["terminal_id"] == "terminal_claude_main"
    assert attributes["terminal_exit_status"] == "0"
    assert attributes["terminal_exit_status_process"] == "launched_command"
    assert attributes["terminal_exit_status_source"] == "tmux_pane_dead_status"
    assert "claude_child_exit_status" not in attributes
    assert attributes["claude_session_end_reason"] == (reason or "unknown")
    assert attributes["claude_session_end_reason_status"] == ("reported" if reason else "missing")
    assert attributes["claude_session_end_evidence"] == "claude_hook"
    assert attributes["claude_session_end_at"] == "103.25"
    assert attributes["claude_session_end_bridge_session_id"] == _SESSION
    assert attributes["claude_session_end_timestamp_source"] == "hook_received"
    assert attributes["session_status_before_exit"] == "running"
    assert attributes["session_turn_active_before_exit"] == "false"
    assert attributes["claude_hook_turn_in_progress"] == "false"
    assert "terminal_cleanup_started_at" not in attributes
    evidence = json.loads(attributes["claude_lifecycle"])
    assert evidence["session_end"]["signal"] is None
    assert evidence["hook_turn_in_progress"] is False
    assert json.loads(attributes["terminal_control_requests"]) == []
    history = json.loads(attributes["terminal_status_history"])
    assert [(entry["status"], entry["source"]) for entry in history][-2:] == [
        ("idle", "forwarded_status"),
        ("running", "pane_activity"),
    ]
    assert "private synthetic prompt" not in json.dumps(row)
    assert "private-synthetic-token" not in json.dumps(row)
    [cleanup_row] = _rows(caplog, "terminal_cleanup_started")
    cleanup_attributes = cleanup_row["attributes"]
    assert float(cleanup_attributes["terminal_cleanup_started_at"]) >= float(
        attributes["terminal_exit_observed_at"]
    )
    assert json.loads(cleanup_attributes["terminal_control_requests"]) == []
    statuses = _drain_session_event_queue(_session_event_queues_ref.get(_SESSION))
    assert any(
        event.get("error", {}).get("code") == "required_terminal_exited" for event in statuses
    )


@pytest.mark.parametrize("failure", ["reader", "serialization"])
async def test_lifecycle_capture_failure_cannot_prevent_exit_cleanup_or_failure(
    launch: _Launch,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    caplog.set_level(logging.INFO)

    def fail(*_args):
        raise OSError("private synthetic error")

    if failure == "reader":
        monkeypatch.setattr(lifecycle, "read_lifecycle_snapshot", fail)
    else:
        monkeypatch.setattr(resource_registry_mod, "lifecycle_log_attributes", fail)
    launch.instance.running = False
    launch.callbacks["on_exit"]()
    await asyncio.wait_for(launch.resources.wait_for_terminal_exit_cleanup(), timeout=2)

    assert launch.terminals.get(_SESSION, "claude", "main") is None
    [row] = _rows(caplog, "required_terminal_exited")
    assert row["attributes"]["lifecycle_capture_failed"] == "true"
    assert row["attributes"]["lifecycle_capture_error_type"] == "OSError"
    assert "private synthetic error" not in json.dumps(row)


async def test_diagnostic_key_collisions_cannot_override_or_block_terminal_failure(
    launch: _Launch, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    released = asyncio.Event()

    async def release(_self, session_id: str) -> None:
        assert session_id == _SESSION
        released.set()

    monkeypatch.setattr(_FakeProcessManager, "release", release)
    publish_exit = launch.resources._terminal_exit_publisher
    assert publish_exit is not None
    publish_exit(
        TerminalExitEvent(
            session_id=_SESSION,
            terminal_id="terminal_claude_main",
            terminal_name="claude",
            session_key="main",
            lifecycle=TerminalLifecycle.REQUIRED,
            command="claude",
            exit_status=1,
            lifecycle_context={
                "event_name": "wrong_event",
                "session_id": "wrong_session",
                "terminal_name": "wrong_terminal",
                "terminal_exit_status": "0",
                "error_code": "wrong_code",
                "error_impact": "wrong_impact",
                "claude_session_end_reason": "unknown",
            },
        )
    )
    await asyncio.wait_for(released.wait(), timeout=2)
    [row] = _rows(caplog, "required_terminal_exited")
    assert row["session_id"] == _SESSION
    assert row["attributes"]["terminal_name"] == "claude"
    assert row["attributes"]["terminal_exit_status"] == "1"
    assert row["attributes"]["error_code"] == "required_terminal_exited"
    assert row["attributes"]["error_impact"] == "blocking"
    assert row["attributes"]["claude_session_end_reason"] == "unknown"
    assert any(
        event.get("status") == "failed"
        and event.get("error", {}).get("code") == "required_terminal_exited"
        for event in _drain_session_event_queue(_session_event_queues_ref.get(_SESSION))
    )


async def test_old_terminal_exit_does_not_read_replacement_hook_evidence(
    launch: _Launch,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    _hook(launch, monkeypatch, "SessionStart", 100.0, source="startup")
    _hook(launch, monkeypatch, "SessionEnd", 101.0, reason="logout")
    replacement = make_test_terminal_instance("claude", "main", tmp_path / "replacement")
    replacement.args = list(launch.instance.args)
    launch.terminals._by_conversation[_SESSION][("claude", "main")] = replacement
    launch.resources.note_session_turn_started(_SESSION)
    await launch.resources._finalize_terminal_exit(
        session_id=_SESSION,
        terminal_name="claude",
        session_key="main",
        lifecycle=TerminalLifecycle.REQUIRED,
        instance=launch.instance,
        resource_role=CLAUDE_NATIVE_TERMINAL_ROLE,
    )

    [row] = _rows(caplog, "terminal_exit_observed")
    assert row["attributes"]["terminal_instance_id"] == launch.instance.diagnostic_id
    assert row["attributes"]["claude_session_end_reason"] == "logout"
    assert row["attributes"]["superseded"] == "True"
    assert "session_turn_active_before_exit" not in row["attributes"]
    assert "session_activity_epoch" not in row["attributes"]
    assert _rows(caplog, "required_terminal_exited") == []
    assert launch.terminals.get(_SESSION, "claude", "main") is replacement
    assert launch.resources.session_turn_is_active(_SESSION)


@pytest.mark.parametrize("action", ["interrupt", "stop_session"])
async def test_runner_request_is_recorded_before_native_control_dispatch(
    launch: _Launch,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    action: str,
) -> None:
    caplog.set_level(logging.INFO)

    async def dispatch(_self, _harness, session_id):
        assert session_id == _SESSION
        assert (
            launch.instance.lifecycle_trace.snapshot()["terminal_control_request_action"] == action
        )
        return Response(status_code=204)

    monkeypatch.setattr(
        NativeInterruptRunner, "interrupt" if action == "interrupt" else "stop", dispatch
    )
    async with _runner_client(launch.app) as client:
        response = await client.post(f"/v1/sessions/{_SESSION}/events", json={"type": action})
    assert response.status_code == 204
    [row] = _rows(caplog, "native_terminal_control_requested")
    assert row["attributes"]["terminal_control_request_action"] == action
    assert float(row["attributes"]["terminal_control_requested_at"]) > 0
    assert "terminal_cleanup_started_at" not in row["attributes"]
    assert "terminal_exit_status_source" not in row["attributes"]
    assert "terminal_exit_status_process" not in row["attributes"]
    assert launch.instance.running


async def test_session_delete_records_request_before_process_cancel_and_cleanup(
    launch: _Launch, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    _hook(launch, monkeypatch, "SessionStart", 100.0, source="startup")
    _hook(launch, monkeypatch, "SessionEnd", 101.0, reason="logout")

    async def cancel(_self, session_id: str) -> bool:
        assert session_id == _SESSION
        snapshot = launch.instance.lifecycle_trace.snapshot()
        assert snapshot["terminal_control_request_action"] == "delete_session"
        assert snapshot["terminal_cleanup_started_at"] is None
        return True

    monkeypatch.setattr(_FakeProcessManager, "forward_cancel", cancel)
    async with _runner_client(launch.app) as client:
        response = await client.delete(f"/v1/sessions/{_SESSION}")
    assert response.status_code == 200
    assert not launch.directory.exists()
    [request] = _rows(caplog, "native_terminal_control_requested")
    [cleanup] = _rows(caplog, "terminal_cleanup_started")
    assert request["attributes"]["terminal_control_request_action"] == "delete_session"
    assert request["attributes"]["claude_session_end_reason"] == "logout"
    assert request["attributes"]["claude_session_end_at"] == "101.0"
    assert request["attributes"]["claude_session_end_identity"] == "matched"
    assert (
        json.loads(request["attributes"]["claude_lifecycle"])["session_end"]["reason"] == "logout"
    )
    assert float(request["attributes"]["terminal_control_requested_at"]) <= float(
        cleanup["attributes"]["terminal_cleanup_started_at"]
    )
    assert _rows(caplog, "required_terminal_exited") == []


async def test_transfer_logs_current_owner_and_original_launch_session(
    launch: _Launch, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    _hook(launch, monkeypatch, "SessionStart", 100.0, source="startup")
    target = "synthetic-transferred-session"
    assert await launch.resources.transfer_terminal(_SESSION, target, "terminal_claude_main")
    try:
        _hook(launch, monkeypatch, "SessionStart", 101.0, session_id="claude-new", source="clear")
        _hook(launch, monkeypatch, "SessionEnd", 102.0, session_id="claude-new", reason="logout")
        launch.resources.note_terminal_control_request(target, "delete_session")
        [request] = _rows(caplog, "native_terminal_control_requested")
        assert request["session_id"] == target
        assert request["attributes"]["terminal_current_session_id"] == target
        assert request["attributes"]["terminal_launch_session_id"] == _SESSION
        assert request["attributes"]["claude_session_id"] == "claude-new"
        assert request["attributes"]["claude_session_end_reason"] == "logout"
        assert (
            json.loads(request["attributes"]["claude_lifecycle"])["launch_session_id"] == _SESSION
        )
    finally:
        await launch.resources.cleanup_session(target)


async def test_startup_exit_captures_reason_before_closing_unobserved_terminal(
    launch: _Launch, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    _hook(launch, monkeypatch, "SessionStart", 100.0, source="startup")
    _hook(launch, monkeypatch, "SessionEnd", 101.0, reason="signal", signal="SIGTERM")
    launch.instance.running = False
    launch.instance._exit_status_is_pending("1||TERM")
    await launch.resources._finalize_terminal_exit(
        session_id=_SESSION,
        terminal_name="claude",
        session_key="main",
        lifecycle=TerminalLifecycle.REQUIRED,
        instance=launch.instance,
        resource_role=CLAUDE_NATIVE_TERMINAL_ROLE,
        before_observation=True,
    )

    [row] = _rows(caplog, "terminal_exit_observed")
    assert row["attributes"]["before_observation"] == "True"
    assert row["attributes"]["claude_session_end_reason"] == "signal"
    assert row["attributes"]["claude_session_end_at"] == "101.0"
    assert row["attributes"]["claude_session_end_signal"] == "SIGTERM"
    assert row["attributes"]["terminal_exit_signal"] == "SIGTERM"
    assert "terminal_cleanup_started_at" not in row["attributes"]
    assert _rows(caplog, "required_terminal_exited") == []
    assert launch.terminals.get(_SESSION, "claude", "main") is None


async def test_explicit_close_and_cleanup_are_distinct_and_sink_json_remains_parseable(
    launch: _Launch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    assert await launch.resources.close_terminal(_SESSION, "terminal_claude_main")
    [request] = _rows(caplog, "terminal_close_requested")
    [cleanup] = _rows(caplog, "terminal_cleanup_started")
    assert request["attributes"]["terminal_control_request_action"] == "close_terminal"
    assert "terminal_exit_observed_at" not in request["attributes"]
    assert "terminal_cleanup_started_at" not in request["attributes"]
    assert "terminal_exit_status_source" not in request["attributes"]
    assert "terminal_exit_status_process" not in request["attributes"]
    assert (
        json.loads(cleanup["attributes"]["terminal_control_requests"])[0]["action"]
        == "close_terminal"
    )
    assert _rows(caplog, "required_terminal_exited") == []

    assert json.loads(request["attributes"]["claude_lifecycle"])["session_end_reason"] == "unknown"
    assert (
        json.loads(request["attributes"]["terminal_control_requests"])[0]["action"]
        == "close_terminal"
    )
