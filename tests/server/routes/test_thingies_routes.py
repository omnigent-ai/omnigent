"""The ``/v1/thingies`` routes: feature gate, REST shapes, sharing and agent writes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runner.identity import (
    ORCHESTRATION_SOURCE_HEADER,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, UnifiedAuthProvider
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.thingy_store.local import LocalThingyStore

pytestmark = pytest.mark.asyncio

ALICE = "alice@example.com"
BOB = "bob@example.com"
_TOKEN = "runner-binding-token"
_CARDS = {"cards": {"type": "object", "properties": {"title": {"type": "string"}}}}
_ACTION = {
    "name": "run_card",
    "description": "Work on a card.",
    "params": {"type": "object", "properties": {"cardId": {"type": "string"}}},
}


@pytest.fixture()
def stores(db_uri: str, tmp_path: Path) -> SimpleNamespace:
    permissions = SqlAlchemyPermissionStore(db_uri)
    for user in (ALICE, BOB):
        permissions.ensure_user(user)
    return SimpleNamespace(
        db_uri=db_uri,
        conversations=SqlAlchemyConversationStore(db_uri),
        permissions=permissions,
        thingies=LocalThingyStore(str(tmp_path / "thingies")),
        root=tmp_path / "thingies",
    )


def _app(stores: SimpleNamespace, tmp_path: Path, *, features: str = "thingies") -> FastAPI:
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(stores.db_uri),
        file_store=SqlAlchemyFileStore(stores.db_uri),
        conversation_store=stores.conversations,
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        permission_store=stores.permissions,
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        feature_flags=resolve_feature_flags({"OMNIGENT_FEATURES": features}),
        thingy_store=stores.thingies,
    )


async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture()
async def client(stores: SimpleNamespace, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    async for value in _client(_app(stores, tmp_path)):
        yield value


def _as(user: str, **extra: str) -> dict[str, str]:
    return {"X-Forwarded-Email": user, **extra}


async def _published(client: httpx.AsyncClient, *, actions: list | None = None) -> dict:
    created = await client.post("/v1/thingies", json={"name": "Board"}, headers=_as(ALICE))
    assert created.status_code == 201, created.text
    thingy = created.json()
    revision = await client.post(
        f"/v1/thingies/{thingy['id']}/revisions",
        json={
            "html": "<main>Board</main>",
            "sdk_version": 1,
            "capabilities": ["data.read", "data.write"],
            "collections": _CARDS,
            "actions": actions or [],
        },
        headers=_as(ALICE),
    )
    assert revision.status_code == 201, revision.text
    published = await client.post(
        f"/v1/thingies/{thingy['id']}/publish",
        json={"revision_id": revision.json()["id"], "if_version": None},
        headers=_as(ALICE),
    )
    assert published.status_code == 200, published.text
    return published.json()


def _rpc(thingy: dict, method: str, revision: str | None = None, **params: Any) -> dict:
    return {
        "protocol": 1,
        "request_id": "req-1",
        "revision_id": revision or thingy["published_revision"],
        "method": f"data.v1.{method}",
        "params": {"collection": "cards", **params},
    }


async def test_a_disabled_server_hides_every_route_before_reading_the_body(
    stores: SimpleNamespace, tmp_path: Path
) -> None:
    async for client in _client(_app(stores, tmp_path, features="-thingies")):
        response = await client.post(
            "/v1/thingies",
            content="{bad",
            headers={**_as(ALICE), "Content-Type": "application/json"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"
        assert (await client.get("/v1/thingies/shared", headers=_as(ALICE))).status_code == 404
        info = (await client.get("/v1/info", headers=_as(ALICE))).json()
        assert info["features"]["thingies"] is False
        assert info["thingies"] is False
    assert not list((stores.root / "thingies").glob("*"))


async def test_requests_need_an_identity_and_a_json_body(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/thingies")).status_code == 401
    form = await client.post(
        "/v1/thingies", content="name=bad", headers={**_as(ALICE), "Content-Type": "text/plain"}
    )
    assert form.status_code == 415
    assert form.json()["error"]["code"] == "INVALID_ARGUMENT"
    info = (await client.get("/v1/info", headers=_as(ALICE))).json()
    assert info["features"]["thingies"] is True
    assert info["thingies"] is True and info["thingy_sharing"] is True


async def test_an_owner_builds_publishes_and_uses_a_thingy(client: httpx.AsyncClient) -> None:
    thingy = await _published(client, actions=[_ACTION])
    base = f"/v1/thingies/{thingy['id']}"
    assert thingy["browse_url"] == f"/thingy/{thingy['id']}"
    assert thingy["owner_id"] == ALICE and thingy["viewer_id"] == ALICE
    assert set(thingy) >= {"viewer_principal_id", "published_revision", "version", "permissions"}
    assert "workspace_path" not in thingy and "anchor_ids" not in thingy

    created = await client.post(
        f"{base}/rpc",
        json=_rpc(thingy, "create", value={"title": "Ship"}, operation_id="c1"),
        headers=_as(ALICE),
    )
    assert created.status_code == 200, created.text
    record = created.json()["result"]
    assert record["value"] == {"title": "Ship"} and record["created_by"] == ALICE
    listed = await client.post(f"{base}/rpc", json=_rpc(thingy, "list"), headers=_as(ALICE))
    assert listed.json()["result"]["items"] == [record]

    described = (await client.get(f"{base}/describe", headers=_as(ALICE))).json()
    assert described["collections"] == _CARDS and described["actions"][0]["name"] == "run_card"
    published = (await client.get(f"{base}/revisions/published", headers=_as(ALICE))).json()
    assert published["html"] == "<main>Board</main>"
    content = (await client.get(f"{base}/content", headers=_as(ALICE))).json()
    assert content["capabilities"] == ["data.read", "data.write"]

    request = {"action": "run_card", "params": {"cardId": "c1"}, "operation_id": "r1"}
    queued = await client.post(f"{base}/actions", json=request, headers=_as(ALICE))
    assert queued.status_code == 201 and queued.json()["merged"] is False
    action = queued.json()
    updated = await client.patch(
        f"{base}/actions/{action['id']}",
        json={"if_version": action["version"], "operation_id": "u1", "state": "running"},
        headers=_as(ALICE),
    )
    assert updated.status_code == 200 and updated.json()["value"]["state"] == "running"
    progress = (await client.get(f"{base}/actions/progress", headers=_as(ALICE))).json()
    assert [item["id"] for item in progress["items"]] == [action["id"]]
    assert progress["items"][0]["status"] == "pending"

    session = await client.put(f"{base}/session", json={"session_id": "s_1"}, headers=_as(ALICE))
    assert session.json()["session_id"] == "s_1"
    status = (await client.get(f"{base}/session/status", headers=_as(ALICE))).json()
    assert status == {
        "session_id": "s_1",
        "status": "unavailable",
        "reason": "session_unavailable",
    }

    comment = await client.post(
        f"{base}/comments",
        json={"body": "Use Q3", "anchor": {"text": "Ship"}, "operation_id": "k1"},
        headers=_as(ALICE),
    )
    assert comment.status_code == 201
    marked = await client.patch(
        f"{base}/comments/{comment.json()['id']}", json={"status": "addressed"}, headers=_as(ALICE)
    )
    assert marked.json()["status"] == "addressed"

    pinned = await client.put(f"{base}/pin", json={"pinned": True}, headers=_as(ALICE))
    assert pinned.json() == {"pinned": [{"id": thingy["id"], "name": "Board"}], "has_owned": True}
    linked = await client.put(f"{base}/sessions/S_one", json={"linked": True}, headers=_as(ALICE))
    assert [item["id"] for item in linked.json()["items"]] == [thingy["id"]]
    shown = await client.get("/v1/thingies/sessions/S_one", headers=_as(ALICE))
    assert [item["id"] for item in shown.json()["items"]] == [thingy["id"]]

    renamed = await client.patch(base, json={"name": "Launch"}, headers=_as(ALICE))
    assert renamed.json()["name"] == "Launch"
    assert [
        item["name"]
        for item in (await client.get("/v1/thingies", headers=_as(ALICE))).json()["items"]
    ] == ["Launch"]

    deleted = await client.delete(base, headers=_as(ALICE))
    assert deleted.json() == {"id": thingy["id"], "deleted": True}
    missing = await client.get(base, headers=_as(ALICE))
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "NOT_FOUND"


async def test_the_frame_learns_when_the_published_revision_changed(
    client: httpx.AsyncClient,
) -> None:
    thingy = await _published(client)
    base = f"/v1/thingies/{thingy['id']}"
    revision = await client.post(
        f"{base}/revisions",
        json={
            "html": "<main>v2</main>",
            "sdk_version": 1,
            "capabilities": ["data.read"],
            "collections": _CARDS,
        },
        headers=_as(ALICE),
    )
    newer = await client.post(
        f"{base}/publish",
        json={"revision_id": revision.json()["id"], "if_version": thingy["version"]},
        headers=_as(ALICE),
    )
    stale = await client.post(f"{base}/rpc", json=_rpc(thingy, "get", id="x"), headers=_as(ALICE))
    assert stale.status_code == 409
    assert stale.json()["error"]["request_id"] == "req-1"
    assert stale.json()["error"]["details"] == {
        "reason": "REVISION_CHANGED",
        "published_revision": newer.json()["published_revision"],
    }


async def test_sharing_follows_the_grant_levels(client: httpx.AsyncClient) -> None:
    thingy = await _published(client)
    base = f"/v1/thingies/{thingy['id']}"
    assert (await client.get(base, headers=_as(BOB))).status_code == 404
    assert (await client.get("/v1/thingies/shared", headers=_as(BOB))).json() == {"items": []}

    granted = await client.put(
        f"{base}/permissions", json={"user_id": BOB, "level": 1}, headers=_as(ALICE)
    )
    assert granted.status_code == 200, granted.text
    assert granted.json() == {
        "user_id": BOB,
        "thingy_id": thingy["id"],
        "level": 1,
        "display_name": BOB,
    }
    seen = (await client.get(base, headers=_as(BOB))).json()
    assert seen["permissions"] == {
        "read": True,
        "write_data": False,
        "publish": False,
        "manage": False,
    }
    shared = (await client.get("/v1/thingies/shared", headers=_as(BOB))).json()["items"]
    assert [(item["id"], item["shared_via"], item["owner_email"]) for item in shared] == [
        (thingy["id"], "user", ALICE)
    ]
    write = await client.post(
        f"{base}/rpc",
        json=_rpc(thingy, "create", value={"title": "x"}, operation_id="b1"),
        headers=_as(BOB),
    )
    assert write.status_code == 403 and write.json()["error"]["code"] == "PERMISSION_DENIED"
    assert (await client.get(f"{base}/permissions", headers=_as(BOB))).status_code == 403
    # Viewers do not get data.write in the content they load.
    assert (await client.get(f"{base}/content", headers=_as(BOB))).json()["capabilities"] == [
        "data.read"
    ]

    await client.put(f"{base}/permissions", json={"user_id": BOB, "level": 2}, headers=_as(ALICE))
    renamed = await client.patch(base, json={"name": "Ours"}, headers=_as(BOB))
    assert renamed.status_code == 200 and renamed.json()["permissions"]["manage"] is False
    assert (await client.delete(base, headers=_as(BOB))).status_code == 403

    kept = await client.put(
        f"{base}/permissions",
        json={"user_id": BOB, "level": 1, "ensure_minimum": True},
        headers=_as(ALICE),
    )
    assert kept.json()["level"] == 2
    await client.put(f"{base}/permissions", json={"user_id": BOB, "level": 3}, headers=_as(ALICE))
    listed = (await client.get(f"{base}/permissions", headers=_as(BOB))).json()
    assert listed == {
        "permissions": [
            {"user_id": BOB, "thingy_id": thingy["id"], "level": 3, "display_name": BOB}
        ],
        "next_cursor": None,
    }
    revoked = await client.delete(f"{base}/permissions/{BOB}", headers=_as(ALICE))
    assert revoked.status_code == 204
    assert (
        await client.delete(f"{base}/permissions/{BOB}", headers=_as(ALICE))
    ).status_code == 204
    assert (await client.get(base, headers=_as(BOB))).status_code == 404


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"user_id": ALICE, "level": 1}, 400),
        ({"user_id": "__public__", "level": 1}, 400),
        ({"user_id": "local", "level": 1}, 400),
        ({"user_id": " ", "level": 1}, 400),
        ({"user_id": BOB, "level": 4}, 400),
    ],
)
async def test_grants_refuse_the_owner_the_public_and_unknown_levels(
    client: httpx.AsyncClient, body: dict, status: int
) -> None:
    thingy = await _published(client)
    response = await client.put(
        f"/v1/thingies/{thingy['id']}/permissions", json=body, headers=_as(ALICE)
    )
    assert response.status_code == status, response.text


async def test_agents_cannot_change_sharing(client: httpx.AsyncClient) -> None:
    thingy = await _published(client)
    response = await client.put(
        f"/v1/thingies/{thingy['id']}/permissions",
        json={"user_id": BOB, "level": 1},
        headers=_as(ALICE, **{ORCHESTRATION_SOURCE_HEADER: "a" * 32}),
    )
    assert response.status_code == 403


async def test_an_agent_writes_only_from_a_private_session(
    client: httpx.AsyncClient, stores: SimpleNamespace
) -> None:
    session = stores.conversations.create_conversation(runner_id=token_bound_runner_id(_TOKEN))
    stores.permissions.grant(ALICE, session.id, LEVEL_OWNER)
    agent = _as(
        ALICE, **{ORCHESTRATION_SOURCE_HEADER: session.id, RUNNER_TUNNEL_TOKEN_HEADER: _TOKEN}
    )
    created = await client.post("/v1/thingies", json={"name": "From agent"}, headers=agent)
    assert created.status_code == 201, created.text

    stores.permissions.grant(BOB, session.id, LEVEL_EDIT)
    refused = await client.post("/v1/thingies", json={"name": "Again"}, headers=agent)
    assert refused.status_code == 403
    # Reads stay open to the agent.
    listed = await client.get("/v1/thingies", headers=agent)
    assert [item["name"] for item in listed.json()["items"]] == ["From agent"]


async def test_deleting_a_thingy_deletes_who_it_was_shared_with(
    client: httpx.AsyncClient, stores: SimpleNamespace
) -> None:
    thingy = await _published(client)
    await client.put(
        f"/v1/thingies/{thingy['id']}/permissions",
        json={"user_id": BOB, "level": 1},
        headers=_as(ALICE),
    )
    await client.delete(f"/v1/thingies/{thingy['id']}", headers=_as(ALICE))
    assert stores.permissions.list_for_thingy(thingy["id"]) == ([], None)
