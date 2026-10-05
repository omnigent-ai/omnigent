"""Routes for the Design page deck index (``/v1/design/artifacts``)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.routes.design import create_design_router
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


class _HeaderAuth:
    def get_user_id(self, request: object) -> str | None:
        return getattr(request, "headers", {}).get("x-test-user")


def _app(
    store: SqlAlchemyConversationStore,
    permissions: SqlAlchemyPermissionStore,
    *,
    enabled: bool = True,
) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(status_code=exc.http_status, content={"error": {"code": exc.code}})

    flags = FeatureFlags(frozenset({Feature.DESIGN}) if enabled else frozenset())
    app.include_router(
        create_design_router(store, permissions, auth_provider=_HeaderAuth(), feature_flags=flags),
        prefix="/v1",
    )
    return app


@pytest.fixture
def stores(db_uri: str) -> tuple[SqlAlchemyConversationStore, SqlAlchemyPermissionStore]:
    permissions = SqlAlchemyPermissionStore(db_uri)
    for user in ("alice", "bob", "carol"):
        permissions.ensure_user(user)
    return SqlAlchemyConversationStore(db_uri), permissions


async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://server"
    ) as client:
        yield client


@pytest.fixture
async def client(stores) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    async for c in _client(_app(*stores)):
        yield c


def _as(user: str) -> dict[str, str]:
    return {"x-test-user": user}


async def test_list_returns_only_sessions_the_caller_can_see(stores, client) -> None:  # type: ignore[no-untyped-def]
    store, permissions = stores
    mine = store.create_conversation(title="Q3 review", workspace="/ws/brand")
    shared = store.create_conversation(title="Launch", workspace="/ws/launch")
    private = store.create_conversation(title="Secret", workspace="/ws/secret")
    permissions.grant("alice", mine.id, LEVEL_OWNER)
    permissions.grant("bob", shared.id, LEVEL_OWNER)
    permissions.grant("alice", shared.id, LEVEL_READ)
    permissions.grant("bob", private.id, LEVEL_OWNER)
    store.record_design_artifact(mine.id, "q3.slides.html", "deck", now=300)
    store.record_design_artifact(shared.id, "launch.slides.html", "deck", now=200)
    store.record_design_artifact(shared.id, "flow.wireframe.html", "wireframe", now=150)
    store.record_design_artifact(private.id, "secret.slides.html", "deck", now=100)

    resp = await client.get("/v1/design/artifacts", headers=_as("alice"))
    assert resp.status_code == 200
    assert resp.json()["data"] == [
        {
            "session_id": mine.id,
            "path": "q3.slides.html",
            "kind": "deck",
            "updated_at": 300,
            "session_title": "Q3 review",
            "workspace": "/ws/brand",
        },
        {
            "session_id": shared.id,
            "path": "launch.slides.html",
            "kind": "deck",
            "updated_at": 200,
            "session_title": "Launch",
            "workspace": "/ws/launch",
        },
        {
            "session_id": shared.id,
            "path": "flow.wireframe.html",
            "kind": "wireframe",
            "updated_at": 150,
            "session_title": "Launch",
            "workspace": "/ws/launch",
        },
    ]

    decks = await client.get("/v1/design/artifacts?kind=wireframe", headers=_as("alice"))
    assert [a["path"] for a in decks.json()["data"]] == ["flow.wireframe.html"]
    carol = await client.get("/v1/design/artifacts", headers=_as("carol"))
    assert carol.json()["data"] == []


async def test_reconcile_replaces_the_session_rows(stores, client) -> None:  # type: ignore[no-untyped-def]
    store, permissions = stores
    conv = store.create_conversation(workspace="/ws")
    permissions.grant("alice", conv.id, LEVEL_OWNER)
    permissions.grant("bob", conv.id, LEVEL_READ)
    store.record_design_artifact(conv.id, "old.slides.html", "deck", now=1)
    store.record_design_artifact(conv.id, "flow.wireframe.html", "wireframe", now=1)

    resp = await client.put(
        f"/v1/sessions/{conv.id}/design-artifacts",
        json={"paths": ["new.slides.html", "notes.md"], "kind": "deck"},
        headers=_as("alice"),
    )
    assert resp.status_code == 204
    assert sorted(a.path for a in store.list_design_artifacts()) == [
        "flow.wireframe.html",
        "new.slides.html",
    ]

    # A read-only collaborator cannot rewrite the index; a stranger gets 404.
    reader = await client.put(
        f"/v1/sessions/{conv.id}/design-artifacts", json={"paths": []}, headers=_as("bob")
    )
    stranger = await client.put(
        f"/v1/sessions/{conv.id}/design-artifacts", json={"paths": []}, headers=_as("carol")
    )
    assert (reader.status_code, stranger.status_code) == (403, 404)
    assert len(store.list_design_artifacts()) == 2


async def test_both_routes_are_dark_without_the_design_flag(stores) -> None:  # type: ignore[no-untyped-def]
    store, permissions = stores
    conv = store.create_conversation()
    permissions.grant("alice", conv.id, LEVEL_OWNER)
    async for client in _client(_app(store, permissions, enabled=False)):
        listed = await client.get("/v1/design/artifacts", headers=_as("alice"))
        put = await client.put(
            f"/v1/sessions/{conv.id}/design-artifacts", json={"paths": []}, headers=_as("alice")
        )
        assert (listed.status_code, put.status_code) == (404, 404)


async def test_list_requires_a_user(client) -> None:  # type: ignore[no-untyped-def]
    assert (await client.get("/v1/design/artifacts")).status_code == 401


async def test_list_excludes_archived_and_sub_agent_sessions(stores, client) -> None:  # type: ignore[no-untyped-def]
    store, permissions = stores
    live = store.create_conversation(workspace="/ws/live")
    archived = store.create_conversation(workspace="/ws/archived")
    child = store.create_conversation(parent_conversation_id=live.id, workspace="/ws/live")
    for conv in (live, archived, child):
        permissions.grant("alice", conv.id, LEVEL_OWNER)
        store.record_design_artifact(conv.id, f"{conv.id}.slides.html", "deck", now=1)
    store.update_conversation(archived.id, archived=True)

    resp = await client.get("/v1/design/artifacts", headers=_as("alice"))
    assert [a["session_id"] for a in resp.json()["data"]] == [live.id]


@pytest.mark.parametrize(
    "path",
    [
        "/abs/a.slides.html",
        "decks\\a.slides.html",
        "decks//a.slides.html",
        "./a.slides.html",
        "decks/../a.slides.html",
        "",
        "x" * 500 + "/a.slides.html",
    ],
)
async def test_reconcile_rejects_unsafe_paths(stores, client, path: str) -> None:  # type: ignore[no-untyped-def]
    store, permissions = stores
    conv = store.create_conversation()
    permissions.grant("alice", conv.id, LEVEL_OWNER)
    resp = await client.put(
        f"/v1/sessions/{conv.id}/design-artifacts",
        json={"paths": ["decks/a.slides.html", path]},
        headers=_as("alice"),
    )
    assert resp.status_code == 422
    assert store.list_design_artifacts() == []


async def test_reconcile_accepts_a_nested_relative_path(stores, client) -> None:  # type: ignore[no-untyped-def]
    store, permissions = stores
    conv = store.create_conversation()
    permissions.grant("alice", conv.id, LEVEL_OWNER)
    resp = await client.put(
        f"/v1/sessions/{conv.id}/design-artifacts",
        json={"paths": ["decks/a.slides.html"]},
        headers=_as("alice"),
    )
    assert resp.status_code == 204
    assert [a.path for a in store.list_design_artifacts()] == ["decks/a.slides.html"]
