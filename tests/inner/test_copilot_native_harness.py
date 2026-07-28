"""Tests for the real ``copilot-native`` harness app and executor."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI

from omnigent.harness_plugins import native_agents
from omnigent.inner.copilot_native_executor import CopilotNativeExecutor
from omnigent.inner.copilot_native_harness import create_app
from omnigent.inner.executor import ExecutorConfig
from omnigent.runtime.harnesses import _HARNESS_MODULES


def test_copilot_native_harness_module_is_registered() -> None:
    """Runtime registry points at the Copilot native harness module."""
    assert _HARNESS_MODULES["copilot-native"] == "omnigent.inner.copilot_native_harness"


def test_copilot_native_harness_create_app_imports() -> None:
    """The harness module exports the required FastAPI app factory."""
    assert isinstance(create_app(), FastAPI)


def test_copilot_native_agent_metadata_is_registered() -> None:
    """The native-agent registry includes the Copilot terminal wrapper."""
    agent = next(agent for agent in native_agents() if agent.key == "copilot")
    assert agent.harness == "copilot-native"
    assert agent.terminal_name == "copilot"


@pytest.mark.asyncio
async def test_copilot_native_executor_runs_message_interrupt_cycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Copilot native injects user text, accepts steering, and interrupts."""
    monkeypatch.setenv("HARNESS_COPILOT_NATIVE_BRIDGE_DIR", str(tmp_path))
    monkeypatch.setenv("HARNESS_COPILOT_NATIVE_REQUEST_SESSION_ID", "conv_1")
    calls: list[tuple[str, str | None]] = []

    def _record_message(*args: object, **kwargs: object) -> None:
        del args
        content = kwargs.get("content")
        calls.append(("message", content if isinstance(content, str) else None))

    def _record_interrupt(*args: object, **kwargs: object) -> None:
        del args, kwargs
        calls.append(("interrupt", None))

    def _record_model(*args: object, **kwargs: object) -> None:
        del args
        model = kwargs.get("model")
        calls.append(("model", model if isinstance(model, str) else None))

    monkeypatch.setattr(
        "omnigent.inner.copilot_native_executor.inject_user_message",
        _record_message,
    )
    monkeypatch.setattr(
        "omnigent.inner.copilot_native_executor.inject_interrupt",
        _record_interrupt,
    )
    monkeypatch.setattr(
        "omnigent.inner.copilot_native_executor.inject_model_command",
        _record_model,
    )

    from omnigent.copilot_native_bridge import write_tmux_target

    write_tmux_target(tmp_path, session_id="conv_1", socket_path=tmp_path, tmux_target="pane")

    executor = CopilotNativeExecutor(bridge_dir=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(model="gpt-5.2"),
        )
    ]
    assert len(events) == 1
    assert events[0].__class__.__name__ == "TurnComplete"
    assert ("model", "gpt-5.2") in calls
    assert ("message", "hello") in calls

    assert await executor.enqueue_session_message("conv_1", "steer") is True
    assert await executor.interrupt_session("conv_1") is True
    assert ("message", "steer") in calls
    assert ("interrupt", None) in calls
    monkeypatch.setenv("HARNESS_COPILOT_NATIVE_REQUEST_SESSION_ID", "conv_2")
    mismatch = CopilotNativeExecutor(bridge_dir=tmp_path)
    assert await mismatch.enqueue_session_message("conv_2", "nope") is False
    assert await mismatch.interrupt_session("conv_2") is False
