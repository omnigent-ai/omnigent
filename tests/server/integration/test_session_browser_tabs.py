"""Integration tests for ``GET``/``PUT /v1/sessions/{id}/browser-tabs``.

Covers the HTTP contract the desktop and mobile clients rely on: per-user
tabs at a revision, 409 on a stale save, validation, the change event, and
access control. The store's own matrix lives in
``tests/stores/test_session_browser_tab_store.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runtime import session_stream
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_READ, UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

ALICE = "alice@example.com"
BOB = "bob@example.com"
DOCS = {"id": "agent-browser", "url": "https://docs.example/guide", "title": "Guide"}
BLANK = {"id": "9f0e", "url": None, "title": None}


@pytest.fixture()
def auth_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """Deployed multi-user posture: a request without an identity header gets 401."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )


def _as(user: str | None) -> dict[str, str] | None:
    return {"X-Forwarded-Email": user} if user is not None else None


async def _create_session(client: httpx.AsyncClient, user: str | None = None) -> str:
    agent = await create_test_agent(client, user=user)
    resp = await client.post("/v1/sessions", json={"agent_id": agent["id"]}, headers=_as(user))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _put(
    client: httpx.AsyncClient,
    session_id: str,
    tabs: list[dict[str, Any]],
    base_revision: int,
    user: str | None = None,
) -> httpx.Response:
    return await client.put(
        f"/v1/sessions/{session_id}/browser-tabs",
        json={"base_revision": base_revision, "tabs": tabs},
        headers=_as(user),
    )


async def _get(client: httpx.AsyncClient, session_id: str, user: str | None = None) -> Any:
    resp = await client.get(f"/v1/sessions/{session_id}/browser-tabs", headers=_as(user))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_saves_and_returns_tabs_at_a_revision(client: httpx.AsyncClient) -> None:
    session_id = await _create_session(client)
    assert await _get(client, session_id) == {"revision": 0, "tabs": []}

    saved = await _put(client, session_id, [DOCS, BLANK], 0)

    assert saved.status_code == 200, saved.text
    assert saved.json() == {"revision": 1, "tabs": [DOCS, BLANK]}
    assert await _get(client, session_id) == saved.json()


async def test_a_stale_save_is_rejected_and_leaves_the_tabs(client: httpx.AsyncClient) -> None:
    session_id = await _create_session(client)
    await _put(client, session_id, [DOCS], 0)

    stale = await _put(client, session_id, [BLANK], 0)

    assert stale.status_code == 409, stale.text
    assert await _get(client, session_id) == {"revision": 1, "tabs": [DOCS]}


@pytest.mark.parametrize(
    "tabs",
    [
        [{"id": "a", "url": "javascript:alert(1)"}],
        [{"id": "", "url": "https://x"}],
        [{"id": "a", "extra": True}],
        [{"id": f"t{index}"} for index in range(21)],
        [BLANK, BLANK],
    ],
)
async def test_rejects_invalid_tabs(client: httpx.AsyncClient, tabs: list[Any]) -> None:
    session_id = await _create_session(client)

    resp = await _put(client, session_id, tabs, 0)

    assert resp.status_code in (400, 422), resp.text
    assert (await _get(client, session_id))["revision"] == 0


async def test_only_a_changing_save_notifies_viewers(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = await _create_session(client)
    published: list[tuple[str, dict[str, Any]]] = []
    real_publish = session_stream.publish

    def record(target: str, event: dict[str, Any]) -> int:
        if event.get("type") == "session.browser_tabs.changed":
            published.append((target, event))
        return real_publish(target, event)

    monkeypatch.setattr(session_stream, "publish", record)

    await _put(client, session_id, [DOCS], 0)
    await _put(client, session_id, [DOCS], 1)

    assert [(target, event["type"], event["session_id"]) for target, event in published] == [
        (session_id, "session.browser_tabs.changed", session_id)
    ]


async def test_each_viewer_of_a_shared_session_keeps_their_own_tabs(
    auth_client: httpx.AsyncClient,
) -> None:
    session_id = await _create_session(auth_client, ALICE)
    shared = await auth_client.put(
        f"/v1/sessions/{session_id}/permissions",
        json={"user_id": BOB, "level": LEVEL_READ},
        headers=_as(ALICE),
    )
    assert shared.status_code in (200, 204), shared.text

    await _put(auth_client, session_id, [DOCS], 0, ALICE)
    bob = await _put(auth_client, session_id, [BLANK], 0, BOB)

    assert bob.status_code == 200, bob.text
    assert (await _get(auth_client, session_id, ALICE))["tabs"] == [DOCS]
    assert (await _get(auth_client, session_id, BOB))["tabs"] == [BLANK]


async def test_requires_access_to_the_session(auth_client: httpx.AsyncClient) -> None:
    session_id = await _create_session(auth_client, ALICE)

    read = await auth_client.get(f"/v1/sessions/{session_id}/browser-tabs", headers=_as(BOB))
    write = await _put(auth_client, session_id, [DOCS], 0, BOB)
    anonymous = await auth_client.get(f"/v1/sessions/{session_id}/browser-tabs")

    assert read.status_code in (403, 404), read.text
    assert write.status_code in (403, 404), write.text
    assert anonymous.status_code == 401, anonymous.text


async def test_server_info_advertises_saved_tabs(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/info")
    assert resp.status_code == 200, resp.text
    assert resp.json()["session_browser_tabs"] is True
