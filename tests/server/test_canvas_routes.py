"""Tests for the GET /v1/canvas/{conversation_id} route (single-user mode)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes.canvas import create_canvas_router
from omnigent.stores.canvas_store.sqlalchemy_store import SqlAlchemyCanvasStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


@pytest.fixture()
def ctx(db_uri: str) -> tuple[TestClient, SqlAlchemyCanvasStore, str]:
    store = SqlAlchemyCanvasStore(db_uri)
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(title="c").id
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.code})

    app.include_router(create_canvas_router(store), prefix="/v1")
    return TestClient(app), store, conv


def test_get_canvas_returns_content(
    ctx: tuple[TestClient, SqlAlchemyCanvasStore, str],
) -> None:
    client, store, conv = ctx
    store.upsert("cnv_1", conv, "Report", "<h1>Hi</h1>", "html")

    res = client.get(f"/v1/canvas/{conv}")
    assert res.status_code == 200
    body = res.json()
    assert body["object"] == "canvas"
    assert body["title"] == "Report"
    assert body["content"] == "<h1>Hi</h1>"
    assert body["content_type"] == "html"


def test_get_canvas_404_when_unset(
    ctx: tuple[TestClient, SqlAlchemyCanvasStore, str],
) -> None:
    client, _, conv = ctx
    assert client.get(f"/v1/canvas/{conv}").status_code == 404


def test_canvas_routes_404_when_disabled(
    ctx: tuple[TestClient, SqlAlchemyCanvasStore, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``canvas.enabled`` is off, both handlers 404 before touching the
    store — the hard server-side gate (#2). GET 404s even though a canvas
    exists; PUT 404s without writing."""
    from omnigent.runtime.caps import RuntimeCaps

    client, store, conv = ctx
    store.upsert("cnv_1", conv, "Report", "<h1>Hi</h1>", "html")
    monkeypatch.setattr(
        "omnigent.server.routes.canvas.get_caps",
        lambda: RuntimeCaps(canvas_enabled=False),
    )

    assert client.get(f"/v1/canvas/{conv}").status_code == 404
    put = client.put(
        f"/v1/canvas/{conv}",
        json={"title": "T", "content": "<p>x</p>", "content_type": "html"},
    )
    assert put.status_code == 404


def test_put_rejects_oversized_content(
    ctx: tuple[TestClient, SqlAlchemyCanvasStore, str],
) -> None:
    from omnigent.entities.canvas import MAX_CANVAS_CONTENT_BYTES

    client, _, conv = ctx
    huge = "x" * (MAX_CANVAS_CONTENT_BYTES + 1)
    res = client.put(f"/v1/canvas/{conv}", json={"title": "T", "content": huge})
    assert res.status_code == 400


def test_multi_user_requires_conversation_access(db_uri: str) -> None:
    # A canvas is conversation-scoped data: in multi-user mode only a caller
    # with access to the conversation may read/write it. A user with no access
    # is 404'd on both GET and PUT, so the conversation's existence isn't leaked.
    from omnigent.server.auth import LEVEL_OWNER

    canvas_store = SqlAlchemyCanvasStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    perm = SqlAlchemyPermissionStore(db_uri)
    conv = conv_store.create_conversation(title="c").id
    for user in ("alice@example.com", "bob@example.com"):
        perm.ensure_user(user)
    perm.grant("alice@example.com", conv, LEVEL_OWNER)  # only alice has access
    canvas_store.upsert("cnv_1", conv, "Report", "<h1>Hi</h1>", "html")

    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.code})

    app.include_router(
        create_canvas_router(
            canvas_store,
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=True),
            permission_store=perm,
            conversation_store=conv_store,
        ),
        prefix="/v1",
    )
    client = TestClient(app)
    alice = {"X-Forwarded-Email": "alice@example.com"}
    bob = {"X-Forwarded-Email": "bob@example.com"}

    # Owner reads + writes.
    assert client.get(f"/v1/canvas/{conv}", headers=alice).status_code == 200
    assert (
        client.put(
            f"/v1/canvas/{conv}", json={"title": "T", "content": "<p>x</p>"}, headers=alice
        ).status_code
        == 200
    )
    # A user with no access to the conversation → 404 on both.
    assert client.get(f"/v1/canvas/{conv}", headers=bob).status_code == 404
    assert (
        client.put(
            f"/v1/canvas/{conv}", json={"title": "T", "content": "<p>x</p>"}, headers=bob
        ).status_code
        == 404
    )
