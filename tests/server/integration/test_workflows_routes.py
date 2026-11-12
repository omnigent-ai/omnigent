"""HTTP tests for ``/v1/workflows``: saving, paging, editing and ownership."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from tests.server.conftest import ControllableMockClient
from tests.server.integration.test_scheduled_tasks_routes import (
    _build_app,
    _headers,
    _make_user,
    _serve,
)

pytestmark = pytest.mark.asyncio

ALICE = "alice@example.com"
BOB = "bob@example.com"
STEPS = [{"id": "build", "name": "Build"}, {"id": "test", "name": "Test", "depends_on": ["build"]}]


@pytest.fixture()
def app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    app = _build_app(db_uri, tmp_path)
    for user in (ALICE, BOB):
        _make_user(db_uri, user)
    return app


@pytest_asyncio.fixture()
async def client(
    app: FastAPI, mock_llm: ControllableMockClient, tmp_path: Path
) -> AsyncIterator[httpx.AsyncClient]:
    async with _serve(app, mock_llm, tmp_path) as client:
        yield client


async def test_a_workflow_round_trips_and_stays_private(client: httpx.AsyncClient) -> None:
    body = {"name": "nightly-build", "description": "Build and test main.", "steps": STEPS}
    created = await client.post("/v1/workflows", json=body, headers=_headers(ALICE))
    assert created.status_code == 200, created.text
    workflow = created.json()
    assert workflow["object"] == "workflow" and workflow["name"] == "nightly-build"
    assert workflow["steps"][1] == {
        "id": "test",
        "name": "Test",
        "goal": None,
        "depends_on": ["build"],
    }

    fetched = await client.get(f"/v1/workflows/{workflow['id']}", headers=_headers(ALICE))
    assert fetched.json() == workflow
    other = await client.get(f"/v1/workflows/{workflow['id']}", headers=_headers(BOB))
    assert other.status_code == 404

    page = (await client.get("/v1/workflows", headers=_headers(ALICE))).json()
    assert page["object"] == "list" and page["has_more"] is False
    assert [item["name"] for item in page["data"]] == ["nightly-build"]
    assert "steps" not in page["data"][0]
    assert page["first_id"] == page["last_id"] == workflow["id"]
    found = await client.get("/v1/workflows?name_prefix=nightly", headers=_headers(ALICE))
    assert [item["id"] for item in found.json()["data"]] == [workflow["id"]]
    assert (await client.get("/v1/workflows", headers=_headers(BOB))).json()["data"] == []


async def test_writes_report_conflicts_bad_input_and_missing_workflows(
    client: httpx.AsyncClient,
) -> None:
    body = {"name": "deploy", "description": "Ship it.", "steps": STEPS}
    created = (await client.post("/v1/workflows", json=body, headers=_headers(ALICE))).json()
    duplicate = await client.post("/v1/workflows", json=body, headers=_headers(ALICE))
    assert duplicate.status_code == 409
    loop = {**body, "name": "loop", "steps": [{"id": "a", "name": "A", "depends_on": ["a"]}]}
    cyclic = await client.post("/v1/workflows", json=loop, headers=_headers(ALICE))
    assert cyclic.status_code == 400 and "cycle" in cyclic.text
    bad_cursor = await client.get("/v1/workflows?after=" + "f" * 32, headers=_headers(ALICE))
    assert bad_cursor.status_code == 400

    path = f"/v1/workflows/{created['id']}"
    renamed = await client.patch(path, json={"name": "ship"}, headers=_headers(ALICE))
    assert renamed.status_code == 200
    assert (renamed.json()["name"], renamed.json()["steps"]) == ("ship", created["steps"])
    assert (await client.patch(path, json={"name": "x"}, headers=_headers(BOB))).status_code == 404
    assert (await client.delete(path, headers=_headers(BOB))).status_code == 404
    deleted = await client.delete(path, headers=_headers(ALICE))
    assert deleted.json() == {"id": created["id"], "object": "workflow.deleted", "deleted": True}
    assert (await client.get(path, headers=_headers(ALICE))).status_code == 404
