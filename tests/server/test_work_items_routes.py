"""Tests for the work-items REST routes (single-user mode)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.server.routes.work_items import create_work_items_router
from omnigent.stores.work_item_store.sqlalchemy_store import SqlAlchemyWorkItemStore


@pytest.fixture()
def client(db_uri: str) -> TestClient:
    """A TestClient over a minimal app mounting only the work-items router.

    No auth_provider/permission_store → single-user mode (no auth required),
    which keeps the test focused on the route behavior.
    """
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": exc.code, "message": exc.message},
        )

    store = SqlAlchemyWorkItemStore(db_uri)
    app.include_router(create_work_items_router(store), prefix="/v1")
    return TestClient(app)


def test_crud_flow(client: TestClient) -> None:
    created = client.post(
        "/v1/work-items",
        json={"title": "Fix deploy", "source": "github", "external_id": "123"},
    )
    assert created.status_code == 200
    payload = created.json()
    assert payload["created"] is True
    assert payload["object"] == "work_item"
    wid = payload["id"]

    listed = client.get("/v1/work-items").json()
    assert listed["object"] == "list"
    assert [i["id"] for i in listed["data"]] == [wid]

    got = client.get(f"/v1/work-items/{wid}")
    assert got.status_code == 200
    assert got.json()["title"] == "Fix deploy"

    patched = client.patch(
        f"/v1/work-items/{wid}",
        json={"status": "needs_review", "pr_url": "https://github.com/acme/app/pull/9"},
    ).json()
    assert patched["status"] == "needs_review"
    assert patched["pr_url"].endswith("/pull/9")

    assert client.get("/v1/work-items", params={"status": "needs_review"}).json()["data"]
    assert client.get("/v1/work-items", params={"status": "done"}).json()["data"] == []

    assert client.delete(f"/v1/work-items/{wid}").json() == {"deleted": True}
    assert client.get(f"/v1/work-items/{wid}").status_code == 404


def test_create_is_idempotent_by_dedup_key(client: TestClient) -> None:
    first = client.post(
        "/v1/work-items",
        json={"title": "t", "source": "slack", "dedup_key": "slack:C1/1.2"},
    ).json()
    assert first["created"] is True
    second = client.post(
        "/v1/work-items",
        json={"title": "dup", "source": "slack", "dedup_key": "slack:C1/1.2"},
    ).json()
    assert second["created"] is False
    assert second["id"] == first["id"]


def test_validation_and_not_found(client: TestClient) -> None:
    bad = client.post("/v1/work-items", json={"title": "x", "source": "bogus"})
    assert bad.status_code == 400

    missing = client.patch("/v1/work-items/wi_nope", json={"status": "done"})
    assert missing.status_code == 404
