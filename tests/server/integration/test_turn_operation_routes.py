"""Public durable turn-operation route integration tests."""

from __future__ import annotations

import unittest.mock
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.entities import MessageData, NewConversationItem
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_EDIT, UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)
from omnigent.stores.turn_operation_store.sqlalchemy_store import (
    SqlAlchemyTurnOperationStore,
)

_ALICE = "alice@example.com"
_INCARNATION = "b" * 32
_EVENT = {
    "type": "message",
    "data": {
        "role": "user",
        "content": [{"type": "input_text", "text": "build it"}],
    },
}
_REQUEST = {"version": "v1alpha1", "event": _EVENT}


class _OperationRunner:
    def __init__(self) -> None:
        self.states: dict[str, tuple[str, str]] = {}
        self.posts: list[dict[str, Any]] = []

    async def get(self, path: str, **_: Any) -> httpx.Response:
        operation_id = path.rsplit("/", 1)[-1]
        state = self.states.get(operation_id)
        if state is None:
            return httpx.Response(
                404,
                json={
                    "error": "operation_not_found",
                    "runner_incarnation_id": _INCARNATION,
                },
            )
        session_id, operation_state = state
        return httpx.Response(
            200,
            json={
                "operation_id": operation_id,
                "session_id": session_id,
                "state": operation_state,
                "runner_incarnation_id": _INCARNATION,
            },
        )

    async def post(self, path: str, *, json: dict[str, Any], **_: Any) -> httpx.Response:
        operation_id = json["operation_id"]
        session_id = path.split("/")[3]
        self.posts.append(json)
        self.states[operation_id] = (session_id, "accepted")
        return httpx.Response(
            202,
            json={
                "operation_id": operation_id,
                "session_id": session_id,
                "state": "accepted",
                "runner_incarnation_id": _INCARNATION,
            },
        )


@pytest.fixture()
def turn_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        turn_operation_store=SqlAlchemyTurnOperationStore(db_uri),
    )


@pytest_asyncio.fixture()
async def turn_client(turn_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=turn_app),
        base_url="http://test",
    ) as client:
        yield client


def _seed_session(db_uri: str) -> str:
    conversation = SqlAlchemyConversationStore(db_uri).create_conversation()
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user(_ALICE)
    permissions.grant(_ALICE, conversation.id, LEVEL_EDIT)
    return conversation.id


@pytest.mark.asyncio
async def test_post_exact_replay_dispatches_and_persists_once(
    turn_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    from omnigent.server.routes import sessions as sessions_mod

    session_id = _seed_session(db_uri)
    runner = _OperationRunner()

    async def _runner(*_: Any, **__: Any) -> _OperationRunner:
        return runner

    headers = {"X-Forwarded-Email": _ALICE, "Idempotency-Key": "turn-1"}
    with unittest.mock.patch.object(sessions_mod, "_get_runner_client", _runner):
        first = await turn_client.post(
            f"/v1/sessions/{session_id}/turn-operations",
            json=_REQUEST,
            headers=headers,
        )
        replay = await turn_client.post(
            f"/v1/sessions/{session_id}/turn-operations",
            json=_REQUEST,
            headers=headers,
        )

    assert first.status_code == 202, first.text
    assert replay.status_code == 202, replay.text
    assert first.json()["state"] == "dispatched"
    assert first.json()["replayed"] is False
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["replayed"] is True
    assert len(runner.posts) == 1
    assert runner.posts[0]["operation_id"] == first.json()["id"]
    assert runner.posts[0]["persisted_item_id"] == first.json()["id"]
    items = SqlAlchemyConversationStore(db_uri).list_items(session_id).data
    assert [item.id for item in items] == [first.json()["id"]]


@pytest.mark.asyncio
async def test_replay_recovers_item_written_before_journal_advance(
    turn_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    from omnigent.server.routes import sessions as sessions_mod

    session_id = _seed_session(db_uri)
    operations = SqlAlchemyTurnOperationStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    operation, _ = operations.create_or_get(
        conversation_id=session_id,
        principal=_ALICE,
        idempotency_key="crash-window",
        request=_REQUEST,
    )
    dispatch_request = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "build it"}],
        "agent_id": None,
        "model": "",
        "has_mcp_servers": False,
        "persisted_item_id": operation.id,
    }
    operations.prepare_input(operation.id, operation.id, dispatch_request)
    conversations.append_idempotent(
        session_id,
        NewConversationItem(
            type="message",
            response_id=f"turn_{operation.id}",
            data=MessageData(
                role="user",
                content=[{"type": "input_text", "text": "build it"}],
            ),
            created_by=_ALICE,
        ),
        operation.id,
    )
    runner = _OperationRunner()

    async def _runner(*_: Any, **__: Any) -> _OperationRunner:
        return runner

    with unittest.mock.patch.object(sessions_mod, "_get_runner_client", _runner):
        response = await turn_client.post(
            f"/v1/sessions/{session_id}/turn-operations",
            json=_REQUEST,
            headers={
                "X-Forwarded-Email": _ALICE,
                "Idempotency-Key": "crash-window",
            },
        )

    assert response.status_code == 202, response.text
    assert response.json()["id"] == operation.id
    assert response.json()["state"] == "dispatched"
    assert len(conversations.list_items(session_id).data) == 1
    assert len(runner.posts) == 1


@pytest.mark.asyncio
async def test_get_reconciles_terminal_runner_status(
    turn_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    from omnigent.server.routes import sessions as sessions_mod

    session_id = _seed_session(db_uri)
    runner = _OperationRunner()

    async def _runner(*_: Any, **__: Any) -> _OperationRunner:
        return runner

    headers = {"X-Forwarded-Email": _ALICE, "Idempotency-Key": "turn-status"}
    with unittest.mock.patch.object(sessions_mod, "_get_runner_client", _runner):
        posted = await turn_client.post(
            f"/v1/sessions/{session_id}/turn-operations",
            json=_REQUEST,
            headers=headers,
        )
        operation_id = posted.json()["id"]
        runner.states[operation_id] = (session_id, "succeeded")
        status = await turn_client.get(
            f"/v1/sessions/{session_id}/turn-operations/{operation_id}",
            headers={"X-Forwarded-Email": _ALICE},
        )

    assert status.status_code == 200, status.text
    assert status.json()["state"] == "succeeded"
    assert status.json()["terminal_at"] is not None


@pytest.mark.asyncio
async def test_same_key_changed_request_conflicts(
    turn_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    from omnigent.server.routes import sessions as sessions_mod

    session_id = _seed_session(db_uri)
    runner = _OperationRunner()

    async def _runner(*_: Any, **__: Any) -> _OperationRunner:
        return runner

    headers = {"X-Forwarded-Email": _ALICE, "Idempotency-Key": "turn-conflict"}
    changed = {
        "version": "v1alpha1",
        "event": {
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "different"}],
            },
        },
    }
    with unittest.mock.patch.object(sessions_mod, "_get_runner_client", _runner):
        first = await turn_client.post(
            f"/v1/sessions/{session_id}/turn-operations",
            json=_REQUEST,
            headers=headers,
        )
        conflict = await turn_client.post(
            f"/v1/sessions/{session_id}/turn-operations",
            json=changed,
            headers=headers,
        )

    assert first.status_code == 202, first.text
    assert conflict.status_code == 409, conflict.text
    assert len(runner.posts) == 1


@pytest.mark.asyncio
async def test_turn_operation_requires_auth_and_idempotency_key(
    turn_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    session_id = _seed_session(db_uri)

    unauthenticated = await turn_client.post(
        f"/v1/sessions/{session_id}/turn-operations",
        json=_REQUEST,
        headers={"Idempotency-Key": "auth-required"},
    )
    missing_key = await turn_client.post(
        f"/v1/sessions/{session_id}/turn-operations",
        json=_REQUEST,
        headers={"X-Forwarded-Email": _ALICE},
    )

    assert unauthenticated.status_code == 401, unauthenticated.text
    assert missing_key.status_code == 422, missing_key.text
