"""Tests for the GET /v1/canvas/{conversation_id} route (single-user mode)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.server.routes.canvas import create_canvas_router
from omnigent.stores.canvas_store.sqlalchemy_store import SqlAlchemyCanvasStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


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
