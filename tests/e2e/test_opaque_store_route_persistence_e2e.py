"""Conversation items must persist when the store's search-text seam returns None.

On the reported build the conversation store holds item ``data`` opaquely, so
``SqlAlchemyConversationStore._item_search_text`` returns ``None`` (its documented
seam) and ``append()`` drops ``search_text`` from the INSERT. The mainline schema
declares that column NOT NULL, so every persist aborts with
``NOT NULL constraint failed: conversation_items.search_text``: the first user
message posted to ``POST /v1/sessions/{id}/events`` returns HTTP 500 and the runner
relay silently loses a ``session.resource.deleted`` terminal teardown event.

The default store never returns ``None`` from the seam, so a minimal subclass stands
in for the deployed store, wired into the real app and the real relay loop.

Usage::

    pytest tests/e2e/test_opaque_store_route_persistence_e2e.py -v
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.entities import NewConversationItem
from omnigent.runtime import init as init_runtime
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.routes._sessions import orchestration
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from tests.server.helpers import create_test_session, echo_runner_client

_NOT_NULL_ERROR = "NOT NULL constraint failed: conversation_items.search_text"
_RELAY_FAILURE = "Relay persist failed for session="

_USER_MESSAGE_EVENT = {
    "type": "message",
    "data": {
        "role": "user",
        "content": [{"type": "input_text", "text": "reply with exactly: LOCAL_OK"}],
    },
}
_TERMINAL_TEARDOWN_EVENT = {
    "type": "session.resource.deleted",
    "resource_id": "terminal_claude_main",
    "resource_type": "terminal",
}


class _OpaqueStore(SqlAlchemyConversationStore):
    """Stand-in for a deployed store whose item data has no plaintext body to index."""

    def _item_search_text(self, item: NewConversationItem) -> str | None:
        return None


@pytest.fixture()
def opaque_store(db_uri: str) -> _OpaqueStore:
    return _OpaqueStore(db_uri)


@pytest.fixture()
def app(db_uri: str, tmp_path: Path, opaque_store: _OpaqueStore) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_store = SqlAlchemyAgentStore(db_uri)
    file_store = SqlAlchemyFileStore(db_uri)
    agent_cache = AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache")
    init_runtime(
        conversation_store=opaque_store,
        agent_store=agent_store,
        agent_cache=agent_cache,
        file_store=file_store,
        artifact_store=artifact_store,
    )
    return create_app(
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=opaque_store,
        artifact_store=artifact_store,
        agent_cache=agent_cache,
    )


@pytest_asyncio.fixture()
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def test_first_user_message_persists_through_opaque_store(
    client: httpx.AsyncClient,
    opaque_store: _OpaqueStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = await create_test_session(client, name="opaque-store")
    session_id = session["id"]
    fake_runner = echo_runner_client()

    async def _get_runner_client(_session_id: str, _runner_router: object) -> httpx.AsyncClient:
        return fake_runner

    monkeypatch.setattr("omnigent.server.routes.sessions._get_runner_client", _get_runner_client)
    try:
        with caplog.at_level(logging.ERROR, logger="omnigent.server.app"):
            response = await client.post(
                f"/v1/sessions/{session_id}/events", json=_USER_MESSAGE_EVENT
            )
    finally:
        await fake_runner.aclose()

    db_errors = [
        record.getMessage().splitlines()[0]
        for record in caplog.records
        if _NOT_NULL_ERROR in record.getMessage()
    ]
    assert response.status_code == 202, (
        f"bug is live: POST /events returned {response.status_code} {response.text}; "
        f"server logged {db_errors}"
    )
    items = opaque_store.list_items(session_id, limit=50).data
    assert [item.type for item in items] == ["message"], (
        "the first user message did not persist through a search_text-less store"
    )


async def test_relay_persists_terminal_teardown_event(
    client: httpx.AsyncClient,
    opaque_store: _OpaqueStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = await create_test_session(client, name="opaque-store")
    session_id = session["id"]
    frames = [{"type": "session.heartbeat"}, _TERMINAL_TEARDOWN_EVENT]
    sse_body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n"

    async with httpx.AsyncClient(
        base_url="http://runner",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=sse_body)),
    ) as runner:
        with caplog.at_level(logging.ERROR, logger="omnigent.server.routes.sessions"):
            await asyncio.wait_for(
                orchestration._relay_runner_stream_once(session_id, runner, opaque_store),
                timeout=10,
            )

    relay_failures = [
        record.getMessage() for record in caplog.records if _RELAY_FAILURE in record.getMessage()
    ]
    items = opaque_store.list_items(session_id, limit=50).data
    assert not relay_failures, f"bug is live: the relay swallowed {relay_failures}"
    assert [item.type for item in items] == ["resource_event"], (
        "the session.resource.deleted event did not persist through a search_text-less store"
    )
