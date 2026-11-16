"""GET /v1/sessions restricted by ``connected`` and ``host_id``."""

from __future__ import annotations

import time
import uuid

import fastapi
import httpx
import pytest

from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.server.helpers import create_test_agent


async def _ids(client: httpx.AsyncClient, query: str) -> set[str]:
    """
    List sessions with ``query`` and return their ids.

    :param client: The test client.
    :param query: The query string after ``?``.
    :returns: The listed session ids.
    """
    resp = await client.get(f"/v1/sessions?limit=1000&include_archived=true&{query}")
    assert resp.status_code == 200, resp.text
    return {session["id"] for session in resp.json()["data"]}


async def test_connected_and_host_id_restrict_the_list(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    agent = await create_test_agent(client)
    live, idle, other = [
        (await client.post("/v1/sessions", json={"agent_id": agent["id"]})).json()["id"]
        for _ in range(3)
    ]
    store = SqlAlchemyConversationStore(db_uri)
    assert store.set_runner_id(live, "runner-live")
    assert store.set_runner_id(idle, "runner-idle")
    store.touch_runner_liveness(["runner-live"], int(time.time()))
    host = uuid.uuid4().hex
    store.set_host_id(live, host, workspace="/w")

    assert {live, idle, other} <= await _ids(client, "visibility=all")
    assert await _ids(client, "visibility=all&connected=true") == {live}
    assert await _ids(client, f"visibility=all&host_id={host}") == {live}
    assert await _ids(client, f"visibility=all&host_id={uuid.uuid4().hex}") == set()


async def test_connected_keeps_a_tunnelled_runner_with_stale_last_seen(
    app: fastapi.FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = await create_test_agent(client)
    tunnelled, stale = [
        (await client.post("/v1/sessions", json={"agent_id": agent["id"]})).json()["id"]
        for _ in range(2)
    ]
    store = SqlAlchemyConversationStore(db_uri)
    assert store.set_runner_id(tunnelled, "runner-tunnelled")
    assert store.set_runner_id(stale, "runner-stale")
    store.touch_runner_liveness(["runner-tunnelled", "runner-stale"], 0)
    monkeypatch.setitem(app.state.tunnel_registry._sessions, "runner-tunnelled", object())

    assert await _ids(client, "visibility=all&connected=true") == {tunnelled}
