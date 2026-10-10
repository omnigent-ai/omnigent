"""Host-scoped session lists preserve permissions and pagination."""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture
def api(db_uri: str):
    store = SqlAlchemyConversationStore(db_uri)
    agents = SqlAlchemyAgentStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    agent_id = uuid4().hex
    agents.create(agent_id=agent_id, name="test-agent", bundle_location=f"{agent_id}/bundle")
    for user in (ALICE, BOB):
        permissions.ensure_user(user)
    host_id = uuid4().hex
    ids = {"host": host_id, "agent": agent_id}
    for index, name in enumerate(("owned", "other_host", "unbound", "shared", "private"), start=1):
        conv = store.create_conversation(
            conversation_id=UUID(int=index).hex,
            title=name,
            agent_id=agent_id,
            host_id=None
            if name == "unbound"
            else uuid4().hex
            if name == "other_host"
            else host_id,
            workspace="/workspace" if name != "unbound" else None,
        )
        ids[name] = conv.id
        permissions.grant(BOB if name in ("shared", "private") else ALICE, conv.id, LEVEL_OWNER)
        if name == "shared":
            permissions.grant(ALICE, conv.id, LEVEL_READ)
    app = FastAPI()
    app.include_router(
        create_sessions_router(
            conversation_store=store,
            agent_store=agents,
            permission_store=permissions,
            auth_provider=UnifiedAuthProvider(source="header"),
        ),
        prefix="/v1",
    )
    with TestClient(app, headers={"X-Forwarded-Email": ALICE}) as client:
        yield client, ids


@pytest.mark.parametrize("dashed", [False, True])
@pytest.mark.parametrize("visibility", ["all", "mine", "shared"])
def test_host_filter_preserves_visibility(
    api, dashed: bool, visibility: Literal["all", "mine", "shared"]
) -> None:
    client, ids = api
    response = client.get(
        "/v1/sessions",
        params={
            "host_id": str(UUID(ids["host"])) if dashed else ids["host"],
            "visibility": visibility,
        },
    )
    assert response.status_code == 200
    expected = {
        "all": {ids["owned"], ids["shared"]},
        "mine": {ids["owned"]},
        "shared": {ids["shared"]},
    }
    assert {row["id"] for row in response.json()["data"]} == expected[visibility]
    # There is no registered/live host: persistence, not liveness, defines the filter.
    assert all(row["host_id"] == ids["host"] for row in response.json()["data"])


def test_omitted_host_filter_includes_unbound_sessions(api) -> None:
    client, ids = api
    response = client.get("/v1/sessions")
    assert response.status_code == 200
    assert {row["id"] for row in response.json()["data"]} == {
        ids["owned"],
        ids["other_host"],
        ids["unbound"],
        ids["shared"],
    }


def test_unknown_host_returns_empty_page(api) -> None:
    client, _ = api
    response = client.get("/v1/sessions", params={"host_id": uuid4().hex})
    assert response.status_code == 200
    page = response.json()
    assert page["data"] == []
    assert page["has_more"] is False
    assert page["first_id"] is None
    assert page["last_id"] is None


@pytest.mark.parametrize("host_id", ["", "not-a-uuid", "abcd"])
def test_invalid_host_rejected(api, host_id: str) -> None:
    client, _ = api
    response = client.get("/v1/sessions", params={"host_id": host_id})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "host_id"]


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_host_filter_pagination_and_agent_filter(api, order: str) -> None:
    client, ids = api
    params = {
        "host_id": ids["host"],
        "agent_id": ids["agent"],
        "agent_name": "test-agent",
        "limit": 1,
        "order": order,
    }
    matching = [ids["owned"], ids["shared"]]
    if order == "desc":
        matching.reverse()
    first = client.get("/v1/sessions", params=params)
    assert first.status_code == 200
    page = first.json()
    assert [row["id"] for row in page["data"]] == matching[:1]
    assert page["first_id"] == page["last_id"] == matching[0]
    assert page["has_more"] is True

    second = client.get("/v1/sessions", params={**params, "after": page["last_id"]})
    assert second.status_code == 200
    page = second.json()
    assert [row["id"] for row in page["data"]] == matching[1:]
    assert page["has_more"] is False
    previous = client.get("/v1/sessions", params={**params, "before": page["first_id"]})
    assert previous.status_code == 200
    assert [row["id"] for row in previous.json()["data"]] == matching[:1]
