"""Tests for ``GET /v1/sessions/{id}/claude_goal``.

The route forwards a read to the session's runner and reports any reply it
cannot trust as unavailable, so the browser keeps its last snapshot.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE,
    CODEX_NATIVE_WRAPPER_VALUE,
    WRAPPER_LABEL_KEY,
)
from omnigent.errors import ErrorCode, OmnigentError, WrongReplicaError
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.claude import sessions as claude
from omnigent.server.routes.sessions import _RunnerForwardResult
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import create_session_for_agent, create_test_agent

UNAVAILABLE = {"goal": None, "available": False}
GOAL = {
    "condition": "All tests pass",
    "status": "paused",
    "detail": "Goal paused · goal checks kept finding it unmet this turn",
    "iterations": None,
    "tokens": None,
    "duration_ms": None,
    "set_at": 1.5,
    "updated_at": 2.5,
}


async def _session(client: httpx.AsyncClient, db_uri: str, wrapper: str) -> str:
    agent = await create_test_agent(client, name="claude-goal-agent")
    session_id = await create_session_for_agent(client, agent["id"])
    SqlAlchemyConversationStore(db_uri).set_labels(session_id, {WRAPPER_LABEL_KEY: wrapper})
    return session_id


class _StagedForward:
    """Replace the runner forward with a canned result and record its events."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, result: object) -> None:
        self.events: list[dict[str, Any]] = []

        async def _forward(
            session_id: str, runner_router: object, event: dict[str, Any]
        ) -> _RunnerForwardResult | None:
            self.events.append(event)
            if isinstance(result, Exception):
                raise result
            return result  # type: ignore[return-value]

        monkeypatch.setattr(claude, "_forward_session_change_to_runner", _forward)


def _result(status: int, body: object) -> _RunnerForwardResult:
    return _RunnerForwardResult(
        status_code=status, body=body if isinstance(body, str) else json.dumps(body)
    )


async def test_returns_the_runner_goal(
    client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = await _session(client, db_uri, CLAUDE_NATIVE_WRAPPER_VALUE)
    staged = _StagedForward(monkeypatch, _result(200, {"goal": GOAL, "available": True}))

    response = await client.get(f"/v1/sessions/{session_id}/claude_goal")

    assert response.status_code == 200, response.text
    assert response.json() == {"goal": GOAL, "available": True}
    assert staged.events == [{"type": "claude_goal_get"}]


@pytest.mark.parametrize(
    "result",
    [
        None,
        OmnigentError("runner offline", code=ErrorCode.RUNNER_UNAVAILABLE),
        _result(404, {"error": "unknown event"}),
        _result(200, "not json"),
        _result(200, {"goal": None}),
        _result(200, {"goal": GOAL, "available": False}),
        _result(200, {"goal": {**GOAL, "status": "unknown"}, "available": True}),
    ],
    ids=[
        "no-runner",
        "forward-error",
        "older-runner",
        "malformed",
        "missing-availability",
        "goal-while-unavailable",
        "unknown-status",
    ],
)
async def test_untrusted_runner_replies_read_as_unavailable(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    result: object,
) -> None:
    session_id = await _session(client, db_uri, CLAUDE_NATIVE_WRAPPER_VALUE)
    _StagedForward(monkeypatch, result)

    response = await client.get(f"/v1/sessions/{session_id}/claude_goal")

    assert response.status_code == 200, response.text
    assert response.json() == UNAVAILABLE


async def test_rejects_sessions_that_are_not_claude_native(
    client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = await _session(client, db_uri, CODEX_NATIVE_WRAPPER_VALUE)
    staged = _StagedForward(monkeypatch, None)

    response = await client.get(f"/v1/sessions/{session_id}/claude_goal")

    assert response.status_code == 400, response.text
    assert staged.events == []


async def test_unknown_session_is_not_found(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/sessions/conv_missing/claude_goal")

    assert response.status_code == 404, response.text


@pytest.mark.parametrize(("host_online", "expected_code"), [(True, 400), (False, 200)])
async def test_a_runner_on_another_replica_is_re_addressed_not_unavailable(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    host_online: bool,
    expected_code: int,
) -> None:
    session_id = await _session(client, db_uri, CLAUDE_NATIVE_WRAPPER_VALUE)
    host_id = "host_0123456789abcdef0123456789abcdef"
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(host_id, "laptop", "alice@example.com")
    if not host_online:
        hosts.set_offline(host_id)
    SqlAlchemyConversationStore(db_uri).set_host_id(session_id, host_id, workspace="/workspace")
    # The host has no tunnel on this replica's registry.
    app.state.host_registry = HostRegistry()
    app.state.host_store = hosts

    response = await client.get(f"/v1/sessions/{session_id}/claude_goal")

    assert response.status_code == expected_code, response.text
    if host_online:
        assert response.json()["error"]["code"] == ErrorCode.WRONG_REPLICA
    else:
        assert response.json() == UNAVAILABLE


async def test_a_bound_runner_on_another_replica_is_re_addressed(
    client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = await _session(client, db_uri, CLAUDE_NATIVE_WRAPPER_VALUE)
    _StagedForward(
        monkeypatch,
        WrongReplicaError(
            "runner is on another replica", host_id="host_0123456789abcdef0123456789abcdef"
        ),
    )

    response = await client.get(f"/v1/sessions/{session_id}/claude_goal")

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == ErrorCode.WRONG_REPLICA
