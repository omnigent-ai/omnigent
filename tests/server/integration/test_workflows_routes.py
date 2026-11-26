"""HTTP tests for ``/v1/workflows``: saving, paging, editing and ownership."""

from __future__ import annotations

import random
import string
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.db.db_models import MAX_STORED_VALUE_BYTES
from omnigent.entities.session_journal import JournalEntryTooLargeError
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.server.routes._sessions.common import _session_sandbox_status_cache
from omnigent.server.routes.workflow_permissions import UNSHAREABLE_WORKFLOW_MESSAGE
from omnigent.server.schemas import SandboxStatus
from omnigent.server.sharing_settings import SharingMode
from omnigent.server.startup_inputs import STARTUP_LABEL
from omnigent.stores.conversation_store import (
    CREATED_BY_SESSION_LABEL_KEY,
    SCHEDULED_TASK_ORIGIN_LABEL_KEY,
    SCHEDULED_TASK_RUN_LABEL_KEY,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.session_journal_store import SessionJournalStore
from omnigent.stores.workflow_store import WorkflowStore
from omnigent.tools.builtins.journal import JOURNAL_TOOLS
from tests.debug_log_helpers import capture_debug_rows
from tests.server.conftest import ControllableMockClient
from tests.server.helpers import create_test_agent
from tests.server.integration.test_scheduled_tasks_routes import (
    _build_app,
    _create_body,
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


async def test_session_creation_snapshots_workflow_before_initial_items(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    agent = await create_test_agent(client, user=ALICE)
    saved = await client.post(
        "/v1/workflows",
        json={"name": "Release", "description": "Build and test.", "steps": STEPS},
        headers=_headers(),
    )
    assert saved.status_code == 200, saved.text
    workflow = saved.json()
    created = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "workflow_id": workflow["id"],
            "initial_items": [
                {
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Run this workflow."}],
                    },
                }
            ],
        },
        headers=_headers(),
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    journal = f"/v1/sessions/{session_id}/journal"
    entries = (await client.get(journal, headers=_headers())).json()["data"]
    assert [entry["kind"] for entry in entries] == ["plan_status", "plan"]
    plan = entries[1]
    assert plan["author"] == {"type": "omnigent"}
    assert plan["workflow_id"] == workflow["id"]
    content = dict(plan["content"])
    assert isinstance(content.pop("graph_revision"), str)
    assert content == {
        "name": workflow["name"],
        "steps": workflow["steps"],
        "version": 1,
        "source": "workflow",
    }
    conversations = SqlAlchemyConversationStore(db_uri)
    items = conversations.list_items(session_id, order="asc").data
    assert [item.type for item in items] == ["resource_event", "message"]
    assert items[0].data.event_type == "session.journal.plan"
    runs_url = f"/v1/workflows/{workflow['id']}/runs"
    runs = (await client.get(runs_url, headers=_headers())).json()["data"]
    assert len(runs) == 1
    assert runs[0]["session_id"] == session_id and runs[0]["id"] == plan["id"]

    # The agent can start the existing steps without writing another plan.
    conversations.set_runner_id(session_id, token_bound_runner_id("journal-test-runner"))
    started = await client.post(
        journal,
        json=_write("status", {"status": "started"}, "start", step="build"),
        headers=_runner(session_id),
    )
    assert started.status_code == 200, started.text

    changed = await client.patch(
        f"/v1/workflows/{workflow['id']}",
        json={"steps": [{"id": "deploy", "name": "Deploy"}]},
        headers=_headers(),
    )
    assert changed.status_code == 200, changed.text
    plans = (await client.get(journal, params={"kind": "plan"}, headers=_headers())).json()["data"]
    assert plans == [plan]
    next_run = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "workflow_id": workflow["id"]},
        headers=_headers(),
    )
    assert next_run.status_code == 201, next_run.text
    next_journal = f"/v1/sessions/{next_run.json()['id']}/journal"
    next_plan = (await client.get(next_journal, headers=_headers())).json()["data"][1]
    assert next_plan["content"]["steps"] == changed.json()["steps"]
    assert len((await client.get(runs_url, headers=_headers())).json()["data"]) == 2


async def test_a_run_is_titled_for_its_workflow_until_someone_renames_it(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    agent = await create_test_agent(client, user=ALICE)
    workflow = await _save_workflow(client, "Blah borgus")
    opening = f'"{workflow["name"]}" (ID {workflow["id"]}).'

    def message(text: str) -> dict[str, Any]:
        # As a native harness's transcript records the message the session page sent.
        content = [{"type": "input_text", "text": text}]
        item_data = {"role": "user", "content": content}
        return {
            "type": "external_conversation_item",
            "data": {"item_type": "message", "item_data": item_data},
        }

    async def create(first_message: str | None = None, **body: Any) -> str:
        response = await client.post(
            "/v1/sessions", json={"agent_id": agent["id"], **body}, headers=_headers()
        )
        assert response.status_code == 201, response.text
        session_id = response.json()["id"]
        if first_message is not None:
            sent = await client.post(
                f"/v1/sessions/{session_id}/events",
                json=message(first_message),
                headers=_headers(),
            )
            assert sent.status_code == 202, sent.text
        return session_id

    # The Workflows page passes workflow_id, as does an agent's sys_session_create.
    page_run = await create(
        f"Use the workflow skill to run the saved workflow {opening}", workflow_id=workflow["id"]
    )
    # The composer's workflow picker sends only the workflow skill's message.
    picked_run = await create(f"/workflow Run the saved workflow {opening}\n\nThe upload tests")
    named_run = await create(workflow_id=workflow["id"], title="Hotfix release")
    # A first message that reads like the workflow's name doesn't make the title a seed.
    echoed_run = await create(workflow["name"], workflow_id=workflow["id"])
    # A child's title is its sub-agent name, unique under its parent, so children keep theirs.
    children = [await create(workflow_id=workflow["id"], parent_session_id=page_run) for _ in "ab"]
    conversations = SqlAlchemyConversationStore(db_uri)

    def title(session_id: str) -> str | None:
        conversation = conversations.get_conversation(session_id)
        assert conversation is not None
        return conversation.title

    assert [title(run) for run in (page_run, picked_run, named_run, echoed_run)] == [
        "Blah borgus",
        "Blah borgus",
        "Hotfix release",
        "Blah borgus",
    ]
    assert [title(child) for child in children] == [f"untitled:{child}" for child in children]

    # A harness's automatic title replaces only a seed taken from the message itself.
    for run in (page_run, picked_run, echoed_run):
        declined = await client.post(
            f"/v1/sessions/{run}/auto-title",
            json={"title": "Run Saved Workflow"},
            headers=_headers(),
        )
        assert declined.status_code == 200, declined.text
        assert declined.json()["reason"] == "title_changed"
    renamed = await client.patch(
        f"/v1/sessions/{picked_run}", json={"title": "My rename"}, headers=_headers()
    )
    assert renamed.status_code == 200, renamed.text
    assert title(picked_run) == "My rename"


@pytest.mark.parametrize("too_large", [False, True])
async def test_workflow_journal_failure_removes_the_unreturned_session(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    too_large: bool,
) -> None:
    agent = await create_test_agent(client, user=ALICE)
    saved = await client.post(
        "/v1/workflows",
        json={"name": "Release", "description": "Build and test.", "steps": STEPS},
        headers=_headers(),
    )
    assert saved.status_code == 200, saved.text
    conversations = SqlAlchemyConversationStore(db_uri)
    before = [session.id for session in conversations.list_conversations().data]

    # Inject a journal storage failure after the conversation row has been created.
    def unavailable(*args: object, **kwargs: object) -> None:
        if too_large:
            raise JournalEntryTooLargeError(
                "the stored entry", MAX_STORED_VALUE_BYTES + 1, MAX_STORED_VALUE_BYTES
            )
        raise RuntimeError("journal storage unavailable")

    monkeypatch.setattr(SessionJournalStore, "append", unavailable)
    if too_large:
        response = await client.post(
            "/v1/sessions",
            json={"agent_id": agent["id"], "workflow_id": saved.json()["id"]},
            headers=_headers(),
        )
        assert response.status_code == 400 and "entry_too_large" in response.text
    else:
        with pytest.raises(RuntimeError, match="journal storage unavailable"):
            await client.post(
                "/v1/sessions",
                json={"agent_id": agent["id"], "workflow_id": saved.json()["id"]},
                headers=_headers(),
            )
    assert [session.id for session in conversations.list_conversations().data] == before


@pytest.mark.parametrize(
    "case", ["foreign", "missing", "invalid", "draft", "disabled", "oversized"]
)
async def test_invalid_workflow_creates_no_session(
    client: httpx.AsyncClient, app: FastAPI, db_uri: str, case: str
) -> None:
    agent = await create_test_agent(client, user=ALICE)
    steps = [] if case == "draft" else STEPS
    if case == "oversized":
        # The saved steps fit; adding the initial plan's envelope exceeds the journal cap.
        noise = random.Random(1)
        steps = [
            {
                "id": f"s{index}",
                "name": f"Step {index}",
                "goal": "".join(noise.choices(string.ascii_letters + string.digits, k=235)),
            }
            for index in range(90)
        ]
    saved = await client.post(
        "/v1/workflows",
        json={
            "name": "Near limit" if case == "oversized" else "Release",
            "description": "Build and test.",
            "steps": steps,
        },
        headers=_headers(BOB if case == "foreign" else ALICE),
    )
    assert saved.status_code == 200, saved.text
    workflow_id = saved.json()["id"]
    if case == "missing":
        workflow_id = uuid4().hex
    elif case == "invalid":
        workflow_id = "not-a-workflow-id"
    elif case == "disabled":
        app.state.feature_flags = resolve_feature_flags({"OMNIGENT_FEATURES": "-workflows"})
    conversations = SqlAlchemyConversationStore(db_uri)
    before = [session.id for session in conversations.list_conversations().data]
    response = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "workflow_id": workflow_id},
        headers=_headers(),
    )
    assert response.status_code == (404 if case == "disabled" else 400), response.text
    if case == "oversized":
        assert "entry_too_large" in response.text
    assert [session.id for session in conversations.list_conversations().data] == before


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
    assert info["workflow_sharing"] is False
    for method, url, body in (
        ("GET", "/v1/workflows", None),
        ("POST", "/v1/workflows", {"name": "Hidden", "description": "", "steps": STEPS}),
        ("GET", "/v1/workflows/shared", None),
        ("GET", path + "/permissions", None),
        ("PUT", path + "/permissions", {"user_id": BOB, "level": LEVEL_READ}),
        ("DELETE", path + f"/permissions/{BOB}", None),
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
    assert info["workflow_sharing"] is True
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

    created = await client.post(
        "/v1/workflows", json={**body, "name": "weekly"}, headers=_headers(ALICE)
    )
    assert created.status_code == 200, created.text
    page = (await client.get("/v1/workflows?limit=1", headers=_headers(ALICE))).json()
    cursor = page["next_cursor"]
    renamed = await client.patch(
        f"/v1/workflows/{workflow['id']}", json={"name": "zz-build"}, headers=_headers(ALICE)
    )
    assert renamed.status_code == 200, renamed.text
    following = await client.get(
        "/v1/workflows", params={"limit": 1, "after": cursor}, headers=_headers(ALICE)
    )
    assert [item["name"] for item in following.json()["data"]] == ["weekly"]
    deleted = await client.delete(f"/v1/workflows/{workflow['id']}", headers=_headers(ALICE))
    assert deleted.status_code == 200, deleted.text
    following = await client.get(
        "/v1/workflows", params={"after": cursor}, headers=_headers(ALICE)
    )
    assert [item["name"] for item in following.json()["data"]] == ["weekly"]
    assert following.json()["next_cursor"] is None


async def test_telemetry_separates_drafts_saves_and_rejected_writes(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    source = _session(db_uri, runner_token=None)
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
    # Save plan names the description it replaces, so one written meanwhile stays.
    for expected, kept in [
        ("Plan the next release.", "Prepare the next release."),
        ("Prepare the next release.", "Ship the next release."),
    ]:
        saved = await client.patch(
            path,
            json={"description": "Ship the next release.", "expected_description": expected},
            headers=_headers(ALICE),
        )
        assert saved.status_code == 200 and saved.json()["description"] == kept
    # The proposal a description came from stays until the description is written otherwise.
    source = uuid4().hex
    proposed = await client.patch(
        path,
        json={"description": "Ship it.", "description_source": source},
        headers=_headers(ALICE),
    )
    assert proposed.json()["description_source"] == source
    edited = await client.patch(path, json={"description": "Ship it."}, headers=_headers(ALICE))
    assert edited.json()["description_source"] is None
    malformed = await client.patch(
        path, json={"description": "Ship.", "description_source": "x"}, headers=_headers(ALICE)
    )
    assert malformed.status_code == 400
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
        path, json={"steps": proposal.json()["content"]["steps"]}, headers=_headers(ALICE)
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


async def _save_workflow(client: httpx.AsyncClient, name: str) -> dict[str, str]:
    saved = await client.post(
        "/v1/workflows",
        json={"name": name, "description": f"{name}.", "steps": STEPS},
        headers=_headers(),
    )
    assert saved.status_code == 200, saved.text
    return saved.json()


async def _run(
    client: httpx.AsyncClient, session_id: str, workflow: dict[str, str], key: str
) -> None:
    """Start a run of *workflow* in *session_id*, as its agent does."""
    response = await client.post(
        f"/v1/sessions/{session_id}/journal",
        json=_write(
            "plan", {"name": workflow["name"], "steps": STEPS}, key, workflow_id=workflow["id"]
        ),
        headers=_runner(session_id),
    )
    assert response.status_code == 200, response.text


async def test_runs_list_the_workflow_runs_their_sessions_started(
    client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [100]
    for module in ("session_journal_store", "conversation_store.sqlalchemy_store"):
        monkeypatch.setattr(f"omnigent.stores.{module}.now_epoch", lambda: clock[0])
    stack = await _save_workflow(client, "Land a stack")
    ready = await _save_workflow(client, "Ready a PR")
    conversations = SqlAlchemyConversationStore(db_uri)

    def start(starter: str, title: str, at: int) -> str:
        # As sys_session_create's same-host placement records an independent session.
        clock[0] = at
        session_id = _session(db_uri, title=title, labels={CREATED_BY_SESSION_LABEL_KEY: starter})
        conversations.add_new_sibling(starter, session_id, user_id=ALICE)
        return session_id

    root = _session(db_uri, title="Land stack")
    await _run(client, root, stack, "first-run")
    first = start(root, "Ready #1", 110)
    await _run(client, first, ready, "ready-1")
    deeper = start(first, "Ready #1 fix", 120)
    await _run(client, deeper, ready, "ready-1-fix")
    unrun = start(root, "Plain worker", 125)
    second = start(root, "Ready #2", 130)
    await _run(client, second, ready, "ready-2")
    # A session's later runs aren't what its starter began it for.
    clock[0] = 140
    await _run(client, first, ready, "ready-1-again")
    later_fix = start(first, "Ready #1 later fix", 150)
    await _run(client, later_fix, ready, "ready-1-later-fix")
    # An automation's next occurrence starts a run without naming a workflow.
    clock[0] = 200
    conversations.set_labels(root, {SCHEDULED_TASK_RUN_LABEL_KEY: "next-occurrence"})
    noted = await client.post(
        f"/v1/sessions/{root}/journal",
        json=_write("note", {"body": "Next occurrence"}, "next-occurrence"),
        headers=_runner(root),
    )
    assert noted.status_code == 200, noted.text
    start(root, "Ready #3", 210)

    url = f"/v1/workflows/{stack['id']}/runs"
    (run,) = (await client.get(url, headers=_headers())).json()["data"]
    # Each run below another follows the session that started it, and the
    # session's next run, named or not, ends the sessions that count as its own,
    # at every level: "Ready #1 later fix" belongs to Ready #1's later run.
    assert [below["title"] for below in run["descendant_runs"]] == [
        "Ready #1",
        "Ready #1 fix",
        "Ready #2",
    ]
    assert run["descendant_run_count"] == 3 and run["descendant_runs_cursor"] is None
    assert run["descendant_runs_active"] is False
    # Workflow names come from the server, so ownership doesn't depend on a loaded list.
    assert run["workflow_name"] == "Land a stack"
    assert {below["workflow_name"] for below in run["descendant_runs"]} == {"Ready a PR"}
    assert len(run["descendant_runs"]) == 3 and unrun not in {
        below["session_id"] for below in run["descendant_runs"]
    }
    assert run["started_by"] is None and run["workflow_id"] == stack["id"]
    assert [below["started_by"] for below in run["descendant_runs"]] == [
        {"session_id": root, "title": "Land stack"},
        {"session_id": first, "title": "Ready #1"},
        {"session_id": root, "title": "Land stack"},
    ]
    assert all(
        below["workflow_id"] == ready["id"] and below["descendant_runs"] == []
        for below in run["descendant_runs"]
    )

    # The called workflow's runs name who started them and list the runs below them.
    called = (await client.get(f"/v1/workflows/{ready['id']}/runs", headers=_headers())).json()
    first_runs = [run for run in called["data"] if run["session_id"] == first]
    again, original = first_runs
    assert original["started_by"] == {"session_id": root, "title": "Land stack"}
    assert [below["session_id"] for below in original["descendant_runs"]] == [deeper]
    assert [below["session_id"] for below in again["descendant_runs"]] == [later_fix]

    # Someone the session is shared with can't read its starter or the runs below it.
    SqlAlchemyPermissionStore(db_uri).grant(BOB, second, LEVEL_READ)
    shared = await client.get(
        f"/v1/workflows/{ready['id']}/runs", params={"session_id": second}, headers=_headers(BOB)
    )
    assert shared.status_code == 200, shared.text
    (viewed,) = shared.json()["data"]
    assert viewed["session_id"] == second and viewed["started_by"] is None
    assert viewed["workflow_name"] is None


async def test_a_run_an_automation_started_names_the_automation_to_whoever_may_see_it(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    workflow = await _save_workflow(client, "Release")
    created = await client.post(
        "/v1/scheduled-tasks",
        json=_create_body(
            host_id=None,
            workspace=None,
            name="Nightly release",
            prompt=f'/workflow Run the saved workflow "Release" (ID {workflow["id"]}).',
        ),
        headers=_headers(),
    )
    assert created.status_code == 200, created.text
    task_id = created.json()["id"]
    # A scheduled run's session records the automation that started it.
    scheduled = _session(db_uri, labels={SCHEDULED_TASK_ORIGIN_LABEL_KEY: task_id})
    manual = _session(db_uri)
    for session_id in (scheduled, manual):
        await _run(client, session_id, workflow, session_id)
    await _share(client, workflow["id"])
    SqlAlchemyPermissionStore(db_uri).grant(BOB, scheduled, LEVEL_READ)

    runs_url = f"/v1/workflows/{workflow['id']}/runs"
    alices = (await client.get(runs_url, headers=_headers())).json()["data"]
    assert {run["session_id"]: run["automation"] for run in alices} == {
        scheduled: {"id": task_id, "name": "Nightly release"},
        manual: None,
    }
    # Bob reads the session, but the automation isn't shared with him.
    (bobs,) = (await client.get(runs_url, headers=_headers(BOB))).json()["data"]
    assert (bobs["session_id"], bobs["automation"]) == (scheduled, None)


async def test_a_runs_descendants_continue_page_by_page_in_order(
    client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omnigent.server.workflows._DESCENDANT_RUNS_PAGE", 2)
    clock = [100]
    for module in ("session_journal_store", "conversation_store.sqlalchemy_store"):
        monkeypatch.setattr(f"omnigent.stores.{module}.now_epoch", lambda: clock[0])
    stack = await _save_workflow(client, "Land a stack")
    ready = await _save_workflow(client, "Ready a PR")
    conversations = SqlAlchemyConversationStore(db_uri)

    async def start(starter: str, title: str, at: int) -> str:
        clock[0] = at
        session_id = _session(db_uri, title=title, labels={CREATED_BY_SESSION_LABEL_KEY: starter})
        conversations.add_new_sibling(starter, session_id, user_id=ALICE)
        await _run(client, session_id, ready, title)
        return session_id

    root = _session(db_uri, title="Land stack")
    await _run(client, root, stack, "root-run")
    first = await start(root, "Ready #1", 110)
    fix = await start(first, "Ready #1 fix", 120)
    waiting = await start(root, "Ready #2", 130)
    asked = await client.post(
        f"/v1/sessions/{waiting}/journal",
        json=_write("status", {"status": "needs_input", "text": "Which PR?"}, "ask", step="build"),
        headers=_runner(waiting),
    )
    assert asked.status_code == 200, asked.text
    titles = ["Ready #1", "Ready #1 fix", "Ready #2"]

    (run,) = (await client.get(f"/v1/workflows/{stack['id']}/runs", headers=_headers())).json()[
        "data"
    ]
    assert run["descendant_run_count"] == 3
    assert [below["title"] for below in run["descendant_runs"]] == titles[:2]
    assert run["descendant_runs_cursor"] == fix
    # A run waiting past the first page still says the list has work going on.
    assert run["descendant_runs_active"] is True
    url = f"/v1/workflows/{stack['id']}/runs/{run['id']}/descendant_runs"
    rest = await client.get(
        url,
        params={"session_id": root, "after": run["descendant_runs_cursor"]},
        headers=_headers(),
    )
    assert rest.status_code == 200, rest.text
    (last,) = rest.json()["data"]
    assert last["title"] == "Ready #2" and rest.json()["next_cursor"] is None
    assert last["started_by"] == {"session_id": root, "title": "Land stack"}
    assert last["workflow_name"] == "Ready a PR"

    paged: list[str] = []
    cursor: str | None = None
    while True:
        params = {"session_id": root, "limit": 1, **({"after": cursor} if cursor else {})}
        page = (await client.get(url, params=params, headers=_headers())).json()
        paged += [below["title"] for below in page["data"]]
        if (cursor := page["next_cursor"]) is None:
            break
    assert paged == titles

    refused = await client.get(
        url, params={"session_id": root, "after": uuid4().hex}, headers=_headers()
    )
    assert refused.status_code == 400, refused.text
    for missing in (
        f"/v1/workflows/{ready['id']}/runs/{run['id']}/descendant_runs",
        f"/v1/workflows/{stack['id']}/runs/{uuid4().hex}/descendant_runs",
    ):
        response = await client.get(missing, params={"session_id": root}, headers=_headers())
        assert response.status_code == 404, missing
    # Reading the run's session stands in for owning the workflow; the sessions
    # below it stay Alice's own.
    assert (
        await client.get(url, params={"session_id": root}, headers=_headers(BOB))
    ).status_code == 404
    SqlAlchemyPermissionStore(db_uri).grant(BOB, root, LEVEL_READ)
    shared = await client.get(url, params={"session_id": root}, headers=_headers(BOB))
    assert shared.status_code == 200, shared.text
    assert shared.json() == {**shared.json(), "data": [], "next_cursor": None}


async def test_a_session_filter_lists_its_current_run_for_anyone_who_can_read_it(
    client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = await _save_workflow(client, "Land a stack")
    other = await _save_workflow(client, "Ready a PR")
    shared = _session(db_uri, title="Shared")
    await _run(client, shared, stack, "shared-run")
    await _run(client, shared, stack, "shared-run-again")
    moved_on = _session(db_uri, title="Moved on")
    await _run(client, moved_on, stack, "moved-on-run")
    await _run(client, moved_on, other, "other-run")
    private = _session(db_uri, title="Private")
    await _run(client, private, stack, "private-run")
    SqlAlchemyPermissionStore(db_uri).grant(BOB, shared, LEVEL_READ)
    url = f"/v1/workflows/{stack['id']}/runs"

    def run_ids(response: httpx.Response) -> list[str]:
        assert response.status_code == 200, response.text
        return [run["id"] for run in response.json()["data"]]

    listed = (await client.get(url, headers=_headers())).json()["data"]
    # Runs in one session list newest first; the newer one is the session's current run.
    current = next(run["id"] for run in listed if run["session_id"] == shared)
    own = run_ids(await client.get(url, params={"session_id": shared}, headers=_headers()))
    assert own == [current]
    moved = await client.get(url, params={"session_id": moved_on}, headers=_headers())
    assert run_ids(moved) == []
    # Bob doesn't own the workflow, so only the session he can read lists its run.
    assert (await client.get(url, headers=_headers(BOB))).status_code == 404
    read: list[str] = []
    get_many = WorkflowStore.get_many

    def recording_get_many(self: WorkflowStore, workflow_ids: list[str]) -> object:
        read.extend(workflow_ids)
        return get_many(self, workflow_ids)

    monkeypatch.setattr(WorkflowStore, "get_many", recording_get_many)
    bobs = await client.get(url, params={"session_id": shared}, headers=_headers(BOB))
    assert run_ids(bobs) == own
    # Nor is it shared with him, so naming the run never reads the workflow.
    assert bobs.json()["data"][0]["workflow_name"] is None
    assert stack["id"] not in read
    for session_id in (private, uuid4().hex, "not-a-session"):
        refused = await client.get(url, params={"session_id": session_id}, headers=_headers(BOB))
        assert refused.status_code == 404, session_id
    # An automation's next occurrence ends the run before it writes anything.
    SqlAlchemyConversationStore(db_uri).set_labels(
        shared, {SCHEDULED_TASK_RUN_LABEL_KEY: "next-occurrence"}
    )
    assert run_ids(await client.get(url, params={"session_id": shared}, headers=_headers())) == []


async def test_each_family_lists_the_runs_below_it_up_to_its_own_cap(
    client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omnigent.server.workflows.MAX_SESSION_FAMILY_MEMBERS", 2)
    stack = await _save_workflow(client, "Land a stack")
    ready = await _save_workflow(client, "Ready a PR")
    conversations = SqlAlchemyConversationStore(db_uri)
    roots = []
    for name in ("First", "Second"):
        root = _session(db_uri, title=f"{name} stack")
        await _run(client, root, stack, f"{name}-run")
        for number in range(2):
            worker = _session(
                db_uri, title=f"{name} #{number}", labels={CREATED_BY_SESSION_LABEL_KEY: root}
            )
            conversations.add_new_sibling(root, worker, user_id=ALICE)
            await _run(client, worker, ready, f"{name}-{number}")
        roots.append(root)

    listed = (await client.get(f"/v1/workflows/{stack['id']}/runs", headers=_headers())).json()
    # Together the families pass the cap, but neither does alone.
    assert {
        run["session_id"]: run["descendant_run_count"] for run in listed["data"]
    } == dict.fromkeys(roots, 2)


async def test_a_run_whose_session_waits_on_its_sandbox_is_launching(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    stack = await _save_workflow(client, "Land a stack")
    ready = await _save_workflow(client, "Ready a PR")
    conversations = SqlAlchemyConversationStore(db_uri)
    root = _session(db_uri, title="Land stack")
    await _run(client, root, stack, "land")
    worker = _session(db_uri, title="Ready #1", labels={CREATED_BY_SESSION_LABEL_KEY: root})
    conversations.add_new_sibling(root, worker, user_id=ALICE)
    await _run(client, worker, ready, "ready-1")

    async def runs(workflow: dict[str, str]) -> list[dict[str, Any]]:
        listed = await client.get(f"/v1/workflows/{workflow['id']}/runs", headers=_headers())
        assert listed.status_code == 200, listed.text
        return listed.json()["data"]

    async def worker_run() -> dict[str, Any]:
        (run,) = await runs(ready)
        return run

    assert (await worker_run())["sandbox_launching"] is False
    # Every replica reads the startup label a sandbox launch keeps on its session.
    conversations.set_labels(worker, {STARTUP_LABEL: "pending"})
    launching = await worker_run()
    assert launching["sandbox_launching"] is True and launching["session_status"] == "idle"
    (root_run,) = await runs(stack)
    assert root_run["descendant_runs_active"] is True
    try:
        # The launching replica's stage comes first, so its failure ends the launch.
        _session_sandbox_status_cache[worker] = SandboxStatus(stage="failed", error="no capacity")
        assert (await worker_run())["sandbox_launching"] is False
        _session_sandbox_status_cache[worker] = SandboxStatus(stage="provisioning")
        conversations.set_labels(worker, {STARTUP_LABEL: "failed"})
        assert (await worker_run())["sandbox_launching"] is True
    finally:
        _session_sandbox_status_cache.pop(worker, None)
    # A launch that failed elsewhere, or a session whose sandbox is up, isn't launching.
    assert (await worker_run())["sandbox_launching"] is False
    conversations.delete_label(worker, STARTUP_LABEL)
    assert (await worker_run())["sandbox_launching"] is False
    (root_run,) = await runs(stack)
    assert root_run["descendant_runs_active"] is False

    # Only the session's current run reads its launch.
    conversations.set_labels(worker, {STARTUP_LABEL: "pending"})
    await _run(client, worker, ready, "ready-2")
    current, earlier = await runs(ready)
    assert current["is_current"] and current["sandbox_launching"] is True
    assert earlier["sandbox_launching"] is False


# ── Sharing ─────────────────────────────────────────────────────────────────


async def _share(client: httpx.AsyncClient, workflow_id: str, user_id: str = BOB) -> None:
    granted = await client.put(
        f"/v1/workflows/{workflow_id}/permissions",
        json={"user_id": user_id, "level": LEVEL_READ},
        headers=_headers(ALICE),
    )
    assert granted.status_code == 200, granted.text


async def test_a_shared_workflow_is_read_only_for_the_people_it_is_shared_with(
    client: httpx.AsyncClient,
) -> None:
    workflow = await _save_workflow(client, "Release")
    path = f"/v1/workflows/{workflow['id']}"
    assert (workflow["viewer_role"], workflow["shareable"]) == ("owner", True)
    assert (await client.get(path, headers=_headers(BOB))).status_code == 404

    granted = await client.put(
        path + "/permissions", json={"user_id": BOB, "level": LEVEL_READ}, headers=_headers(ALICE)
    )
    assert granted.status_code == 200, granted.text
    assert granted.json() == {
        "user_id": BOB,
        "workflow_id": workflow["id"],
        "level": LEVEL_READ,
        "display_name": BOB,
    }
    listed = (await client.get(path + "/permissions", headers=_headers(ALICE))).json()
    assert listed == {"permissions": [granted.json()], "next_cursor": None}

    seen = (await client.get(path, headers=_headers(BOB))).json()
    assert (seen["viewer_role"], seen["owner_display_name"]) == ("viewer", ALICE)
    assert seen["steps"] == workflow["steps"]
    shared = (await client.get("/v1/workflows/shared", headers=_headers(BOB))).json()
    assert [
        (item["id"], item["viewer_role"], item["owner_display_name"]) for item in shared["data"]
    ] == [(workflow["id"], "viewer", ALICE)]
    assert shared["next_cursor"] is None
    # Bob's own list and alice's shared list are unchanged.
    assert (await client.get("/v1/workflows", headers=_headers(BOB))).json()["data"] == []
    assert (await client.get("/v1/workflows/shared", headers=_headers(ALICE))).json()["data"] == []

    # Every change, and who it is shared with, stays with its owner.
    for method, url, body in (
        ("PATCH", path, {"name": "Changed"}),
        ("DELETE", path, None),
        ("GET", path + "/permissions", None),
        ("PUT", path + "/permissions", {"user_id": "carol@example.com", "level": LEVEL_READ}),
        ("DELETE", path + f"/permissions/{BOB}", None),
    ):
        response = await client.request(method, url, json=body, headers=_headers(BOB))
        assert response.status_code == 404, (method, url, response.text)
    assert (await client.get(path, headers=_headers(ALICE))).json()["name"] == "Release"

    for _ in range(2):
        revoked = await client.delete(path + f"/permissions/{BOB}", headers=_headers(ALICE))
        assert revoked.status_code == 204, revoked.text
    assert (await client.get(path, headers=_headers(BOB))).status_code == 404
    assert (await client.get("/v1/workflows/shared", headers=_headers(BOB))).json()["data"] == []


@pytest.mark.parametrize(
    ("user_id", "level"),
    [
        (ALICE, LEVEL_READ),
        (RESERVED_USER_PUBLIC, LEVEL_READ),
        (RESERVED_USER_LOCAL, LEVEL_READ),
        (BOB, LEVEL_EDIT),
        ("  ", LEVEL_READ),
        ("x" * 129, LEVEL_READ),
    ],
    ids=["owner", "public", "local", "edit", "blank", "over-wide"],
)
async def test_a_workflow_is_shared_only_read_only_with_someone_else(
    client: httpx.AsyncClient, user_id: str, level: int
) -> None:
    workflow = await _save_workflow(client, "Release")
    path = f"/v1/workflows/{workflow['id']}/permissions"
    refused = await client.put(
        path, json={"user_id": user_id, "level": level}, headers=_headers(ALICE)
    )
    assert refused.status_code == 400, refused.text
    assert (await client.get(path, headers=_headers(ALICE))).json()["permissions"] == []


async def test_agents_read_a_shared_workflow_by_id_but_not_who_it_is_shared_with(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    workflow = await _save_workflow(client, "Release")
    path = f"/v1/workflows/{workflow['id']}"
    await _share(client, workflow["id"])
    bobs_agent = _runner(_session(db_uri, owner=BOB), BOB)

    # A run of the shared workflow loads its description.
    read = await client.get(path, headers=bobs_agent)
    assert read.status_code == 200, read.text
    assert read.json()["viewer_role"] == "viewer"
    # Lookups by name and lists keep to the caller's own workflows.
    by_name = await client.get("/v1/workflows", params={"name": "Release"}, headers=bobs_agent)
    assert by_name.json()["data"] == []
    assert (await client.get("/v1/workflows/shared", headers=bobs_agent)).status_code == 403
    alices_agent = _runner(_session(db_uri))
    for method, url, body in (
        ("GET", path + "/permissions", None),
        ("PUT", path + "/permissions", {"user_id": "carol@example.com", "level": LEVEL_READ}),
        ("DELETE", path + f"/permissions/{BOB}", None),
    ):
        response = await client.request(method, url, json=body, headers=alices_agent)
        assert response.status_code == 403, (method, url, response.text)
    assert (
        len(
            (await client.get(path + "/permissions", headers=_headers(ALICE))).json()[
                "permissions"
            ]
        )
        == 1
    )


async def test_people_a_workflow_is_shared_with_run_it_and_keep_their_runs_private(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    workflow = await _save_workflow(client, "Release")
    path = f"/v1/workflows/{workflow['id']}"
    await _share(client, workflow["id"])
    agent = await create_test_agent(client, user=BOB)
    run = {"agent_id": agent["id"], "workflow_id": workflow["id"]}

    created = await client.post("/v1/sessions", json=run, headers=_headers(BOB))
    assert created.status_code == 201, created.text
    started = created.json()["id"]
    entries = (await client.get(f"/v1/sessions/{started}/journal", headers=_headers(BOB))).json()
    assert entries["data"][-1]["workflow_id"] == workflow["id"]
    # An agent's plan in another of bob's sessions can name it too.
    chat = _session(db_uri, owner=BOB)
    written = await client.post(
        f"/v1/sessions/{chat}/journal",
        json=_write(
            "plan", {"name": "Release", "steps": STEPS}, "run", workflow_id=workflow["id"]
        ),
        headers=_runner(chat, BOB),
    )
    assert written.status_code == 200, written.text

    bobs = (await client.get(path + "/runs", headers=_headers(BOB))).json()["data"]
    assert {item["session_id"] for item in bobs} == {started, chat}
    assert {item["workflow_name"] for item in bobs} == {"Release"}
    assert (await client.get(path + "/runs", headers=_headers(ALICE))).json()["data"] == []

    # Unshared, it can't start another run; the runs bob made stay his.
    assert (
        await client.delete(path + f"/permissions/{BOB}", headers=_headers(ALICE))
    ).status_code == 204
    again = await client.post("/v1/sessions", json=run, headers=_headers(BOB))
    assert again.status_code == 400, again.text
    assert "workflow_not_found" in again.json()["error"]["message"]
    assert (
        await client.get(f"/v1/sessions/{started}/journal", headers=_headers(BOB))
    ).status_code == 200


async def test_a_deleted_owners_workflow_stops_being_shared_before_its_cleanup_ends(
    client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server import accounts_store as accounts_module
    from omnigent.server.accounts_store import SqlAlchemyAccountStore

    workflow = await _save_workflow(client, "Release")
    await _share(client, workflow["id"])
    agent = await create_test_agent(client, user=BOB)
    run = {"agent_id": agent["id"], "workflow_id": workflow["id"]}
    started = await client.post("/v1/sessions", json=run, headers=_headers(BOB))
    assert started.status_code == 201, started.text
    # Renamed after Bob's run started, so its name comes from storage, not the run.
    renamed = await client.patch(
        f"/v1/workflows/{workflow['id']}", json={"name": "Release v2"}, headers=_headers(ALICE)
    )
    assert renamed.status_code == 200, renamed.text
    session_runs = f"/v1/workflows/{workflow['id']}/runs?session_id={started.json()['id']}"
    (before,) = (await client.get(session_runs, headers=_headers(BOB))).json()["data"]
    assert before["workflow_name"] == "Release v2"

    def interrupted(*args: object, **kwargs: object) -> int:
        raise RuntimeError("cleanup interrupted")

    # The account is deleted, but removing its workflows fails at the first batch.
    monkeypatch.setattr(accounts_module, "delete_grants_on_workflows", interrupted)
    assert SqlAlchemyAccountStore(db_uri).delete_user(ALICE) is True
    permissions = SqlAlchemyPermissionStore(db_uri)
    assert permissions.resolve_workflow_access([BOB], workflow["id"]) == LEVEL_READ

    path = f"/v1/workflows/{workflow['id']}"
    assert (await client.get(path, headers=_headers(BOB))).status_code == 404
    assert (await client.get("/v1/workflows/shared", headers=_headers(BOB))).json()["data"] == []
    again = await client.post("/v1/sessions", json=run, headers=_headers(BOB))
    assert again.status_code == 400, again.text
    assert "workflow_not_found" in again.json()["error"]["message"]
    # The run Bob already made stays his, but no longer names the workflow.
    journal = f"/v1/sessions/{started.json()['id']}/journal"
    assert (await client.get(journal, headers=_headers(BOB))).status_code == 200
    (after,) = (await client.get(session_runs, headers=_headers(BOB))).json()["data"]
    assert after["workflow_name"] is None


async def test_deleting_a_workflow_deletes_who_it_was_shared_with(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    workflow = await _save_workflow(client, "Release")
    await _share(client, workflow["id"])
    deleted = await client.delete(f"/v1/workflows/{workflow['id']}", headers=_headers(ALICE))
    assert deleted.status_code == 200, deleted.text
    assert SqlAlchemyPermissionStore(db_uri).list_for_workflow(workflow["id"]) == ([], None)
    assert (await client.get("/v1/workflows/shared", headers=_headers(BOB))).json()["data"] == []


async def test_sharing_follows_the_store_and_the_servers_sharing_mode(
    client: httpx.AsyncClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = await _save_workflow(client, "Release")
    path = f"/v1/workflows/{workflow['id']}"
    grant = {"user_id": BOB, "level": LEVEL_READ}
    store = app.state.permission_store

    app.state.sharing_mode = lambda: SharingMode.OFF
    off = await client.put(path + "/permissions", json=grant, headers=_headers(ALICE))
    assert off.status_code == 403, off.text
    app.state.sharing_mode = lambda: SharingMode.RESTRICTED_READ_ONLY
    await _share(client, workflow["id"])

    # A workflow whose grants the store can't keep shows as such, and can't be shared.
    monkeypatch.setattr(store, "workflow_shareable", lambda workflow_id: False)
    assert (await client.get(path, headers=_headers(ALICE))).json()["shareable"] is False
    listed = (await client.get("/v1/workflows", headers=_headers(ALICE))).json()["data"]
    assert [item["shareable"] for item in listed] == [False]
    refused = await client.put(
        path + "/permissions",
        json={"user_id": "carol@example.com", "level": 1},
        headers=_headers(ALICE),
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["message"] == UNSHAREABLE_WORKFLOW_MESSAGE

    # A store without workflow grants turns sharing off.
    monkeypatch.setattr(type(store), "supports_workflow_grants", property(lambda self: False))
    assert (await client.get("/v1/info")).json()["workflow_sharing"] is False
    for method, url, body in (
        ("GET", path + "/permissions", None),
        ("PUT", path + "/permissions", grant),
        ("DELETE", path + f"/permissions/{BOB}", None),
    ):
        response = await client.request(method, url, json=body, headers=_headers(ALICE))
        assert response.status_code == 403, (method, url, response.text)
    assert (await client.get("/v1/workflows/shared", headers=_headers(BOB))).json()["data"] == []
    assert (await client.get(path, headers=_headers(BOB))).status_code == 404
