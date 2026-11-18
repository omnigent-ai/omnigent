"""UI file requests are authorized, transient and scoped to the calling session."""

import asyncio
from contextlib import aclosing

import httpx
import pytest

from omnigent.runner.tool_dispatch import execute_tool
from omnigent.runtime import session_stream
from omnigent.server.auth import LEVEL_READ
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def test_tool_delivers_to_all_current_streams_without_replay(
    client: httpx.AsyncClient,
) -> None:
    agent = await create_test_agent(client)
    created = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert created.status_code == 201
    session_id = created.json()["id"]
    url = f"/v1/sessions/{session_id}/ui/open-file"
    response = await client.post(url, json={"path": "README.md"})
    assert response.status_code == 202
    assert response.json() == {"subscribers": 0}

    async with (
        aclosing(session_stream.subscribe(session_id, ready_event={"type": "ready"})) as first,
        aclosing(session_stream.subscribe(session_id, ready_event={"type": "ready"})) as second,
        aclosing(
            session_stream.subscribe("unrelated", ready_event={"type": "ready"})
        ) as unrelated,
    ):
        for stream in (first, second, unrelated):
            assert await anext(stream) == {"type": "ready"}
        result = await execute_tool(
            tool_name="ui_open_file",
            arguments='{"path": "./docs/guide.md"}',
            server_client=client,
            conversation_id=session_id,
        )
        assert result == '{"subscribers": 2}'
        for stream in (first, second):
            event = await asyncio.wait_for(anext(stream), timeout=1)
            assert event["type"] == "ui.open_file"
            assert event["path"] == "docs/guide.md", "old requests must not be replayed"
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(unrelated), timeout=0.05)


async def test_open_file_requires_session_edit_access(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    owner = {"X-Forwarded-Email": "alice@example.com"}
    agent = await create_test_agent(auth_client, user="alice@example.com")
    created = await auth_client.post("/v1/sessions", json={"agent_id": agent["id"]}, headers=owner)
    assert created.status_code == 201
    session_id = created.json()["id"]
    SqlAlchemyPermissionStore(db_uri).grant("reader@example.com", session_id, LEVEL_READ)
    for user in ("reader@example.com", "stranger@example.com"):
        response = await auth_client.post(
            f"/v1/sessions/{session_id}/ui/open-file",
            json={"path": "README.md"},
            headers={"X-Forwarded-Email": user},
        )
        assert response.status_code in (403, 404)
    response = await auth_client.post(
        f"/v1/sessions/{session_id}/ui/open-file", json={"path": "README.md"}, headers=owner
    )
    assert response.status_code == 202
    missing = await auth_client.post(
        "/v1/sessions/missing/ui/open-file", json={"path": "README.md"}, headers=owner
    )
    assert missing.status_code == 404


@pytest.mark.parametrize(
    "body", [{"path": "../secret"}, {}, {"path": 12}, {"path": "x", "session_id": "other"}]
)
async def test_open_file_validates_http_requests(client: httpx.AsyncClient, body: dict) -> None:
    response = await client.post("/v1/sessions/unused/ui/open-file", json=body)
    assert response.status_code == 422
