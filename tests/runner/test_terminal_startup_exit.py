"""Terminal startup failures preserve lifecycle evidence before registration."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from omnigent.debug_logging import record_to_row
from omnigent.inner.datamodel import TerminalEnvSpec
from omnigent.inner.terminal import TerminalCreateResult
from omnigent.runner.native import orchestration
from omnigent.runner.resource_registry import (
    CLAUDE_NATIVE_TERMINAL_ROLE,
    CODEX_NATIVE_TERMINAL_ROLE,
    PI_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
    TerminalExitEvent,
)
from omnigent.terminals import registry as terminal_registry_module
from omnigent.terminals.registry import TerminalRegistry
from tests.runner.helpers import make_test_terminal_instance


@pytest.mark.parametrize("phase", ["launch", "observe"])
@pytest.mark.parametrize("capture", ["0", "1"])
@pytest.mark.parametrize(
    "role", [CODEX_NATIVE_TERMINAL_ROLE, CLAUDE_NATIVE_TERMINAL_ROLE, PI_NATIVE_TERMINAL_ROLE]
)
async def test_dead_before_observation_records_exit_without_publishing_a_resource(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    phase: str,
    capture: str,
    role: str,
) -> None:
    monkeypatch.setenv("OMNIGENT_HARNESS_STDERR_ENABLED", capture)
    terminals = TerminalRegistry()
    resources = SessionResourceRegistry(terminal_registry=terminals)
    resources._set_session_status_memo("failed-child", "running", record_activity=True)
    resources._sync_status_edge("failed-child", "running")
    instance = make_test_terminal_instance("native", "main", tmp_path)
    instance.launch = AsyncMock()  # type: ignore[method-assign]
    secret = "private-startup-token-" * 500
    output = (
        f"Authorization: Bearer {secret}\nerror: unexpected argument '--invalid' found"
        + "\n" * 80
        + "Pane is dead (status 2, Wed Sep 23 00:00:00 2026)"
    )
    probes = 0

    async def alive() -> bool:
        nonlocal probes
        probes += 1
        if phase == "observe" and probes == 1:
            return True
        instance._remember_exit_status("1 2")
        instance._last_exit_snapshot = output
        instance._remember_pane_snapshot("visible usage hint; initial error scrolled away")
        instance.running = False
        return False

    async def close() -> None:
        assert instance.last_exit_status() == 2
        assert instance._last_exit_snapshot == output

    monkeypatch.setattr(instance, "is_alive", alive)
    close_mock = AsyncMock(side_effect=close)
    monkeypatch.setattr(instance, "close", close_mock)
    monkeypatch.setattr(
        terminal_registry_module,
        "create_terminal_instance",
        lambda *_args, **_kwargs: TerminalCreateResult(instance=instance, cwd=tmp_path),
    )
    published: list[TerminalExitEvent] = []
    resources.set_terminal_exit_publisher(published.append)
    launch = (
        resources.launch_auxiliary_terminal
        if role == CODEX_NATIVE_TERMINAL_ROLE
        else resources.launch_required_terminal
    )

    with caplog.at_level(logging.INFO, logger="omnigent.runner.resource_registry"):
        with pytest.raises(RuntimeError, match="exit status 2") as exited:
            await launch(
                "failed-child",
                "native",
                "main",
                TerminalEnvSpec(command="native"),
                resource_role=role,
            )

    records = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "terminal_exit_observed"
    ]
    assert len(records) == 1
    row = record_to_row(records[0], "runner")
    assert row["session_id"] == "failed-child"
    assert records[0].attributes["terminal_instance_id"] == instance.diagnostic_id
    assert records[0].attributes["terminal_exit_status"] == 2
    assert records[0].attributes["before_observation"] is True
    assert records[0].attributes["terminal_lifecycle"] == (
        "auxiliary" if role == CODEX_NATIVE_TERMINAL_ROLE else "required"
    )
    excerpt = records[0].attributes["terminal_last_output"]
    if role == CODEX_NATIVE_TERMINAL_ROLE and capture == "1":
        assert "unexpected argument" in excerpt
        assert "[REDACTED]" in excerpt
    else:
        assert excerpt is None
        assert "unexpected argument" not in str(row)
    assert "private-startup-token" not in str(row)
    assert published == []
    assert terminals.get("failed-child", "native", "main") is None
    assert resources.terminal_resource_role("failed-child", "terminal_native_main") is None
    assert resources._last_session_status["failed-child"] == "running"
    assert "failed-child" in resources._active_session_turns
    assert resources._published_session_status["failed-child"] == ("running", None)
    close_mock.assert_awaited_once()

    events: list[object] = []
    runtime_name = {
        CODEX_NATIVE_TERMINAL_ROLE: "Codex",
        CLAUDE_NATIVE_TERMINAL_ROLE: "Claude",
        PI_NATIVE_TERMINAL_ROLE: "Pi",
    }[role]
    error = orchestration._publish_native_terminal_start_error(
        lambda session_id, event: events.append((session_id, event)),
        "failed-child",
        runtime_name,
        exited.value,
    )
    assert events == [
        ("failed-child", {"type": "session.status", "status": "failed", "error": error})
    ]
    assert error["code"] == "native_terminal_start_failed"
    message = error["message"]
    if role == CODEX_NATIVE_TERMINAL_ROLE:
        assert "Codex terminal exited with status 2 before becoming available." in message
        assert "see the runner log" not in message
    else:
        assert f"Native {runtime_name} terminal failed to start" in message
    if role == CODEX_NATIVE_TERMINAL_ROLE and capture == "1":
        assert "unexpected argument '--invalid'" in message
        assert "[REDACTED]" in message
    else:
        assert "unexpected argument" not in message
        assert "Codex startup terminal output:" not in message
    assert "private-startup-token" not in message
    assert "thread discovery timed out" not in message
    assert len(message) < 4500
