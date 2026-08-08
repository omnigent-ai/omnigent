"""Runner lifecycle tests for framework checkpoints."""

from __future__ import annotations

from typing import Any, Literal, cast

import pytest

from omnigent.runner import create_runner_app
from omnigent.runtime.session_checkpoint import build_checkpoint


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
