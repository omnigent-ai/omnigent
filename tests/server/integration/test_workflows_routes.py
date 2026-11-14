"""HTTP tests for ``/v1/workflows``: saving, paging, editing and ownership."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.tools.builtins.journal import JOURNAL_TOOLS
from tests.debug_log_helpers import capture_debug_rows
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
    app = _build_app(db_uri, tmp_path, workflows=True)
    for user in (ALICE, BOB):
        _make_user(db_uri, user)
    return app


@pytest_asyncio.fixture()
async def client(
    app: FastAPI, mock_llm: ControllableMockClient, tmp_path: Path
) -> AsyncIterator[httpx.AsyncClient]:
    async with _serve(app, mock_llm, tmp_path) as client:
        yield client


@pytest.mark.parametrize("features", ["", "workflows,-workflows"])
async def test_disabling_workflows_blocks_api_and_tools_without_changing_saved_data(
    client: httpx.AsyncClient, app: FastAPI, db_uri: str, features: str
) -> None:
    created = await client.post(
        "/v1/workflows",
        json={"name": "Release", "description": "Build and test.", "steps": STEPS},
        headers=_headers(ALICE),
    )
    assert created.status_code == 200, created.text
    workflow = created.json()
    path = f"/v1/workflows/{workflow['id']}"
    session_id = _session(db_uri)
    journal = f"/v1/sessions/{session_id}/journal"

    app.state.feature_flags = resolve_feature_flags({"OMNIGENT_FEATURES": features})
    info = (await client.get("/v1/info")).json()
    assert info["features"]["workflows"] is False
    assert info["workflows"] is False and info["session_journal"] is False
    for method, url, body in (
        ("GET", "/v1/workflows", None),
        ("POST", "/v1/workflows", {"name": "Hidden", "description": "", "steps": STEPS}),
        ("GET", path, None),
        ("PATCH", path, {"name": "Changed"}),
        ("DELETE", path, None),
        ("GET", path + "/runs", None),
        ("GET", journal, None),
        ("POST", journal, _write("plan", {"steps": STEPS}, "disabled")),
        (
            "POST",
            journal + "/input-requests",
            {"step": "build", "questions": [{"question": "Continue?"}]},
        ),
        ("GET", f"/v1/scheduled-tasks/{uuid4().hex}/run-steps", None),
    ):
        response = await client.request(method, url, json=body, headers=_runner(session_id))
        assert response.status_code == 404, (method, url, response.text)
        assert response.json()["error"]["message"] == "Workflows are disabled on this server"

    for tool in JOURNAL_TOOLS:
        response = await client.post(
            f"/v1/sessions/{session_id}/mcp",
            headers=_headers(ALICE),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool.name(), "arguments": {}},
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["error"]["message"] == "Workflows are disabled on this server"

    app.state.feature_flags = resolve_feature_flags({"OMNIGENT_FEATURES": "workflows"})
    info = (await client.get("/v1/info")).json()
    assert info["features"]["workflows"] is True
    assert info["workflows"] is True and info["session_journal"] is True
    assert (await client.get(path, headers=_headers(ALICE))).json() == workflow
    assert (await client.get(journal, headers=_headers(ALICE))).json()["data"] == []
    assert len((await client.get("/v1/workflows", headers=_headers(ALICE))).json()["data"]) == 1


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


async def test_telemetry_separates_drafts_saves_and_rejected_writes(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    source = _session(db_uri)
    token = "workflow-test-runner-token"
    assert SqlAlchemyConversationStore(db_uri).set_runner_id(source, token_bound_runner_id(token))
    with capture_debug_rows("server") as rows:
        draft = await client.post(
            "/v1/workflows",
            json={"name": "private-draft", "description": "Private description"},
            headers=_headers(ALICE),
        )
        assert draft.status_code == 200, draft.text
        workflow_id = draft.json()["id"]
        path = f"/v1/workflows/{workflow_id}"
        metadata = await client.patch(
            path, json={"description": "private-description"}, headers=_headers(ALICE)
        )
        assert metadata.status_code == 200, metadata.text
        assert [
            row["event_name"] for row in rows if row["event_name"].startswith("workflow_")
        ] == ["workflow_created"]
        for _ in range(2):
            saved = await client.patch(path, json={"steps": STEPS}, headers=_headers(ALICE))
            assert saved.status_code == 200, saved.text
        refused = await client.patch(path, json={"steps": STEPS}, headers=_headers(BOB))
        assert refused.status_code == 404
        duplicate = await client.post(
            "/v1/workflows",
            json={"name": "private-draft", "description": "Private description"},
            headers=_headers(ALICE),
        )
        assert duplicate.status_code == 409
        invalid = await client.patch(
            path, json={"steps": [{"id": "INVALID"}]}, headers=_headers(ALICE)
        )
        assert invalid.status_code in (400, 422)
        ready = await client.post(
            "/v1/workflows",
            json={"name": "private-ready", "description": "Private description", "steps": STEPS},
            headers={**_runner(source), RUNNER_TUNNEL_TOKEN_HEADER: token},
        )
        assert ready.status_code == 200, ready.text

    events = [row for row in rows if row["event_name"].startswith("workflow_")]
    created = [row for row in events if row["event_name"] == "workflow_created"]
    saves = [row for row in events if row["event_name"] == "workflow_saved"]
    assert len(created) == 2 and len(saves) == 3
    assert [row["attributes"]["has_plan"] for row in created] == ["False", "True"]
    assert [row["attributes"]["creation_source"] for row in created] == ["api", "agent"]
    assert created[1]["session_id"] == source
    # Repeated saves/edits of the same workflow do not inflate the adoption count.
    assert {row["attributes"]["workflow_id"] for row in saves} == {
        workflow_id,
        ready.json()["id"],
    }
    assert {row["user_id"] for row in events} == {ALICE}
    assert "private-" not in str(events)


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
