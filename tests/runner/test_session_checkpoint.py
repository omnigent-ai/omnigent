"""Runner lifecycle tests for framework checkpoints."""

from __future__ import annotations

from typing import Any, Literal, cast

import pytest

from omnigent.runner import create_runner_app
from omnigent.runtime import telemetry
from omnigent.runtime.session_checkpoint import SessionHandover, build_checkpoint


class _Response:
    status_code = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _CheckpointServer:
    """Minimal server client that retains checkpoint writes."""

    def __init__(self, checkpoint: dict[str, Any] | None) -> None:
        self.checkpoint = checkpoint
        self.writes: list[dict[str, Any]] = []

    async def get(self, url: str, **kwargs: Any) -> _Response:
        del url, kwargs
        return _Response({"checkpoint": self.checkpoint})

    async def put(self, url: str, **kwargs: Any) -> _Response:
        del url
        self.writes.append(cast(dict[str, Any], kwargs["json"]))
        self.checkpoint = cast(dict[str, Any], kwargs["json"])["checkpoint"]
        return _Response({"checkpoint": self.checkpoint})


def _prior_history() -> list[dict[str, Any]]:
    return [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Implement it."}],
        },
        {
            "type": "function_call",
            "call_id": "commit",
            "name": "shell",
            "arguments": '{"command":"git commit -m checkpoint"}',
        },
        {"type": "function_call_output", "call_id": "commit", "output": "[branch abc1234]"},
        {
            "type": "function_call",
            "call_id": "push",
            "name": "shell",
            "arguments": '{"command":"git push origin branch"}',
        },
        {"type": "function_call_output", "call_id": "push", "output": "pushed"},
    ]


@pytest.mark.parametrize("terminal_status", ["idle", "failed", "cancelled"])
async def test_runner_persists_checkpoint_for_every_terminal_outcome(
    terminal_status: Literal["idle", "failed", "cancelled"],
) -> None:
    """A terminal runner path writes a checkpoint without replaying covered work."""
    session_id = "conv_checkpoint_lifecycle"
    prior_history = _prior_history()
    stored = build_checkpoint(session_id=session_id, history=prior_history, status="idle")
    server = _CheckpointServer(stored.model_dump(mode="json"))
    app = create_runner_app(server_client=cast(Any, server))
    current_history = [
        *prior_history,
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Just create a PR"}],
        },
    ]

    checkpoint, model_history = await app.state.checkpoint_for_turn(
        session_id,
        "openai-agents",
        current_history,
    )

    assert checkpoint is not None
    assert model_history == [current_history[-1]]
    assert server.writes[-1]["checkpoint"]["latest_user_directive"] == "Just create a PR"
    app.state.session_histories[session_id] = current_history
    app.state.checkpoint_turn_status[session_id] = terminal_status
    await app.state.persist_session_checkpoint(session_id)

    persisted = server.writes[-1]["checkpoint"]
    assert persisted["status"] == terminal_status
    assert persisted["phase"] == "open_pr"
    assert persisted["pending"] == "Create the pull request with github__create_pull_request."
    assert "Do not repeat the verified git commit." in persisted["do_not_repeat"]
    assert "Do not repeat the verified git push." in persisted["do_not_repeat"]
    app.state.session_histories.pop(session_id, None)


async def test_runner_records_checkpoint_load_and_save_as_agent_tool_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checkpoint I/O waits for the agent parent and records direct TOOL children."""
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    session_id = "conv_checkpoint_trace"
    stored = build_checkpoint(
        session_id=session_id,
        history=_prior_history(),
        status="idle",
    )
    server = _CheckpointServer(stored.model_dump(mode="json"))
    recorded: list[dict[str, Any]] = []

    def _record(tool_name: str, **kwargs: Any) -> None:
        recorded.append({"tool_name": tool_name, **kwargs})

    monkeypatch.setattr(telemetry, "record_completed_tool_call", _record)
    app = create_runner_app(server_client=cast(Any, server))
    history = [
        *_prior_history(),
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Open the PR."}],
        },
    ]

    checkpoint, _ = await app.state.checkpoint_for_turn(
        session_id,
        "openai-agents",
        history,
    )

    assert checkpoint is not None
    assert recorded == []

    parent = "00-" + "1" * 32 + "-" + "2" * 16 + "-01"
    app.state.bind_checkpoint_traceparent(session_id, parent)

    assert [call["tool_name"] for call in recorded] == [
        "session_checkpoint.load",
        "session_checkpoint.save",
    ]
    assert all(call["parent_traceparent"] == parent for call in recorded)
    assert all(call["attributes"]["session.id"] == session_id for call in recorded)

    app.state.session_histories[session_id] = history
    app.state.checkpoint_turn_status[session_id] = "idle"
    await app.state.persist_session_checkpoint(session_id)

    assert [call["tool_name"] for call in recorded] == [
        "session_checkpoint.load",
        "session_checkpoint.save",
        "session_checkpoint.save",
    ]
    assert recorded[-1]["attributes"]["checkpoint.status"] in {"idle", "complete"}
    app.state.session_histories.pop(session_id, None)


async def test_runner_records_handover_load_as_agent_tool_leaf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    session_id = "conv_handover_trace"
    handover = SessionHandover(
        original_directive="Continue the task.",
        objective="Finish the runtime change.",
        phase="validate",
        remaining_work=["Run tests."],
        next_action="Run tests.",
        context_tokens=56000,
        created_at="2026-08-10T12:00:00+00:00",
    )
    stored = build_checkpoint(
        session_id=session_id,
        history=_prior_history(),
        status="active",
        handover=handover,
        handover_count=1,
    )
    server = _CheckpointServer(stored.model_dump(mode="json"))
    recorded: list[dict[str, Any]] = []

    def _record(tool_name: str, **kwargs: Any) -> None:
        recorded.append({"tool_name": tool_name, **kwargs})

    monkeypatch.setattr(telemetry, "record_completed_tool_call", _record)
    app = create_runner_app(server_client=cast(Any, server))

    checkpoint, _ = await app.state.checkpoint_for_turn(
        session_id,
        "pi",
        _prior_history(),
    )

    assert checkpoint is not None
    parent = "00-" + "3" * 32 + "-" + "4" * 16 + "-01"
    app.state.bind_checkpoint_traceparent(session_id, parent)

    assert [call["tool_name"] for call in recorded] == [
        "session_checkpoint.load",
        "session_handover.load",
        "session_checkpoint.save",
    ]
    assert recorded[1]["attributes"]["handover.present"] is True


async def test_runner_records_handover_save_and_immediate_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    session_id = "conv_handover_rollover"
    server = _CheckpointServer(None)
    recorded: list[dict[str, Any]] = []

    def _record(tool_name: str, **kwargs: Any) -> None:
        recorded.append({"tool_name": tool_name, **kwargs})

    monkeypatch.setattr(telemetry, "record_completed_tool_call", _record)
    app = create_runner_app(server_client=cast(Any, server))
    history = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Continue the task."}],
        }
    ]
    await app.state.checkpoint_for_turn(session_id, "pi", history)
    parent = "00-" + "5" * 32 + "-" + "6" * 16 + "-01"
    app.state.bind_checkpoint_traceparent(session_id, parent)

    handover = SessionHandover(
        original_directive="Continue the task.",
        objective="Finish the runtime change.",
        phase="validate",
        remaining_work=["Run tests."],
        next_action="Run tests.",
        context_tokens=56000,
        created_at="2026-08-10T12:00:00+00:00",
    )
    await app.state.handle_harness_compaction(
        session_id,
        {
            "summary": "Structured handover",
            "total_tokens": 900,
            "handover": handover.model_dump(mode="json"),
            "handover_loaded": True,
            "compacted_messages": history,
        },
    )

    assert [call["tool_name"] for call in recorded] == [
        "session_checkpoint.load",
        "session_checkpoint.save",
        "session_handover.save",
        "session_handover.load",
    ]
    assert server.writes[-1]["checkpoint"]["handover"]["next_action"] == "Run tests."


async def test_generic_compaction_without_anchor_keeps_runner_history() -> None:
    session_id = "conv_generic_compaction"
    server = _CheckpointServer(None)
    app = create_runner_app(server_client=cast(Any, server))
    history = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Keep this history."}],
        }
    ]
    app.state.session_histories[session_id] = history

    await app.state.handle_harness_compaction(
        session_id,
        {
            "summary": "Generic summary",
            "total_tokens": 100,
        },
    )

    assert app.state.session_histories[session_id] == history
