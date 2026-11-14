"""HTTP tests for ``/v1/workflows``: saving, paging, editing and ownership."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

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
from tests.server.integration.test_session_journal_routes import _runner, _session, _write

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
    found = await client.get("/v1/workflows?name=nightly-build", headers=_headers(ALICE))
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


async def test_draft_planning_is_persistent_private_and_separate_from_runs(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    created = await client.post(
        "/v1/workflows",
        json={"name": "release", "description": "Plan the next release."},
        headers=_headers(ALICE),
    )
    assert created.status_code == 200, created.text
    draft = created.json()
    assert draft["is_draft"] and draft["steps"] == []
    path = f"/v1/workflows/{draft['id']}"
    drafts = await client.get("/v1/workflows", headers=_headers(ALICE))
    assert drafts.json()["data"][0]["id"] == draft["id"]
    assert drafts.json()["data"][0]["is_draft"] is True

    planner = _session(db_uri)
    foreign = _session(db_uri, owner=BOB)
    refused = await client.patch(
        path, json={"planning_session_id": foreign}, headers=_headers(ALICE)
    )
    assert refused.status_code == 403
    missing = await client.patch(
        path, json={"planning_session_id": foreign}, headers=_headers(BOB)
    )
    assert missing.status_code == 404
    unknown = await client.patch(
        path, json={"planning_session_id": uuid4().hex}, headers=_headers(ALICE)
    )
    assert unknown.status_code == 403
    linked = await client.patch(
        path,
        json={"planning_session_id": planner, "description": "Prepare the next release."},
        headers=_headers(ALICE),
    )
    assert linked.status_code == 200, linked.text
    assert linked.json()["description"] == "Prepare the next release."
    assert (await client.get(path, headers=_headers(ALICE))).json()[
        "planning_session_id"
    ] == planner
    assert (
        await client.patch(path, json={"planning_session_id": planner}, headers=_headers(ALICE))
    ).status_code == 200

    journal = f"/v1/sessions/{planner}/journal"
    empty = await client.post(
        journal, json=_write("plan", {"steps": []}, "empty"), headers=_runner(planner)
    )
    assert empty.status_code == 400
    plan = {"name": draft["name"], "steps": STEPS}
    premature = await client.post(
        journal,
        json=_write("plan", plan, "run", workflow_id=draft["id"]),
        headers=_runner(planner),
    )
    assert premature.status_code == 400 and "workflow_is_draft" in premature.text
    proposal = await client.post(
        journal, json=_write("plan", plan, "proposal"), headers=_runner(planner)
    )
    assert proposal.status_code == 200, proposal.text
    saved = await client.patch(
        path, json={"steps": proposal.json()["payload"]["steps"]}, headers=_headers(ALICE)
    )
    assert saved.status_code == 200 and saved.json()["is_draft"] is False
    assert saved.json()["planning_session_id"] == planner
    assert (await client.get(path + "/runs", headers=_headers(ALICE))).json()["data"] == []
    listed = (await client.get("/v1/workflows", headers=_headers(ALICE))).json()["data"][0]
    assert listed["id"] == draft["id"] and listed["is_draft"] is False
    replacement = _session(db_uri)
    replaced = await client.patch(
        path,
        json={"planning_session_id": replacement},
        headers=_headers(ALICE),
    )
    assert replaced.status_code == 200 and replaced.json()["planning_session_id"] == replacement
    assert (await client.get(path, headers=_headers(ALICE))).json()[
        "planning_session_id"
    ] == replacement

    cleared = await client.patch(path, json={"steps": []}, headers=_headers(ALICE))
    assert cleared.status_code == 200 and cleared.json()["is_draft"] is True
    assert (await client.get(path, headers=_headers(ALICE))).json()["steps"] == []
    assert (await client.get("/v1/workflows", headers=_headers(ALICE))).json()["data"][0][
        "is_draft"
    ] is True
