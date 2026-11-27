"""The ``/v1/gadgets`` routes: feature gate, REST shapes, sharing and agent writes."""

from __future__ import annotations

import json
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
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.gadget_store.local import LocalGadgetStore
from tests.gadgets.grant_store import GadgetGrantStore

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
    permissions = GadgetGrantStore(db_uri)
    for user in (ALICE, BOB):
        permissions.ensure_user(user)
    return SimpleNamespace(
        db_uri=db_uri,
        conversations=SqlAlchemyConversationStore(db_uri),
        permissions=permissions,
        gadgets=LocalGadgetStore(str(tmp_path / "gadgets")),
        root=tmp_path / "gadgets",
    )


def _app(
    stores: SimpleNamespace, tmp_path: Path, *, features: str = "gadgets", with_store: bool = True
) -> FastAPI:
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
        gadget_store=stores.gadgets if with_store else None,
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
    created = await client.post("/v1/gadgets", json={"name": "Board"}, headers=_as(ALICE))
    assert created.status_code == 201, created.text
    gadget = created.json()
    revision = await client.post(
        f"/v1/gadgets/{gadget['id']}/revisions",
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
        f"/v1/gadgets/{gadget['id']}/publish",
        json={"revision_id": revision.json()["id"], "if_version": None},
        headers=_as(ALICE),
    )
    assert published.status_code == 200, published.text
    return published.json()


def _rpc(gadget: dict, method: str, revision: str | None = None, **params: Any) -> dict:
    return {
        "protocol": 1,
        "request_id": "req-1",
        "revision_id": revision or gadget["published_revision"],
        "method": f"data.v1.{method}",
        "params": {"collection": "cards", **params},
    }


@pytest.mark.parametrize(
    ("features", "with_store"), [("-gadgets", True), ("gadgets", False)], ids=["off", "no-store"]
)
async def test_a_disabled_server_hides_every_route_before_reading_the_body(
    stores: SimpleNamespace, tmp_path: Path, features: str, with_store: bool
) -> None:
    # Turning the flag on without a store mounts no routes, so the web must not offer gadgets.
    async for client in _client(_app(stores, tmp_path, features=features, with_store=with_store)):
        response = await client.post(
            "/v1/gadgets",
            content="{bad",
            headers={**_as(ALICE), "Content-Type": "application/json"},
        )
        assert response.status_code == 404
        if with_store:
            # A mounted route refuses before reading the body; without a store the routes aren't
            # mounted, and the app's own 404 answers, in whatever shape the app uses.
            assert response.json()["error"]["code"] == "NOT_FOUND"
        assert (await client.get("/v1/gadgets/shared", headers=_as(ALICE))).status_code == 404
        info = (await client.get("/v1/info", headers=_as(ALICE))).json()
        assert info["features"]["gadgets"] is False
        assert info["gadgets"] is False
    assert not list((stores.root / "gadgets").glob("*"))


async def test_rules_of_the_wrong_type_get_the_gadget_error_shape(
    client: httpx.AsyncClient,
) -> None:
    # One input for the HTTP mapping; the service tests cover the malformed-rule matrix.
    constraint = {"name": "bad", "kind": "unique", "collection": [], "fields": ["title"]}
    created = await client.post("/v1/gadgets", json={"name": "Board"}, headers=_as(ALICE))
    response = await client.post(
        f"/v1/gadgets/{created.json()['id']}/revisions",
        json={
            "html": "<main>Board</main>",
            "sdk_version": 1,
            "capabilities": ["data.read"],
            "collections": _CARDS,
            "constraints": [constraint],
        },
        headers=_as(ALICE),
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


async def test_requests_need_an_identity_and_a_json_body(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (await client.get("/v1/gadgets")).status_code == 401
    # A change from a page on an untrusted origin is refused before its body is read.
    monkeypatch.setenv("OMNIGENT_WS_ALLOWED_ORIGINS", "https://omnigent.example")
    foreign = await client.post(
        "/v1/gadgets",
        json={"name": "Board"},
        headers={**_as(ALICE), "Origin": "https://evil.example"},
    )
    assert foreign.status_code == 403
    assert (await client.get("/v1/gadgets", headers=_as(ALICE))).json()["items"] == []
    trusted = await client.post(
        "/v1/gadgets",
        json={"name": "Board"},
        headers={**_as(ALICE), "Origin": "https://omnigent.example"},
    )
    assert trusted.status_code in (200, 201)
    monkeypatch.delenv("OMNIGENT_WS_ALLOWED_ORIGINS")
    form = await client.post(
        "/v1/gadgets", content="name=bad", headers={**_as(ALICE), "Content-Type": "text/plain"}
    )
    assert form.status_code == 415
    assert form.json()["error"]["code"] == "INVALID_ARGUMENT"
    info = (await client.get("/v1/info", headers=_as(ALICE))).json()
    assert info["features"]["gadgets"] is True
    assert info["gadgets"] is True and info["gadget_sharing"] is True


def _own_session(stores: SimpleNamespace, owner: str = ALICE) -> str:
    session = stores.conversations.create_conversation()
    stores.permissions.grant(owner, session.id, LEVEL_OWNER)
    return session.id


async def test_an_owner_builds_publishes_and_uses_a_gadget(
    client: httpx.AsyncClient, stores: SimpleNamespace
) -> None:
    own, other = _own_session(stores), _own_session(stores)
    gadget = await _published(client, actions=[_ACTION])
    base = f"/v1/gadgets/{gadget['id']}"
    assert gadget["browse_url"] == f"/gadget/{gadget['id']}"
    assert gadget["owner_id"] == ALICE and gadget["viewer_id"] == ALICE
    assert set(gadget) >= {"viewer_principal_id", "published_revision", "version", "permissions"}
    assert "workspace_path" not in gadget and "anchor_ids" not in gadget

    created = await client.post(
        f"{base}/rpc",
        json=_rpc(gadget, "create", value={"title": "Ship"}, operation_id="c1"),
        headers=_as(ALICE),
    )
    assert created.status_code == 200, created.text
    record = created.json()["result"]
    # Gadget code sees the author as an opaque ID, the viewer ID its content gives it.
    assert record["value"] == {"title": "Ship"} and record["created_by"] != ALICE
    listed = await client.post(f"{base}/rpc", json=_rpc(gadget, "list"), headers=_as(ALICE))
    assert listed.json()["result"]["items"] == [record]

    described = (await client.get(f"{base}/describe", headers=_as(ALICE))).json()
    assert described["collections"] == _CARDS and described["actions"][0]["name"] == "run_card"
    published = (await client.get(f"{base}/revisions/published", headers=_as(ALICE))).json()
    assert published["html"] == "<main>Board</main>"
    content = (await client.get(f"{base}/content", headers=_as(ALICE))).json()
    assert content["capabilities"] == ["data.read", "data.write"]
    assert content["viewer"]["id"] == record["created_by"]
    assert ALICE not in json.dumps(content["viewer"])

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

    session = await client.put(f"{base}/session", json={"session_id": own}, headers=_as(ALICE))
    assert session.json()["session_id"] == own
    status = (await client.get(f"{base}/session/status", headers=_as(ALICE))).json()
    assert status == {"session_id": own, "status": "idle", "reason": None}

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
    for odd in ([], {}):
        refused = await client.patch(
            f"{base}/comments/{comment.json()['id']}", json={"status": odd}, headers=_as(ALICE)
        )
        assert refused.status_code == 400
        assert refused.json()["error"]["code"] == "INVALID_ARGUMENT"

    pinned = await client.put(f"{base}/pin", json={"pinned": True}, headers=_as(ALICE))
    assert pinned.json() == {
        "pinned": [{"id": gadget["id"], "name": "Board"}],
        "has_owned": True,
        "next_cursor": None,
    }
    linked = await client.put(
        f"{base}/sessions/{other}", json={"linked": True}, headers=_as(ALICE)
    )
    assert [item["id"] for item in linked.json()["items"]] == [gadget["id"]]
    shown = await client.get(f"/v1/gadgets/sessions/{other}", headers=_as(ALICE))
    assert [item["id"] for item in shown.json()["items"]] == [gadget["id"]]
    # Showing it beside a session also made that the gadget session.
    assert (await client.get(f"{base}/session", headers=_as(ALICE))).json()["session_id"] == other

    renamed = await client.patch(base, json={"name": "Launch"}, headers=_as(ALICE))
    assert renamed.json()["name"] == "Launch"
    assert [
        item["name"]
        for item in (await client.get("/v1/gadgets", headers=_as(ALICE))).json()["items"]
    ] == ["Launch"]

    deleted = await client.delete(base, headers=_as(ALICE))
    assert deleted.json() == {"id": gadget["id"], "deleted": True}
    missing = await client.get(base, headers=_as(ALICE))
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "NOT_FOUND"


async def test_the_frame_learns_when_the_published_revision_changed(
    client: httpx.AsyncClient,
) -> None:
    gadget = await _published(client)
    base = f"/v1/gadgets/{gadget['id']}"
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
        json={"revision_id": revision.json()["id"], "if_version": gadget["version"]},
        headers=_as(ALICE),
    )
    stale = await client.post(f"{base}/rpc", json=_rpc(gadget, "get", id="x"), headers=_as(ALICE))
    assert stale.status_code == 409
    assert stale.json()["error"]["request_id"] == "req-1"
    assert stale.json()["error"]["details"] == {
        "reason": "REVISION_CHANGED",
        "published_revision": newer.json()["published_revision"],
    }


async def test_sharing_follows_the_grant_levels(client: httpx.AsyncClient) -> None:
    gadget = await _published(client)
    base = f"/v1/gadgets/{gadget['id']}"
    assert (await client.get(base, headers=_as(BOB))).status_code == 404
    assert (await client.get("/v1/gadgets/shared", headers=_as(BOB))).json()["items"] == []

    granted = await client.put(
        f"{base}/permissions", json={"user_id": BOB, "level": 1}, headers=_as(ALICE)
    )
    assert granted.status_code == 200, granted.text
    assert granted.json() == {
        "user_id": BOB,
        "gadget_id": gadget["id"],
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
    shared = (await client.get("/v1/gadgets/shared", headers=_as(BOB))).json()["items"]
    assert [(item["id"], item["shared_via"], item["owner_email"]) for item in shared] == [
        (gadget["id"], "user", ALICE)
    ]
    write = await client.post(
        f"{base}/rpc",
        json=_rpc(gadget, "create", value={"title": "x"}, operation_id="b1"),
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
            {"user_id": BOB, "gadget_id": gadget["id"], "level": 3, "display_name": BOB}
        ],
        "next_cursor": None,
    }
    revoked = await client.delete(f"{base}/permissions/{BOB}", headers=_as(ALICE))
    assert revoked.status_code == 204
    assert (
        await client.delete(f"{base}/permissions/{BOB}", headers=_as(ALICE))
    ).status_code == 204
    assert (await client.get(base, headers=_as(BOB))).status_code == 404


@pytest.mark.parametrize("user_id", [ALICE, "__public__", "local"])
async def test_grants_refuse_the_owner_the_public_and_the_local_user(
    client: httpx.AsyncClient, user_id: str
) -> None:
    # Blank IDs and unknown levels: test_refused_grants_say_why_in_the_gadget_error_shape.
    gadget = await _published(client)
    response = await client.put(
        f"/v1/gadgets/{gadget['id']}/permissions",
        json={"user_id": user_id, "level": 1},
        headers=_as(ALICE),
    )
    assert response.status_code == 400, response.text


async def test_agents_cannot_change_sharing(client: httpx.AsyncClient) -> None:
    gadget = await _published(client)
    response = await client.put(
        f"/v1/gadgets/{gadget['id']}/permissions",
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
    created = await client.post("/v1/gadgets", json={"name": "From agent"}, headers=agent)
    assert created.status_code == 201, created.text

    listed = await client.get("/v1/gadgets", headers=agent)
    assert [item["name"] for item in listed.json()["items"]] == ["From agent"]
    gadget_id = created.json()["id"]

    # Once Bob can edit the session, he could ask Alice's agent to do anything: reads of
    # her gadgets are refused as well as writes.
    stores.permissions.grant(BOB, session.id, LEVEL_EDIT)
    for method, path in (
        ("POST", "/v1/gadgets"),
        ("GET", "/v1/gadgets"),
        ("GET", f"/v1/gadgets/{gadget_id}"),
        ("GET", f"/v1/gadgets/{gadget_id}/describe"),
        ("GET", f"/v1/gadgets/{gadget_id}/comments"),
        ("GET", f"/v1/gadgets/{gadget_id}/actions"),
        ("GET", "/v1/gadgets/shared"),
    ):
        refused = await client.request(
            method, path, json={"name": "Again"} if method == "POST" else None, headers=agent
        )
        assert refused.status_code == 403, (method, path, refused.text)
        assert refused.json()["error"]["code"] == "PERMISSION_DENIED"
    # Alice herself, outside the agent, still reads them.
    assert (await client.get(f"/v1/gadgets/{gadget_id}", headers=_as(ALICE))).status_code == 200


async def test_deleting_a_gadget_deletes_who_it_was_shared_with(
    client: httpx.AsyncClient, stores: SimpleNamespace
) -> None:
    gadget = await _published(client)
    await client.put(
        f"/v1/gadgets/{gadget['id']}/permissions",
        json={"user_id": BOB, "level": 1},
        headers=_as(ALICE),
    )
    await client.delete(f"/v1/gadgets/{gadget['id']}", headers=_as(ALICE))
    assert stores.permissions.list_for_gadget(gadget["id"]) == ([], None)


async def test_a_revoked_viewers_shelf_drops_the_gadget_and_its_new_name(
    client: httpx.AsyncClient,
) -> None:
    gadget = await _published(client)
    base = f"/v1/gadgets/{gadget['id']}"
    await client.put(f"{base}/permissions", json={"user_id": BOB, "level": 1}, headers=_as(ALICE))
    pinned = await client.put(f"{base}/pin", json={"pinned": True}, headers=_as(BOB))
    assert pinned.json()["pinned"] == [{"id": gadget["id"], "name": "Board"}]

    await client.delete(f"{base}/permissions/{BOB}", headers=_as(ALICE))
    await client.patch(base, json={"name": "Synthetic secret plan"}, headers=_as(ALICE))
    shelf = await client.get("/v1/gadgets/shelf", headers=_as(BOB))
    assert shelf.json() == {"pinned": [], "has_owned": False, "next_cursor": None}
    assert "Synthetic secret plan" not in shelf.text


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"user_id": BOB, "level": 4}, "Can view (1), Can edit (2) or Can manage (3)"),
        ({"user_id": BOB, "level": "1"}, "level"),
        ({"user_id": BOB, "level": 1.0}, "level"),
        ({"user_id": BOB}, "level"),
        ({"user_id": "  ", "level": 1}, "Name a user or group"),
        ({"user_id": f" {ALICE} ", "level": 1}, "owner cannot be granted"),
    ],
)
async def test_refused_grants_say_why_in_the_gadget_error_shape(
    client: httpx.AsyncClient, body: dict, message: str
) -> None:
    gadget = await _published(client)
    response = await client.put(
        f"/v1/gadgets/{gadget['id']}/permissions", json=body, headers=_as(ALICE)
    )
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "INVALID_ARGUMENT"
    assert message in error["message"] and error["message"] != "Invalid request"
    assert "request_id" in error


async def test_sharing_errors_use_the_gadget_codes(client: httpx.AsyncClient) -> None:
    gadget = await _published(client)
    base = f"/v1/gadgets/{gadget['id']}"
    await client.put(f"{base}/permissions", json={"user_id": BOB, "level": 1}, headers=_as(ALICE))
    refused = await client.get(f"{base}/permissions", headers=_as(BOB))
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "PERMISSION_DENIED"
    agent = await client.get(
        f"{base}/permissions", headers=_as(ALICE, **{ORCHESTRATION_SOURCE_HEADER: "a" * 32})
    )
    assert agent.status_code == 403 and agent.json()["error"]["code"] == "PERMISSION_DENIED"


async def test_grantee_ids_are_trimmed_and_one_person_keeps_one_grant(
    client: httpx.AsyncClient, stores: SimpleNamespace
) -> None:
    gadget = await _published(client)
    base = f"/v1/gadgets/{gadget['id']}"
    spaced = await client.put(
        f"{base}/permissions", json={"user_id": f"  {BOB} ", "level": 1}, headers=_as(ALICE)
    )
    assert spaced.status_code == 200, spaced.text
    assert spaced.json()["user_id"] == BOB
    assert stores.permissions.get_user(f"  {BOB} ") is None
    again = await client.put(
        f"{base}/permissions", json={"user_id": BOB, "level": 2}, headers=_as(ALICE)
    )
    assert again.json()["user_id"] == BOB and again.json()["level"] == 2
    grants, _ = stores.permissions.list_for_gadget(gadget["id"])
    assert [(grant.user_id, grant.level) for grant in grants] == [(BOB, 2)]


async def test_ids_that_differ_only_in_case_are_separate_grantees(
    client: httpx.AsyncClient, stores: SimpleNamespace
) -> None:
    # Authorization matches principal IDs exactly, so sharing does too: granting or
    # revoking `usera` never changes what `UserA` holds.
    for user_id in ("UserA", "usera"):
        stores.permissions.ensure_user(user_id)
    gadget = await _published(client)
    base = f"/v1/gadgets/{gadget['id']}"
    reader = await client.put(
        f"{base}/permissions", json={"user_id": "UserA", "level": 1}, headers=_as(ALICE)
    )
    assert reader.status_code == 200, reader.text
    manager = await client.put(
        f"{base}/permissions", json={"user_id": "usera", "level": 3}, headers=_as(ALICE)
    )
    assert manager.status_code == 200, manager.text
    assert manager.json()["user_id"] == "usera"
    assert stores.permissions.resolve_gadget_access(["UserA"], gadget["id"]) == 1
    assert stores.permissions.resolve_gadget_access(["usera"], gadget["id"]) == 3

    revoked = await client.delete(f"{base}/permissions/usera", headers=_as(ALICE))
    assert revoked.status_code == 204, revoked.text
    assert stores.permissions.resolve_gadget_access(["usera"], gadget["id"]) is None
    assert stores.permissions.resolve_gadget_access(["UserA"], gadget["id"]) == 1
    # Revoking an ID that holds no grant leaves the other spelling's grant alone too.
    missing = await client.delete(f"{base}/permissions/USERA", headers=_as(ALICE))
    assert missing.status_code == 204
    assert stores.permissions.resolve_gadget_access(["UserA"], gadget["id"]) == 1


async def test_a_store_that_canonicalizes_aliases_keeps_one_grant_per_person(
    client: httpx.AsyncClient, stores: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A deployment whose principals are case-insensitive says so in validate_grants;
    # the routes then share, re-share and revoke under its canonical ID.
    def lowercase(grants: Any) -> list[tuple[str, int]]:
        return [(user_id.lower(), level) for user_id, level in grants]

    monkeypatch.setattr(stores.permissions, "validate_grants", lowercase)
    gadget = await _published(client)
    base = f"/v1/gadgets/{gadget['id']}"
    first = await client.put(
        f"{base}/permissions", json={"user_id": BOB.upper(), "level": 1}, headers=_as(ALICE)
    )
    assert first.status_code == 200, first.text
    assert first.json()["user_id"] == BOB
    again = await client.put(
        f"{base}/permissions", json={"user_id": "Bob@Example.com", "level": 2}, headers=_as(ALICE)
    )
    assert again.json()["user_id"] == BOB and again.json()["level"] == 2
    grants, _ = stores.permissions.list_for_gadget(gadget["id"])
    assert [(grant.user_id, grant.level) for grant in grants] == [(BOB, 2)]
    owner = await client.put(
        f"{base}/permissions", json={"user_id": ALICE.upper(), "level": 1}, headers=_as(ALICE)
    )
    assert owner.status_code == 400 and "owner cannot be granted" in owner.text
    revoked = await client.delete(f"{base}/permissions/{BOB.upper()}", headers=_as(ALICE))
    assert revoked.status_code == 204
    assert stores.permissions.resolve_gadget_access([BOB], gadget["id"]) is None


async def test_a_re_share_does_not_read_the_grant_list(
    client: httpx.AsyncClient, stores: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    gadget = await _published(client)
    base = f"/v1/gadgets/{gadget['id']}"
    await client.put(f"{base}/permissions", json={"user_id": BOB, "level": 1}, headers=_as(ALICE))

    def no_listing(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("one grant must not page through the gadget's grants")

    monkeypatch.setattr(stores.permissions, "list_for_gadget", no_listing)
    again = await client.put(
        f"{base}/permissions", json={"user_id": BOB, "level": 2}, headers=_as(ALICE)
    )
    assert again.status_code == 200, again.text
    assert again.json()["user_id"] == BOB and again.json()["level"] == 2


async def test_a_grant_racing_the_gadgets_deletion_leaves_no_grant(
    client: httpx.AsyncClient, stores: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    gadget = await _published(client)
    original = stores.permissions.grant_for_gadget

    def deleted_meanwhile(*args: Any, **kwargs: Any) -> Any:
        # The gadget and its grants go just before this grant commits.
        stores.gadgets.delete(gadget["id"])
        stores.permissions.delete_for_gadget(gadget["id"])
        return original(*args, **kwargs)

    monkeypatch.setattr(stores.permissions, "grant_for_gadget", deleted_meanwhile)
    response = await client.put(
        f"/v1/gadgets/{gadget['id']}/permissions",
        json={"user_id": BOB, "level": 1},
        headers=_as(ALICE),
    )
    assert response.status_code == 404
    assert stores.permissions.list_for_gadget(gadget["id"]) == ([], None)


async def test_a_gadget_is_shown_only_beside_a_session_its_viewer_can_read(
    client: httpx.AsyncClient, stores: SimpleNamespace
) -> None:
    gadget = await _published(client)
    base = f"/v1/gadgets/{gadget['id']}"
    bobs = _own_session(stores, BOB)
    made_up = "0" * 32
    for session_id in (bobs, made_up):
        linked = await client.put(
            f"{base}/sessions/{session_id}", json={"linked": True}, headers=_as(ALICE)
        )
        assert linked.status_code == 404, linked.text
        registered = await client.put(
            f"{base}/session", json={"session_id": session_id}, headers=_as(ALICE)
        )
        assert registered.status_code == 404, registered.text
    assert (await client.get(f"{base}/session", headers=_as(ALICE))).json()["session_id"] is None
    # A session shared with her shows the gadget, but its agent works for Bob, so it doesn't
    # become her gadget session, and she can't make it one.
    stores.permissions.grant(ALICE, bobs, LEVEL_EDIT)
    linked = await client.put(f"{base}/sessions/{bobs}", json={"linked": True}, headers=_as(ALICE))
    assert linked.status_code == 200, linked.text
    assert (await client.get(f"{base}/session", headers=_as(ALICE))).json()["session_id"] is None
    registered = await client.put(f"{base}/session", json={"session_id": bobs}, headers=_as(ALICE))
    assert registered.status_code == 403, registered.text
    cited = await client.put(
        f"{base}/sessions/{bobs}",
        json={"linked": True, "handles_actions": False},
        headers=_as(ALICE),
    )
    assert cited.status_code == 200, cited.text

    # Her own session shared with edit access shows it too, and takes no actions either.
    shared = _own_session(stores)
    stores.permissions.grant(BOB, shared, LEVEL_EDIT)
    linked = await client.put(
        f"{base}/sessions/{shared}", json={"linked": True}, headers=_as(ALICE)
    )
    assert linked.status_code == 200, linked.text
    assert (await client.get(f"{base}/session", headers=_as(ALICE))).json()["session_id"] is None
    registered = await client.put(
        f"{base}/session", json={"session_id": shared}, headers=_as(ALICE)
    )
    assert registered.status_code == 403, registered.text

    # Her own private session becomes her gadget session; shared only with a reader it stays
    # one, and shared with an editor later it stops being used.
    private = _own_session(stores)
    linked = await client.put(
        f"{base}/sessions/{private}", json={"linked": True}, headers=_as(ALICE)
    )
    assert linked.status_code == 200, linked.text
    current = await client.get(f"{base}/session", headers=_as(ALICE))
    assert current.json()["session_id"] == private
    stores.permissions.grant(BOB, private, LEVEL_READ)
    current = await client.get(f"{base}/session", headers=_as(ALICE))
    assert current.json()["session_id"] == private
    stores.permissions.grant(BOB, private, LEVEL_EDIT)
    current = await client.get(f"{base}/session", headers=_as(ALICE))
    assert current.json()["session_id"] is None


async def test_viewers_read_only_the_published_revision(client: httpx.AsyncClient) -> None:
    gadget = await _published(client)
    base = f"/v1/gadgets/{gadget['id']}"
    draft = await client.post(
        f"{base}/revisions",
        json={
            "html": "<p>Synthetic draft</p>",
            "sdk_version": 1,
            "capabilities": [],
            "collections": {},
        },
        headers=_as(ALICE),
    )
    await client.put(f"{base}/permissions", json={"user_id": BOB, "level": 1}, headers=_as(ALICE))
    hidden = await client.get(f"{base}/revisions/{draft.json()['id']}", headers=_as(BOB))
    assert hidden.status_code == 404 and "Synthetic draft" not in hidden.text
    published = await client.get(
        f"{base}/revisions/{gadget['published_revision']}", headers=_as(BOB)
    )
    assert published.status_code == 200
    own = await client.get(f"{base}/revisions/{draft.json()['id']}", headers=_as(ALICE))
    assert own.json()["html"] == "<p>Synthetic draft</p>"


async def test_an_account_deleted_after_its_request_authenticated_writes_nothing(
    client: httpx.AsyncClient, stores: SimpleNamespace
) -> None:
    # The account is gone by the time the gadget routes look up its registration: with none,
    # the write would land where the account's cleanup never looks.
    from omnigent.server.accounts_store import SqlAlchemyAccountStore

    accounts = SqlAlchemyAccountStore(stores.db_uri)
    accounts.create_user_with_password("carol@example.com", "carol-hash")
    assert accounts.delete_user("carol@example.com") is True

    refused = await client.post(
        "/v1/gadgets", json={"name": "Late board"}, headers=_as("carol@example.com")
    )

    assert refused.status_code == 401, refused.text
    assert list(stores.gadgets.iter_owned("carol@example.com")) == []
    assert not (stores.root / "owners").exists() or not any(
        stores.root.joinpath("owners").iterdir()
    )


async def test_an_account_activated_from_a_sharing_placeholder_uses_gadgets(
    client: httpx.AsyncClient, stores: SimpleNamespace
) -> None:
    # Sharing made a placeholder row without a registration; an administrator then set a
    # password. The live account is itself: it uses gadgets, with no registration.
    from omnigent.db.account_authority import account_authority_scope
    from omnigent.server.accounts_store import SqlAlchemyAccountStore

    accounts = SqlAlchemyAccountStore(stores.db_uri)
    stores.permissions.ensure_user("erin@example.com")
    accounts.update_password("erin@example.com", "erin-hash")
    account = accounts.get_user("erin@example.com")
    assert account is not None and account.account_generation is not None
    # As the accounts login binds it: the request carries the account's generation.
    with account_authority_scope("erin@example.com", account.account_generation):
        assert stores.permissions.outlived_accounts(["erin@example.com"]) == set()
        created = await client.post(
            "/v1/gadgets", json={"name": "Erin's board"}, headers=_as("erin@example.com")
        )
        assert created.status_code == 201, created.text
        listed = await client.get("/v1/gadgets", headers=_as("erin@example.com"))
    assert [item["id"] for item in listed.json()["items"]] == [created.json()["id"]]


def test_a_request_that_outlived_its_account_names_no_registration(
    stores: SimpleNamespace,
) -> None:
    # Authenticated as one account, whose name was then deleted and registered again: the
    # new account's registration is not this request's.
    from omnigent.db.account_authority import account_authority_scope
    from omnigent.server.accounts_store import SqlAlchemyAccountStore

    accounts = SqlAlchemyAccountStore(stores.db_uri)
    old = accounts.create_user_with_password("dave@example.com", "dave-hash")
    assert accounts.delete_user("dave@example.com") is True
    new = accounts.create_user_with_password("dave@example.com", "dave-hash-2")
    assert new.account_generation != old.account_generation

    with account_authority_scope("dave@example.com", old.account_generation):
        assert stores.permissions.account_registrations(["dave@example.com"]) == {}
        assert stores.permissions.outlived_accounts(["dave@example.com"]) == {"dave@example.com"}
    with account_authority_scope("dave@example.com", new.account_generation):
        assert "dave@example.com" in stores.permissions.account_registrations(["dave@example.com"])


async def test_a_create_waiting_too_long_for_the_owner_index_is_busy_and_retryable(
    client: httpx.AsyncClient, stores: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filelock import FileLock

    import omnigent.stores.gadget_store.local as local_module

    first = await client.post("/v1/gadgets", json={"name": "Board"}, headers=_as(ALICE))
    assert first.status_code == 201
    owner_dir = stores.gadgets._owner_dir(ALICE, first.json().get("owner_registration"))
    monkeypatch.setattr(local_module, "_OWNER_INDEX_WAIT_S", 0.05)
    # Another process updating Alice's owner index meanwhile.
    held = FileLock(str(stores.root / ".locks" / f"{owner_dir.name}.lock"))
    with held:
        busy = await client.post("/v1/gadgets", json={"name": "Second"}, headers=_as(ALICE))
    assert busy.status_code == 503 and busy.headers["retry-after"] == "1"
    assert busy.json()["error"]["details"] == {"reason": "busy", "retryable": True}
    again = await client.post("/v1/gadgets", json={"name": "Second"}, headers=_as(ALICE))
    assert again.status_code == 201


async def test_store_conflicts_carry_their_reason_and_busy_ones_are_retryable() -> None:
    from omnigent.server.gadgets import gadget_failure
    from omnigent.stores.gadget_store import Conflict

    stale = gadget_failure(
        Conflict("changed", reason="version_mismatch", current_version="v2"), "req-9"
    )
    assert stale.status_code == 409
    assert json.loads(bytes(stale.body))["error"] == {
        "code": "CONFLICT",
        "message": "changed",
        "request_id": "req-9",
        "details": {"reason": "version_mismatch", "current_version": "v2"},
    }
    busy = gadget_failure(Conflict("busy", reason="busy"), "req-10")
    assert busy.status_code == 503 and busy.headers["retry-after"] == "1"
    error = json.loads(bytes(busy.body))["error"]
    assert error["code"] == "UNAVAILABLE"
    assert error["details"] == {"reason": "busy", "retryable": True}


@pytest.mark.parametrize("features", ["gadgets", ""])
async def test_the_app_has_account_deletion_remove_gadgets_only_with_the_feature(
    stores: SimpleNamespace, tmp_path: Path, features: str
) -> None:
    from omnigent.gadgets.accounts import DeletedAccountGadgets
    from omnigent.server.accounts_store import SqlAlchemyAccountStore

    accounts = SqlAlchemyAccountStore(stores.db_uri)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    create_app(
        feature_flags=resolve_feature_flags({"OMNIGENT_FEATURES": features}),
        agent_store=SqlAlchemyAgentStore(stores.db_uri),
        file_store=SqlAlchemyFileStore(stores.db_uri),
        conversation_store=stores.conversations,
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        permission_store=stores.permissions,
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        account_store=accounts,
        gadget_store=stores.gadgets,
    )
    assert isinstance(accounts._gadgets, DeletedAccountGadgets)
    # With gadgets off, deleting an account doesn't touch the gadget store.
    assert accounts._gadgets._enabled() is bool(features)


def test_a_session_id_that_is_not_a_uuid_is_left_to_the_permission_store(
    stores: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Deployments can plug in stores whose session IDs are not UUIDs (for example decimal IDs);
    # the permission store, not the ID's format, decides. The stub stands in for such a
    # deployment's access check.
    import omnigent.server.gadgets as gadget_routes

    def check(user_id: str | None, session_id: str, *_args: object, **_kwargs: object) -> bool:
        if session_id == "not-parseable":
            raise ValueError("bad id")
        return user_id == ALICE and session_id == "102717306144392"

    monkeypatch.setattr(gadget_routes, "check_session_access", check)
    can_read = gadget_routes.viewer_can_read_session
    perms, convs = stores.permissions, stores.conversations
    assert can_read(ALICE, "102717306144392", perms, convs)
    assert not can_read(BOB, "102717306144392", perms, convs)
    assert not can_read(ALICE, "not-parseable", perms, convs)
    # Single-user mode has no permission store and takes any well-formed ID.
    assert can_read(ALICE, "0" * 32, None, convs)
    assert not can_read(ALICE, "102717306144392", None, convs)


async def _every_page(
    client: httpx.AsyncClient, path: str, user: str, key: str = "items"
) -> list[list[str]]:
    pages: list[list[str]] = []
    cursor = None
    while True:
        params = {"limit": "1", **({"cursor": cursor} if cursor else {})}
        body = (await client.get(path, params=params, headers=_as(user))).json()
        pages.append([item["id"] for item in body[key]])
        cursor = body["next_cursor"]
        if cursor is None:
            return pages


async def test_own_and_shared_lists_page_with_a_cursor(
    client: httpx.AsyncClient, stores: SimpleNamespace
) -> None:
    made = []
    for n in range(3):
        created = await client.post("/v1/gadgets", json={"name": f"Board {n}"}, headers=_as(ALICE))
        made.append(created.json()["id"])
        granted = await client.put(
            f"/v1/gadgets/{made[-1]}/permissions",
            json={"user_id": BOB, "level": 1},
            headers=_as(ALICE),
        )
        assert granted.status_code == 200, granted.text
    owned = await _every_page(client, "/v1/gadgets", ALICE)
    assert [len(page) for page in owned] == [1, 1, 1] and [
        item for page in owned for item in page
    ] == sorted(made)
    shared = await _every_page(client, "/v1/gadgets/shared", BOB)
    assert sorted([item for page in shared for item in page]) == sorted(made)
    for path in ("/v1/gadgets", f"/v1/gadgets/{made[0]}/actions"):
        # Not a number, a digit Python reads but isn't ASCII, and one too long to convert.
        for limit in ("nope", "\u0663", "9" * 4301):
            bad = await client.get(path, params={"limit": limit}, headers=_as(ALICE))
            assert bad.status_code == 400, (path, limit[:8], bad.text)
            assert bad.json()["error"]["code"] == "INVALID_ARGUMENT"

    # The pins and the gadgets shown beside a session page the same way, in their order.
    session = _own_session(stores, ALICE)
    for gadget_id in made:
        pinned = await client.put(
            f"/v1/gadgets/{gadget_id}/pin", json={"pinned": True}, headers=_as(ALICE)
        )
        assert pinned.status_code == 200, pinned.text
        linked = await client.put(
            f"/v1/gadgets/{gadget_id}/sessions/{session}",
            json={"linked": True},
            headers=_as(ALICE),
        )
        assert linked.status_code == 200, linked.text
    pins = await _every_page(client, "/v1/gadgets/shelf", ALICE, key="pinned")
    assert pins == [[gadget_id] for gadget_id in made]
    beside = await _every_page(client, f"/v1/gadgets/sessions/{session}", ALICE)
    assert beside == [[gadget_id] for gadget_id in made]
    bad_cursor = await client.get(
        "/v1/gadgets/shelf", params={"cursor": "nope"}, headers=_as(ALICE)
    )
    assert bad_cursor.status_code == 400


def test_the_published_api_states_the_statuses_and_nullable_fields_the_routes_use() -> None:
    # The creates answer 201, and an action update clears ``summary``/``session_id`` with null.
    spec = json.loads((Path(__file__).resolve().parents[3] / "openapi.json").read_text())
    paths = spec["paths"]
    for path in ("/v1/gadgets", "/v1/gadgets/{app_id}/revisions", "/v1/gadgets/{app_id}/comments"):
        assert "201" in paths[path]["post"]["responses"], path
        assert "200" not in paths[path]["post"]["responses"], path
    update = paths["/v1/gadgets/{app_id}/actions/{action_id}"]["patch"]
    fields = update["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert fields["summary"]["type"] == ["string", "null"]
    assert fields["session_id"]["type"] == ["string", "null"]
