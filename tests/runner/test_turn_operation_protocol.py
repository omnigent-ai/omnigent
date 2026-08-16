"""Runner HTTP protocol tests for replay-safe operation IDs and status."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runner import create_runner_app
from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient


def _id(seed: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _app(*, frames: list[str] | None = None) -> tuple[FastAPI, _FakeProcessManager]:
    harness = _ScriptedHarnessClient(
        frames
        or [
            _sse({"type": "response.created", "response": {"id": "resp_operation"}}),
            _sse({"type": "response.completed", "response": {"status": "completed"}}),
        ]
    )
    manager = _FakeProcessManager(harness)
    app = create_runner_app(
        process_manager=manager,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    return app, manager


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://runner")


async def _eventually(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not become true")


async def test_operation_post_exact_replay_and_terminal_status() -> None:
    app, manager = _app()
    operation_id = _id("operation")
    session_id = _id("session")
    body = {
        "type": "message",
        "role": "user",
        "content": "build it",
        "harness": "openai-agents",
        "spawn_env": {},
        "operation_id": operation_id,
    }

    async with await _client(app) as client:
        accepted = await client.post(f"/v1/sessions/{session_id}/events", json=body)
        assert accepted.status_code == 202
        assert accepted.json()["operation_id"] == operation_id
        assert accepted.json()["state"] == "running"
        incarnation = accepted.json()["runner_incarnation_id"]

        replay = await client.post(f"/v1/sessions/{session_id}/events", json=dict(body))
        assert replay.status_code == 202
        assert replay.json()["operation_id"] == operation_id
        assert "no new turn" in replay.json()["detail"]

        await _eventually(
            lambda: app.state.turn_operation_registry.get(operation_id).state == "succeeded"
        )
        status = await client.get(f"/v1/turn-operations/{operation_id}")
        assert status.status_code == 200
        assert status.json()["state"] == "succeeded"
        assert status.json()["runner_incarnation_id"] == incarnation

    assert len(manager.get_client_calls) == 1


async def test_exact_replay_does_not_repeat_forwarded_content_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.runner.app as runner_app

    app, manager = _app()
    operation_id = _id("resolve-once")
    session_id = _id("resolve-once-session")
    resolutions = 0

    async def _resolve_once(
        content: list[dict[str, Any]],
        *,
        session_id: str,
        server_client: Any,
    ) -> list[dict[str, Any]]:
        del session_id, server_client
        nonlocal resolutions
        resolutions += 1
        return content

    monkeypatch.setattr(runner_app, "_resolve_forwarded_message_content", _resolve_once)
    body = {
        "content": [{"type": "input_text", "text": "same"}],
        "harness": "openai-agents",
        "spawn_env": {},
        "operation_id": operation_id,
    }
    async with await _client(app) as client:
        first = await client.post(f"/v1/sessions/{session_id}/events", json=body)
        replay = await client.post(f"/v1/sessions/{session_id}/events", json=body)
        await _eventually(
            lambda: app.state.turn_operation_registry.get(operation_id).state == "succeeded"
        )
    assert first.status_code == 202
    assert replay.status_code == 202
    assert resolutions == 1
    assert len(manager.get_client_calls) == 1


async def test_failed_harness_stream_terminalizes_operation_as_failed() -> None:
    app, _manager = _app(
        frames=[
            _sse({"type": "response.created", "response": {"id": "resp_failed"}}),
            _sse(
                {
                    "type": "response.failed",
                    "response": {"status": "failed"},
                    "error": {"message": "executor failed", "code": "executor_error"},
                }
            ),
        ]
    )
    operation_id = _id("failed-operation")
    session_id = _id("failed-session")
    async with await _client(app) as client:
        accepted = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "content": "fail",
                "harness": "openai-agents",
                "spawn_env": {},
                "operation_id": operation_id,
            },
        )
        assert accepted.status_code == 202
        await _eventually(
            lambda: app.state.turn_operation_registry.get(operation_id).state == "failed"
        )
        status = await client.get(f"/v1/turn-operations/{operation_id}")
        assert status.status_code == 200
        assert status.json()["state"] == "failed"


async def test_deleting_live_session_terminalizes_operation_as_cancelled() -> None:
    app, _manager = _app()
    operation_id = _id("deleted-operation")
    session_id = _id("deleted-session")
    registry = app.state.turn_operation_registry
    registry.reserve(
        operation_id=operation_id,
        session_id=session_id,
        request={"content": "delete me", "conversation_id": session_id},
    )
    registry.mark_running(operation_id)
    app.state.active_operation_ids[session_id] = operation_id
    app.state.active_turns[session_id] = None

    async with await _client(app) as client:
        deleted = await client.delete(f"/v1/sessions/{session_id}")
        assert deleted.status_code == 200
        status = await client.get(f"/v1/turn-operations/{operation_id}")
        assert status.status_code == 200
        assert status.json()["state"] == "cancelled"
    assert session_id not in app.state.active_operation_ids


async def test_operation_replay_with_changed_body_or_session_conflicts() -> None:
    app, _manager = _app()
    operation_id = _id("operation")
    session_id = _id("session")
    body = {
        "content": "first",
        "harness": "openai-agents",
        "spawn_env": {},
        "operation_id": operation_id,
    }
    async with await _client(app) as client:
        assert (
            await client.post(f"/v1/sessions/{session_id}/events", json=body)
        ).status_code == 202
        changed = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={**body, "content": "changed"},
        )
        assert changed.status_code == 409
        assert changed.json()["error"] == "operation_conflict"
        rebound = await client.post(f"/v1/sessions/{_id('other')}/events", json=body)
        assert rebound.status_code == 409
        assert rebound.json()["error"] == "operation_conflict"


async def test_new_operation_is_rejected_not_buffered_while_session_busy() -> None:
    app, _manager = _app()
    operation_id = _id("operation")
    session_id = _id("session")
    app.state.active_turns[session_id] = None

    async with await _client(app) as client:
        response = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"content": "next", "operation_id": operation_id},
        )
        assert response.status_code == 409
        assert response.json()["error"] == "session_busy"
        assert app.state.turn_operation_registry.get(operation_id) is None
        assert not app.state.session_message_buffers.get(session_id)

        legacy = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"content": "legacy follow-up"},
        )
        assert legacy.status_code == 202
        assert legacy.json()["status"] == "buffered"
        assert len(app.state.session_message_buffers[session_id]) == 1


async def test_invalid_or_control_operation_request_fails_before_side_effects() -> None:
    app, manager = _app()
    session_id = _id("session")
    async with await _client(app) as client:
        invalid = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"content": "x", "operation_id": "BAD"},
        )
        assert invalid.status_code == 400
        control = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "interrupt", "operation_id": _id("operation")},
        )
        assert control.status_code == 400
        assert control.json()["error"] == "invalid_operation_request"
    assert manager.get_client_calls == []


async def test_unknown_status_is_ambiguous_and_incarnation_scoped() -> None:
    app, _manager = _app()
    operation_id = _id("unknown")
    async with await _client(app) as client:
        response = await client.get(f"/v1/turn-operations/{operation_id}")
        assert response.status_code == 404
        payload = response.json()
        assert payload["error"] == "operation_not_found"
        assert "do not infer" in payload["detail"]
        assert payload["runner_incarnation_id"] == app.state.runner_incarnation_id

        invalid = await client.get("/v1/turn-operations/not-an-operation")
        assert invalid.status_code == 400

    replacement, _replacement_manager = _app()
    assert replacement.state.runner_incarnation_id != app.state.runner_incarnation_id
