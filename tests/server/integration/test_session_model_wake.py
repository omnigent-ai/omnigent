"""Explicit native model changes recover sleeping host-bound runners."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.entities import Conversation, ErrorData
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes._sessions.helpers import (
    _NativeTerminalEnsureOutcome,
    _RunnerForwardResult,
)
from omnigent.server.routes.sessions import routes_core
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def model_session(
    client: httpx.AsyncClient, db_uri: str
) -> tuple[str, SqlAlchemyConversationStore]:
    agent = await create_test_agent(client)
    response = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "labels": {"omnigent.wrapper": "codex-native-ui"}},
    )
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    host = HostStore(db_uri).upsert_on_connect(
        "6b9c07bfb42f687d53af44f018adebec", "laptop", "owner@example.com"
    )
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(session_id, host_id=host.host_id, workspace="/tmp/model-switch")
    store.replace_runner_id(session_id, "runner_old")
    store.update_conversation(session_id, model_override="gpt-5.6-luna")
    return session_id, store


@pytest.fixture
def controls(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    mocks = {
        "_get_runner_client": AsyncMock(return_value=None),
        "ensure_runner_connected": AsyncMock(),
        "_ensure_runner_session_initialized": AsyncMock(return_value=True),
        "_ensure_native_terminal_ready": AsyncMock(
            return_value=_NativeTerminalEnsureOutcome(error=None)
        ),
        "_ensure_runner_relay_ready": AsyncMock(),
        "_forward_session_change_to_runner": AsyncMock(
            return_value=_RunnerForwardResult(status_code=204, body="")
        ),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(routes_core, name, mock)
    return mocks


@pytest.mark.parametrize("terminal_ready", [True, False])
async def test_model_switch_wakes_and_initializes_before_forwarding(
    client: httpx.AsyncClient,
    model_session: tuple[str, SqlAlchemyConversationStore],
    controls: dict[str, AsyncMock],
    terminal_ready: bool,
) -> None:
    session_id, store = model_session
    runner = object()
    order: list[str] = []

    async def wake(**kwargs: Any) -> tuple[object, Conversation]:
        assert kwargs["conv"].runner_id == "runner_old"
        assert kwargs["raise_host_refusal"] is True
        order.append("wake")
        return runner, store.replace_runner_id(session_id, "runner_new")

    async def initialize(*args: Any, **kwargs: Any) -> bool:
        assert args[1].runner_id == "runner_new"
        assert args[2] is runner
        assert kwargs["suppress_recovery_turn"] is True
        assert kwargs["require_success"] is True
        assert store.get_conversation(session_id).model_override == "gpt-5.6-luna"
        order.append("initialize")
        return terminal_ready

    async def forward(*args: Any, **kwargs: Any) -> _RunnerForwardResult:
        assert order == ["wake", "initialize"]
        assert args[2] == {"type": "model_change", "model": "gpt-5.6-sol"}
        assert store.get_conversation(session_id).model_override == "gpt-5.6-sol"
        order.append("forward")
        return _RunnerForwardResult(status_code=204, body="")

    controls["ensure_runner_connected"].side_effect = wake
    controls["_ensure_runner_session_initialized"].side_effect = initialize
    controls["_forward_session_change_to_runner"].side_effect = forward

    response = await client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": "gpt-5.6-sol"}
    )

    assert response.status_code == 200, response.text
    assert order == ["wake", "initialize", "forward"]
    assert response.json()["model_override"] == "gpt-5.6-sol"
    controls["_ensure_runner_relay_ready"].assert_awaited_once()
    terminal = controls["_ensure_native_terminal_ready"]
    assert terminal.await_count == (0 if terminal_ready else 1)
    if not terminal_ready:
        assert terminal.call_args.kwargs["persist_resource_event"] is False
    items = await client.get(f"/v1/sessions/{session_id}/items")
    assert not any(item["type"] == "message" for item in items.json()["data"])


@pytest.mark.parametrize(
    "failure", ["offline", "launch", "initialize", "terminal", "forward", "refused"]
)
async def test_failed_model_switch_keeps_previous_selection(
    client: httpx.AsyncClient,
    model_session: tuple[str, SqlAlchemyConversationStore],
    controls: dict[str, AsyncMock],
    failure: str,
) -> None:
    session_id, store = model_session
    conv = store.get_conversation(session_id)
    controls["ensure_runner_connected"].return_value = (object(), conv)
    error = OmnigentError("Runner is unavailable", code=ErrorCode.RUNNER_UNAVAILABLE)
    if failure == "offline":
        controls["ensure_runner_connected"].return_value = (None, conv)
    elif failure == "launch":
        controls["ensure_runner_connected"].side_effect = error
    elif failure == "initialize":
        controls["_ensure_runner_session_initialized"].side_effect = error
    elif failure == "terminal":
        controls["_ensure_runner_session_initialized"].return_value = False
        controls["_ensure_native_terminal_ready"].return_value = _NativeTerminalEnsureOutcome(
            error=ErrorData(
                source="execution",
                code="native_terminal_start_failed",
                message="Codex terminal failed to start",
            )
        )
    elif failure == "forward":
        controls["_forward_session_change_to_runner"].return_value = None
    else:
        controls["_forward_session_change_to_runner"].return_value = _RunnerForwardResult(
            status_code=503, body='{"detail": "Codex bridge unavailable"}'
        )

    response = await client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": "gpt-5.6-sol"}
    )

    assert response.status_code == 503, response.text
    assert store.get_conversation(session_id).model_override == "gpt-5.6-luna"
    if failure not in ("forward", "refused"):
        controls["_forward_session_change_to_runner"].assert_not_awaited()


async def test_connected_model_switch_does_not_reinitialize(
    client: httpx.AsyncClient,
    model_session: tuple[str, SqlAlchemyConversationStore],
    controls: dict[str, AsyncMock],
) -> None:
    session_id, _store = model_session
    controls["_get_runner_client"].return_value = object()
    response = await client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": "gpt-5.6-sol"}
    )
    assert response.status_code == 200, response.text
    controls["ensure_runner_connected"].assert_not_awaited()
    controls["_ensure_runner_session_initialized"].assert_not_awaited()
    controls["_forward_session_change_to_runner"].assert_awaited_once()


@pytest.mark.parametrize(
    "patch",
    [{"model_override": "gpt-5.6-sol", "silent": True}, {"title": "Renamed"}],
)
async def test_metadata_patch_does_not_wake_runner(
    client: httpx.AsyncClient,
    model_session: tuple[str, SqlAlchemyConversationStore],
    controls: dict[str, AsyncMock],
    patch: dict[str, Any],
) -> None:
    session_id, _store = model_session
    response = await client.patch(f"/v1/sessions/{session_id}", json=patch)
    assert response.status_code == 200, response.text
    controls["ensure_runner_connected"].assert_not_awaited()
    controls["_forward_session_change_to_runner"].assert_not_awaited()


async def test_invalid_model_does_not_wake_runner(
    client: httpx.AsyncClient,
    model_session: tuple[str, SqlAlchemyConversationStore],
    controls: dict[str, AsyncMock],
) -> None:
    session_id, store = model_session
    response = await client.patch(f"/v1/sessions/{session_id}", json={"model_override": " "})
    assert response.status_code == 400, response.text
    assert store.get_conversation(session_id).model_override == "gpt-5.6-luna"
    controls["ensure_runner_connected"].assert_not_awaited()
    controls["_forward_session_change_to_runner"].assert_not_awaited()
