"""HTTP tests for ``/v1/sessions/{id}/journal``: appending, paging and access."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ
from omnigent.stores.conversation_store import SCHEDULED_TASK_RUN_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
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
CAROL = "carol@example.com"


@pytest.fixture()
def app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    app = _build_app(db_uri, tmp_path)
    for user in (ALICE, BOB, CAROL):
        _make_user(db_uri, user)
    return app


@pytest_asyncio.fixture()
async def client(
    app: FastAPI, mock_llm: ControllableMockClient, tmp_path: Path
) -> AsyncIterator[httpx.AsyncClient]:
    async with _serve(app, mock_llm, tmp_path) as client:
        yield client


def _session(db_uri: str, *, labels: dict[str, str] | None = None) -> str:
    """A session alice owns."""
    conversations = SqlAlchemyConversationStore(db_uri)
    session = conversations.create_conversation()
    SqlAlchemyPermissionStore(db_uri).grant(ALICE, session.id, 4)
    if labels:
        conversations.set_labels(session.id, labels)
    return session.id


def _runner(session_id: str, user: str = ALICE) -> dict[str, str]:
    """Headers of a call the session's runner relays for its agent."""
    return {**_headers(user), ORCHESTRATION_SOURCE_HEADER: session_id}


def _write(kind: str, payload: dict[str, object], key: str, **fields: object) -> dict[str, object]:
    return {"kind": kind, "payload": payload, "idempotency_key": key, **fields}


async def test_plans_and_statuses_mark_the_chat_once_each(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    session_id = _session(db_uri)
    url = f"/v1/sessions/{session_id}/journal"
    steps = [
        {"id": "deploy", "name": "Deploy the swap"},
        {"id": "verify", "name": "Verify routing", "depends_on": ["deploy"]},
    ]
    added = [*steps, {"id": "video", "name": "Record the video"}]
    for body in (
        _write("plan", {"name": "Ship the demo", "steps": steps}, "tool:1"),
        _write("status", {"status": "started"}, "tool:2", step="deploy"),
        _write("status", {"status": "done", "text": "Pod 1/1"}, "tool:3", step="deploy"),
        _write("note", {"body": "Notes get no marker"}, "tool:4"),
        # A retried status, the same plan again, then a replan that adds a task.
        _write("status", {"status": "done", "text": "Pod 1/1"}, "tool:3", step="deploy"),
        _write("plan", {"name": "Ship the demo", "steps": steps}, "tool:5"),
        _write("plan", {"name": "Ship the demo", "steps": added}, "tool:6"),
    ):
        resp = await client.post(url, json=body, headers=_runner(session_id))
        assert resp.status_code == 200, resp.text
    items = SqlAlchemyConversationStore(db_uri).list_items(session_id, limit=50).data
    markers = [item.data.resource for item in items if item.type == "resource_event"]
    assert [(m.get("change") or m["status"], m["name"]) for m in markers] == [
        ("made", "Ship the demo"),
        ("started", "Deploy the swap"),
        ("done", "Deploy the swap"),
        ("updated", "Ship the demo"),
    ]
    assert markers[2]["text"] == "Pod 1/1" and markers[2]["native"] is False
    assert markers[3]["added"] == ["Record the video"] and markers[3]["removed"] == []
    statuses = (await client.get(url, params={"kind": "status"}, headers=_headers())).json()
    assert markers[2]["entry_id"] == statuses["data"][-1]["id"]


async def test_the_runner_appends_and_people_page_the_journal(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    session_id = _session(db_uri)
    url = f"/v1/sessions/{session_id}/journal"
    plan = {
        "name": "Cut GET /orders p95 by 20%",
        "steps": [{"id": "baseline", "name": "Baseline"}, {"id": "goal", "name": "Goal"}],
    }
    written = []
    for body in (
        _write("plan", plan, "tool:1"),
        _write("status", {"status": "started"}, "tool:2", step="baseline"),
        _write("status", {"status": "done", "text": "p95 430 ms"}, "tool:3", step="baseline"),
        _write("note", {"body": "# Profile\nN+1 is the top cost"}, "tool:4", step="goal"),
    ):
        resp = await client.post(url, json=body, headers=_runner(session_id))
        assert resp.status_code == 200, resp.text
        written.append(resp.json())
    assert [entry["position"] for entry in written] == [0, 1, 2, 3]
    assert written[0]["payload"]["version"] == 1
    assert written[0]["payload"]["name"] == "Cut GET /orders p95 by 20%"
    assert written[0]["author"] == {"type": "agent"} and written[0]["step"] is None
    assert written[2]["payload"] == {"status": "done", "text": "p95 430 ms"}

    retried = await client.post(
        url, json=_write("note", {"body": "again"}, "tool:4"), headers=_runner(session_id)
    )
    assert retried.json() == written[3]

    newest = (
        await client.get(url, params={"order": "desc", "limit": 2}, headers=_headers())
    ).json()
    assert [entry["position"] for entry in newest["data"]] == [3, 2]
    assert (newest["first_id"], newest["last_id"], newest["has_more"]) == (
        written[3]["id"],
        written[2]["id"],
        True,
    )
    caught_up = await client.get(
        url, params={"order": "asc", "after": written[1]["id"]}, headers=_headers()
    )
    assert [entry["position"] for entry in caught_up.json()["data"]] == [2, 3]
    current = await client.get(
        url, params={"kind": "plan", "order": "desc", "limit": 1}, headers=_headers()
    )
    assert current.json()["data"][0]["payload"]["steps"][0]["id"] == "baseline"
    statuses = await client.get(
        url, params={"kind": "status", "step": "baseline"}, headers=_headers()
    )
    assert [entry["payload"]["status"] for entry in statuses.json()["data"]] == ["started", "done"]
    stale = await client.get(url, params={"after": uuid.uuid4().hex}, headers=_headers())
    assert stale.status_code == 400 and "stale_cursor" in stale.text


async def test_entries_are_attributed_to_the_run_the_session_serves(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    tasks = SqlAlchemyScheduledTaskStore(db_uri)
    task = tasks.create(
        uuid.uuid4().hex,
        "nightly-build",
        "Get the build green",
        "FREQ=DAILY;BYHOUR=7;BYMINUTE=0",
        ALICE,
        uuid.uuid4().hex,
        "UTC",
    )
    run = tasks.create_run(
        uuid.uuid4().hex, task.id, "running", 1_762_498_800, fired_at=1_762_498_801
    )
    session_id = _session(db_uri, labels={SCHEDULED_TASK_RUN_LABEL_KEY: run.id})
    url = f"/v1/sessions/{session_id}/journal"
    resp = await client.post(
        url, json=_write("note", {"body": "Started"}, "tool:1"), headers=_runner(session_id)
    )
    assert resp.json()["scheduled_task_run_id"] == run.id
    SqlAlchemyConversationStore(db_uri).delete_label(session_id, SCHEDULED_TASK_RUN_LABEL_KEY)
    outside = await client.post(
        url, json=_write("note", {"body": "Later"}, "tool:2"), headers=_runner(session_id)
    )
    assert outside.json()["scheduled_task_run_id"] is None

    page = (await client.get(url, headers=_headers())).json()
    assert [entry["scheduled_task_run_id"] for entry in page["data"]] == [run.id, None]
    assert "runs" not in page


async def test_only_the_sessions_own_runner_writes(client: httpx.AsyncClient, db_uri: str) -> None:
    session_id, other = _session(db_uri), _session(db_uri)
    url = f"/v1/sessions/{session_id}/journal"
    body = _write("note", {"body": "x"}, "tool:1")
    for headers in (_headers(), {**_headers(), ORCHESTRATION_SOURCE_HEADER: other}):
        refused = await client.post(url, json=body, headers=headers)
        assert refused.status_code == 403, refused.text
    assert (await client.post(url, json=body, headers=_runner(session_id))).status_code == 200


async def test_reading_needs_read_access_and_writing_needs_edit(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    session_id = _session(db_uri)
    assert (
        await client.get(f"/v1/sessions/{session_id}/journal", headers=_headers(BOB))
    ).status_code in (
        403,
        404,
    )
    # Grants are cached per server, so each user's level is set before first use.
    readable = _session(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.grant(BOB, readable, LEVEL_READ)
    permissions.grant(CAROL, readable, LEVEL_EDIT)
    url = f"/v1/sessions/{readable}/journal"
    body = _write("note", {"body": "x"}, "tool:1")
    assert (await client.get(url, headers=_headers(BOB))).status_code == 200
    assert (await client.post(url, json=body, headers=_runner(readable, BOB))).status_code == 403
    assert (await client.post(url, json=body, headers=_runner(readable, CAROL))).status_code == 200


@pytest.mark.parametrize(
    ("body", "status", "reason"),
    [
        (
            _write("status", {"status": "done"}, "k", step="a"),
            400,
            "invalid_entry: text is required",
        ),
        (
            _write("status", {"status": "done", "text": "x", "emoji": "✅"}, "k", step="a"),
            400,
            "invalid_entry",
        ),
        (_write("note", {"body": "x" * 5000}, "k"), 400, "entry_too_large: body is 5000 bytes"),
        (
            _write(
                "plan",
                {"name": "A", "steps": [{"id": "a", "name": "A", "depends_on": ["b"]}]},
                "k",
            ),
            400,
            "invalid_entry",
        ),
        (
            _write("status", {"status": "started"}, "k", step="Not An ID"),
            400,
            "invalid_entry: step",
        ),
        (
            _write(
                "plan",
                {"name": "A", "steps": [{"id": "a", "name": "A"}], "source": "workflow"},
                "k",
            ),
            400,
            "exactly two fields, name and steps",
        ),
        (
            _write("plan", {"steps": [{"id": "a", "name": "A"}]}, "k"),
            400,
            "exactly two fields, name and steps",
        ),
        (_write("dispatch", {"dispatch_id": "x"}, "k"), 422, "kind"),
        (
            {"kind": "note", "payload": {"body": "x"}, "idempotency_key": ""},
            422,
            "idempotency_key",
        ),
    ],
)
async def test_invalid_entries_are_refused_without_writing(
    client: httpx.AsyncClient, db_uri: str, body: dict[str, object], status: int, reason: str
) -> None:
    session_id = _session(db_uri)
    url = f"/v1/sessions/{session_id}/journal"
    resp = await client.post(url, json=body, headers=_runner(session_id))
    assert resp.status_code == status, resp.text
    assert reason in resp.text
    assert (await client.get(url, headers=_headers())).json()["data"] == []


async def test_a_missing_session_is_not_found(client: httpx.AsyncClient) -> None:
    session_id = uuid.uuid4().hex
    url = f"/v1/sessions/{session_id}/journal"
    assert (await client.get(url, headers=_headers())).status_code == 404
    body = _write("note", {"body": "x"}, "tool:1")
    assert (await client.post(url, json=body, headers=_runner(session_id))).status_code == 404


async def test_clients_cannot_set_the_run_label(client: httpx.AsyncClient, db_uri: str) -> None:
    session_id = _session(db_uri)
    resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"labels": {SCHEDULED_TASK_RUN_LABEL_KEY: uuid.uuid4().hex}},
        headers=_headers(),
    )
    assert resp.status_code == 400, resp.text
    assert "server-internal" in resp.text


async def test_the_server_advertises_the_journal(client: httpx.AsyncClient) -> None:
    info = await client.get("/v1/info", headers=_headers())
    assert info.json()["session_journal"] is True


async def test_the_agents_tools_write_and_read_through_the_runner(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """The runner's handlers drive the real routes: writes, live events and compact reads."""
    import json

    from omnigent.runner.journal_tools import execute_journal_tool

    session_id, worker = _session(db_uri), _session(db_uri)
    client.headers.update(_headers())
    published: list[tuple[str, dict[str, object]]] = []

    async def run(
        tool: str,
        args: dict[str, object],
        *,
        call_id: str | None = None,
        session: str = session_id,
    ) -> dict:
        output = await execute_journal_tool(
            tool,
            json.dumps(args),
            server_client=client,
            conversation_id=session,
            call_id=call_id,
            publish_event=lambda conversation_id, event: published.append(
                (conversation_id, event)
            ),
        )
        return json.loads(output)

    one_step = {
        "name": "Green nightly",
        "steps": [{"id": "nightly", "name": "Get the nightly build green", "depends_on": []}],
    }
    assert await run("sys_journal_write_plan", one_step, call_id="call_1") == {
        "id": published[0][1]["entry"]["id"],
        "position": 0,
        "version": 1,
    }
    assert published[0][0] == session_id and published[0][1]["type"] == "session.journal.appended"
    started = await run("sys_journal_write_status", {"status": "started"}, call_id="call_2")
    assert published[-1][1]["entry"]["step"] == "nightly"
    # A retried call with the same call ID writes nothing new.
    assert (
        await run("sys_journal_write_status", {"status": "started"}, call_id="call_2") == started
    )
    await run(
        "sys_journal_write_note",
        {"body": "Retry the flaky e2e first\nthen bump the image", "step": "nightly"},
    )
    replan = {
        "name": "Green nightly, then ship",
        "steps": [
            {"id": "nightly", "name": "Get the nightly build green", "depends_on": []},
            {"id": "ship", "name": "Ship", "depends_on": ["nightly"]},
        ],
    }
    assert (await run("sys_journal_write_plan", replan))["version"] == 2
    early = await run("sys_journal_write_status", {"status": "started", "step": "ship"})
    assert early["error"].startswith("step_not_ready: ship waits on nightly, not done yet")
    await run(
        "sys_journal_write_status",
        {"status": "needs_input", "step": "nightly", "text": "CI is down: retry later?"},
    )

    read = await run("sys_journal_read", {})
    lines = read["entries"]
    assert [line.split(" · ", 1)[1] for line in lines] == [
        'plan v1 "Green nightly" (agent): 1 step: nightly',
        "status [nightly] ▶ started",
        "note [nightly]: Retry the flaky e2e first\n    then bump the image",
        'plan v2 "Green nightly, then ship" (agent): added ship',
        "status [nightly] ✋ needs_input: CI is down: retry later?",
    ]
    assert lines[0].startswith("#0 ") and read["page"] == {"has_more": False, "next_cursor": None}

    newest = await run("sys_journal_read", {"limit": 2})
    assert [line.split(" ", 1)[0] for line in newest["entries"]] == ["#3", "#4"]
    older = await run("sys_journal_read", {"limit": 2, "cursor": newest["page"]["next_cursor"]})
    assert [line.split(" ", 1)[0] for line in older["entries"]] == ["#1", "#2"]
    mismatched = await run(
        "sys_journal_read", {"kind": "note", "cursor": newest["page"]["next_cursor"]}
    )
    assert mismatched["error"].startswith(
        "invalid_entry: cursor belongs to a read with other arguments"
    )

    plans = await run("sys_journal_read", {"kind": "plan"})
    assert [
        (plan["version"], plan["name"], [step["id"] for step in plan["steps"]])
        for plan in plans["entries"]
    ] == [
        (1, "Green nightly", ["nightly"]),
        (2, "Green nightly, then ship", ["nightly", "ship"]),
    ]
    # Another session the caller can read, and one that doesn't exist.
    assert (await run("sys_journal_read", {"conversation_id": worker}))["entries"] == []
    missing = await run("sys_journal_read", {"conversation_id": uuid.uuid4().hex})
    assert missing["error"] == "session_not_found"
    assert (await run("sys_journal_read", {"conversation_id": "../info"}))["error"].startswith(
        "invalid_entry"
    )

    count = len(published)
    assert (await run("sys_journal_write_status", {"status": "done"}))["error"].startswith(
        "invalid_entry: text is required"
    )
    too_large = await run("sys_journal_write_note", {"body": "x" * 4097})
    assert too_large["error"].startswith("entry_too_large: body is 4097 bytes")
    # Two steps now, so a status must name one; the server says so.
    unnamed = await run("sys_journal_write_status", {"status": "started"})
    assert unnamed["error"].startswith("invalid_entry: step is required")
    assert len(published) == count


_DEPLOY_QUESTION = {
    "question": "Approve the deploy to prod?",
    "header": "Deploy",
    "options": [{"label": "Approve"}, {"label": "Hold", "description": "Wait for the freeze"}],
    "recommended": "Hold",
}
_ONE_TASK_PLAN = {
    "name": "Ship it",
    "steps": [{"id": "deploy", "name": "Deploy to prod", "depends_on": []}],
}


async def test_a_workflow_question_marks_its_task_and_returns_the_answer(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """``sys_workflow_request_input`` through the real route, up to the user's answer."""
    import asyncio
    import json

    from omnigent.runner import pending_approvals
    from omnigent.runner.journal_tools import execute_journal_tool
    from omnigent.runtime import pending_elicitations

    session_id = _session(db_uri)
    client.headers.update(_headers())
    published: list[dict[str, object]] = []

    async def run(tool: str, args: dict[str, object], call_id: str | None = None) -> dict:
        output = await execute_journal_tool(
            tool,
            json.dumps(args),
            server_client=client,
            conversation_id=session_id,
            call_id=call_id,
            publish_event=lambda _session, event: published.append(event),
        )
        return json.loads(output)

    ask = {"step": "deploy", "questions": [_DEPLOY_QUESTION]}
    no_plan = await run("sys_workflow_request_input", ask)
    assert no_plan["error"] == "invalid_input: the session has no plan; write one first"
    await run("sys_journal_write_plan", _ONE_TASK_PLAN)
    elsewhere = await run("sys_workflow_request_input", {**ask, "step": "verify"})
    assert elsewhere["error"] == "invalid_input: step 'verify' isn't in the current plan"

    asking = asyncio.create_task(run("sys_workflow_request_input", ask, call_id="call_ask"))
    # The server raises the question before it answers the runner, which then
    # publishes the task's status; wait for both.
    while (
        not (pending := pending_elicitations.snapshot_for(session_id))
        or published[-1]["entry"]["kind"] != "status"  # type: ignore[index]
    ):
        await asyncio.sleep(0.01)
    # The question is in the Inbox's pending index, with the shared question form.
    [event] = pending
    params = event["params"]
    assert params["message"] == "Waiting for you on Deploy to prod"
    assert (params["phase"], params["journal_step"]) == ("workflow_request_input", "deploy")
    assert params.get("policy_name") is None
    [form] = params["ask_user_question"]["questions"]
    assert [option["label"] for option in form["options"]] == ["Hold (recommended)", "Approve"]
    # The task waits for the user, its status the question.
    entry = published[-1]["entry"]
    assert entry["step"] == "deploy"
    assert entry["payload"] == {"status": "needs_input", "text": "Approve the deploy to prod?"}

    while not pending_approvals.resolve(
        event["elicitation_id"], True, {"Approve the deploy to prod?": "Approve"}
    ):
        await asyncio.sleep(0.01)
    assert await asking == {
        "answered": True,
        "answers": [{"question": "Approve the deploy to prod?", "answer": "Approve"}],
    }


async def test_a_retried_workflow_question_reuses_its_status_and_question(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    session_id = _session(db_uri)
    journal = f"/v1/sessions/{session_id}/journal"
    plan = await client.post(
        journal, json=_write("plan", _ONE_TASK_PLAN, "tool:plan"), headers=_runner(session_id)
    )
    assert plan.status_code == 200, plan.text
    body = {"step": "deploy", "questions": [_DEPLOY_QUESTION], "idempotency_key": "tool:ask"}
    url = f"{journal}/input-requests"
    first = await client.post(url, json=body, headers=_runner(session_id))
    again = await client.post(url, json=body, headers=_runner(session_id))
    assert first.status_code == 200, first.text
    assert again.json() == first.json()
    assert first.json()["elicitation_id"].startswith("elicit_journal_")
    # Only the session's own runner asks.
    refused = await client.post(url, json=body, headers=_headers())
    assert refused.status_code == 403, refused.text


async def test_the_workflow_tools_save_and_load_through_the_real_routes(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """``sys_workflow_save`` and ``sys_workflow_load`` reach ``/v1/workflows`` as the owner."""
    import json

    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
    from omnigent.runner.journal_tools import execute_journal_tool

    session_id = _session(db_uri)
    # The runner's own credentials: its tunnel token, bound to the calling session.
    token = "workflow-tools-runner"
    conversations = SqlAlchemyConversationStore(db_uri)
    assert conversations.set_runner_id(session_id, token_bound_runner_id(token)) is True
    client.headers.update({**_headers(), RUNNER_TUNNEL_TOKEN_HEADER: token})

    async def run(tool: str, args: dict[str, object]) -> dict:
        output = await execute_journal_tool(
            tool, json.dumps(args), server_client=client, conversation_id=session_id
        )
        return json.loads(output)

    workflow = {
        "name": "Triage flaky tests",
        "description": "Finds and fixes flaky tests. Give it: the test suite.",
        "steps": [
            {"id": "explore", "name": "Explore the failing tests", "depends_on": []},
            {"id": "fix", "name": "Fix the failing tests", "depends_on": ["explore"]},
        ],
    }
    saved = await run("sys_workflow_save", workflow)
    assert set(saved) == {"id", "name"}, saved
    assert saved["name"] == "Triage flaky tests"
    assert (await run("sys_workflow_save", workflow))["error"] == "name_taken"

    by_name = await run("sys_workflow_load", {"workflow": "triage flaky tests"})
    assert by_name["id"] == saved["id"]
    assert [step["id"] for step in by_name["steps"]] == ["explore", "fix"]
    assert await run("sys_workflow_load", {"workflow": saved["id"]}) == by_name
    listed = (await client.get("/v1/workflows")).json()["data"]
    assert [item["name"] for item in listed] == ["Triage flaky tests"]

    missing = await run("sys_workflow_load", {"workflow": "Triage flaky test runs"})
    assert missing["error"] == "workflow_not_found"
    assert missing["similar"] == ["Triage flaky tests"]
