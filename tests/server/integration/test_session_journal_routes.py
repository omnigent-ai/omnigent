"""HTTP tests for ``/v1/sessions/{id}/journal``: appending, paging and access."""

from __future__ import annotations

import json
import random
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI

from omnigent.db.db_models import SqlSessionJournalEntry
from omnigent.runner.identity import (
    ORCHESTRATION_SOURCE_HEADER,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, RESERVED_USER_LOCAL
from omnigent.stores import session_journal_store
from omnigent.stores.conversation_store import (
    CREATED_BY_SESSION_LABEL_KEY,
    SCHEDULED_TASK_ORIGIN_LABEL_KEY,
    SCHEDULED_TASK_RUN_LABEL_KEY,
    SCHEDULED_TASK_SOURCE_LABEL_KEY,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from tests.debug_log_helpers import capture_debug_rows
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
_RUNNER_TOKEN = "journal-test-runner"


@pytest.fixture()
def app(
    runtime_init: None, db_uri: str, tmp_path: Path, request: pytest.FixtureRequest
) -> FastAPI:
    app = _build_app(
        db_uri, tmp_path, workflows=True, local_single_user=getattr(request, "param", False)
    )
    for user in (ALICE, BOB, CAROL):
        _make_user(db_uri, user)
    return app


@pytest_asyncio.fixture()
async def client(
    app: FastAPI, mock_llm: ControllableMockClient, tmp_path: Path
) -> AsyncIterator[httpx.AsyncClient]:
    async with _serve(app, mock_llm, tmp_path) as client:
        yield client


def _session(
    db_uri: str,
    *,
    labels: dict[str, str] | None = None,
    title: str | None = None,
    owner: str = ALICE,
    parent_conversation_id: str | None = None,
    runner_token: str | None = _RUNNER_TOKEN,
) -> str:
    """A session *owner* owns, alice by default."""
    conversations = SqlAlchemyConversationStore(db_uri)
    session = conversations.create_conversation(
        title=title, parent_conversation_id=parent_conversation_id
    )
    SqlAlchemyPermissionStore(db_uri).grant(owner, session.id, 4)
    if labels:
        conversations.set_labels(session.id, labels)
    if runner_token is not None:
        conversations.set_runner_id(session.id, token_bound_runner_id(runner_token))
    return session.id


def _runner(session_id: str, user: str = ALICE) -> dict[str, str]:
    """Headers of a call the session's runner relays for its agent."""
    return {
        **_headers(user),
        ORCHESTRATION_SOURCE_HEADER: session_id,
        RUNNER_TUNNEL_TOKEN_HEADER: _RUNNER_TOKEN,
    }


def _oversized_description() -> str:
    """CJK noise within the 2,000-character limit whose stored form is over 2,000 bytes."""
    noise = random.Random(0)
    return "".join(chr(noise.randrange(0x4E00, 0x9FFF)) for _ in range(2000))


def _write(kind: str, content: dict[str, object], key: str, **fields: object) -> dict[str, object]:
    return {"kind": kind, "content": content, "idempotency_key": key, **fields}


async def test_telemetry_reconstructs_workflow_usage_cohorts_without_retry_or_planning_counts(
    client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [100]
    monkeypatch.setattr("omnigent.stores.session_journal_store.now_epoch", lambda: clock[0])
    steps = [{"id": "private-step", "name": "Private task", "goal": "Private instructions"}]
    saved = await client.post(
        "/v1/workflows",
        json={"name": "Private workflow", "description": "Private description", "steps": steps},
        headers=_headers(),
    )
    assert saved.status_code == 200, saved.text
    workflow_id = saved.json()["id"]
    task_id, run_id = uuid.uuid4().hex, uuid.uuid4().hex
    automation = _session(
        db_uri,
        labels={SCHEDULED_TASK_ORIGIN_LABEL_KEY: task_id, SCHEDULED_TASK_RUN_LABEL_KEY: run_id},
    )
    worker = _session(db_uri, labels={CREATED_BY_SESSION_LABEL_KEY: automation})
    child = _session(db_uri, parent_conversation_id=worker)
    sessions = [
        (_session(db_uri), "interactive", "saved"),
        (_session(db_uri), "interactive", "ad_hoc"),
        (automation, "automation", "saved"),
        (worker, "automation", "ad_hoc"),
        (child, "automation", "saved"),
    ]
    proposal = _session(db_uri)
    plans = {}
    with capture_debug_rows("server") as rows:
        for session_id, _, kind in sessions:
            url = f"/v1/sessions/{session_id}/journal"
            body = _write("plan", {"name": "Private plan", "steps": steps}, "plan")
            if kind == "saved":
                body["workflow_id"] = workflow_id
            clock[0] = 100
            for _ in range(2):
                response = await client.post(url, json=body, headers=_runner(session_id))
                assert response.status_code == 200, response.text
            plans[session_id] = response.json()["id"]
            for status, elapsed in (("started", 5), ("done", 11)):
                clock[0] = 100 + elapsed
                body = _write("status", {"status": status, "text": "Private result"}, status)
                for _ in range(2):
                    response = await client.post(url, json=body, headers=_runner(session_id))
                    assert response.status_code == 200, response.text

        proposed = await client.post(
            f"/v1/sessions/{proposal}/journal",
            json=_write("plan", {"name": "Private proposal", "steps": steps}, "proposal"),
            headers=_runner(proposal),
        )
        assert proposed.status_code == 200, proposed.text
        # Saving an already-executed ad hoc plan only associates a new plan version.
        adhoc = sessions[1][0]
        associated = await client.post(
            f"/v1/sessions/{adhoc}/journal",
            json=_write(
                "plan", {"name": "Private plan", "steps": steps}, "save", workflow_id=workflow_id
            ),
            headers=_runner(adhoc),
        )
        assert associated.status_code == 200, associated.text

    events = [row for row in rows if row["event_name"].startswith("workflow_")]
    plan_events = [row for row in events if row["event_name"] == "workflow_plan_written"]
    status_events = [row for row in events if row["event_name"] == "workflow_step_status_written"]
    assert len(plan_events) == 7 and len(status_events) == 10
    assert len({row["attributes"]["journal_entry_id"] for row in events}) == len(events)
    assert "Private" not in str(events) and "private-step" not in str(events)

    # A dashboard gets only serialized rows, not access to the live stores.
    observed = {
        (
            row["session_id"],
            row["attributes"]["execution_source"],
            row["attributes"]["workflow_kind"],
        )
        for row in status_events
    }
    assert observed == set(sessions)
    assert Counter((source, kind) for _, source, kind in observed) == {
        ("interactive", "saved"): 1,
        ("interactive", "ad_hoc"): 1,
        ("automation", "saved"): 2,
        ("automation", "ad_hoc"): 1,
    }
    assert {row["user_id"] for row in status_events} == {ALICE}
    plan_keys = {
        row["attributes"]["plan_id"]: json.loads(row["attributes"]["step_keys"])
        for row in plan_events
    }
    for row in status_events:
        attrs = row["attributes"]
        assert attrs["plan_id"] == plans[row["session_id"]]
        assert attrs["step_key"] in plan_keys[attrs["plan_id"]]
        assert attrs["plan_elapsed_seconds"] == ("5" if attrs["status"] == "started" else "11")
        if attrs["execution_source"] == "automation":
            assert attrs["automation_session_id"] == automation and attrs["task_id"] == task_id
        if row["session_id"] == automation:
            assert attrs["scheduled_task_run_id"] == run_id
        else:
            # A worker must not be attributed to an ancestor's newer continued run.
            assert "scheduled_task_run_id" not in attrs
    association = plan_events[-1]["attributes"]
    assert association["plan_change"] == "association_changed"
    assert association["previous_plan_id"] == plans[adhoc]
    assert json.loads(association["previous_step_keys"]) == plan_keys[plans[adhoc]]
    assert json.loads(association["step_keys"]) != plan_keys[plans[adhoc]]

    # Plan-local keys retain their lineage when a step moves after a newly added step.
    with capture_debug_rows("server") as replanned_rows:
        replanned = await client.post(
            f"/v1/sessions/{adhoc}/journal",
            json=_write(
                "plan",
                {
                    "name": "Private plan",
                    "steps": [{"id": "new-step", "name": "New task"}, *steps],
                },
                "replan",
            ),
            headers=_runner(adhoc),
        )
        assert replanned.status_code == 200, replanned.text
        status = await client.post(
            f"/v1/sessions/{adhoc}/journal",
            json=_write(
                "status",
                {"status": "done", "text": "Private result"},
                "replanned-status",
                step="private-step",
            ),
            headers=_runner(adhoc),
        )
        assert status.status_code == 200, status.text
    replanned_event, status_event = [
        row["attributes"] for row in replanned_rows if row["event_name"].startswith("workflow_")
    ]
    assert json.loads(replanned_event["previous_step_keys"]) == [
        None,
        *json.loads(association["step_keys"]),
    ]
    assert status_event["step_key"] == json.loads(replanned_event["step_keys"])[1]
    assert "Private" not in str(replanned_rows) and "private-step" not in str(replanned_rows)


async def test_telemetry_records_failed_and_waiting_steps_but_not_rejected_writes(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    session_id = _session(db_uri)
    url = f"/v1/sessions/{session_id}/journal"
    with capture_debug_rows("server") as rows:
        response = await client.post(
            url,
            json=_write(
                "plan", {"name": "Plan", "steps": [{"id": "check", "name": "Check"}]}, "p"
            ),
            headers=_runner(session_id),
        )
        assert response.status_code == 200, response.text
        for status in ("failed", "needs_input"):
            response = await client.post(
                url,
                json=_write("status", {"status": status, "text": "Private blocker"}, status),
                headers=_runner(session_id),
            )
            assert response.status_code == 200, response.text
        refused = await client.post(
            url,
            json=_write("status", {"status": "done", "text": "Finished"}, "foreign"),
            headers=_runner(session_id, BOB),
        )
        assert refused.status_code == 404
        invalid = await client.post(
            url, json=_write("status", {"status": "nope"}, "invalid"), headers=_runner(session_id)
        )
        assert invalid.status_code == 400
    statuses = [row for row in rows if row["event_name"] == "workflow_step_status_written"]
    assert [row["attributes"]["status"] for row in statuses] == ["failed", "needs_input"]


async def test_a_logging_failure_does_not_fail_a_committed_workflow_write(
    client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server import workflow_telemetry

    def unavailable(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Logging sink unavailable")

    # Exercise a failed logging dependency while keeping the route and persistence real.
    monkeypatch.setattr(workflow_telemetry._logger, "info", unavailable)
    created = await client.post(
        "/v1/workflows",
        json={"name": "Workflow", "description": "Check the result"},
        headers=_headers(),
    )
    assert created.status_code == 200, created.text
    session_id = _session(db_uri)
    url = f"/v1/sessions/{session_id}/journal"
    response = await client.post(
        url,
        json=_write("plan", {"name": "Plan", "steps": [{"id": "check", "name": "Check"}]}, "p"),
        headers=_runner(session_id),
    )
    assert response.status_code == 200, response.text
    page = await client.get(url, headers=_headers())
    assert [entry["id"] for entry in page.json()["data"]] == [response.json()["id"]]


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
        _write("note", {"body": "Notes get no visible marker"}, "tool:4"),
        # A retried status, the same plan again, then a replan that adds a task.
        _write("status", {"status": "done", "text": "Pod 1/1"}, "tool:3", step="deploy"),
        _write("plan", {"name": "Ship the demo", "steps": steps}, "tool:5"),
        _write("plan", {"name": "Ship the demo", "steps": added}, "tool:6"),
    ):
        resp = await client.post(url, json=body, headers=_runner(session_id))
        assert resp.status_code == 200, resp.text
    items = SqlAlchemyConversationStore(db_uri).list_items(session_id, limit=50).data
    markers = [
        item.data.resource
        for item in items
        if item.type == "resource_event" and item.data.event_type != "session.journal.checkpoint"
    ]
    checkpoints = [
        item
        for item in items
        if item.type == "resource_event" and item.data.event_type == "session.journal.checkpoint"
    ]
    assert len(checkpoints) == 2
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
    assert written[0]["content"]["version"] == 1
    assert written[0]["content"]["name"] == "Cut GET /orders p95 by 20%"
    assert written[0]["author"] == {"type": "agent"} and written[0]["step"] is None
    assert written[2]["content"] == {
        "status": "done",
        "text": "p95 430 ms",
        "graph_revision": written[0]["content"]["graph_revision"],
    }

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
    assert current.json()["data"][0]["content"]["steps"][0]["id"] == "baseline"
    statuses = await client.get(
        url, params={"kind": "status", "step": "baseline"}, headers=_headers()
    )
    assert [entry["content"]["status"] for entry in statuses.json()["data"]] == ["started", "done"]
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
    from omnigent.runtime import pending_elicitations

    session_id, other = _session(db_uri), _session(db_uri)
    url = f"/v1/sessions/{session_id}/journal"
    body = _write("note", {"body": "x"}, "tool:1")
    assert (
        await client.post(
            url, json=_write("plan", _ONE_TASK_PLAN, "plan"), headers=_runner(session_id)
        )
    ).status_code == 200
    requests = (
        (url, body),
        (
            url + "/input-requests",
            {"step": "deploy", "questions": [_DEPLOY_QUESTION], "idempotency_key": "ask"},
        ),
    )
    for headers in (
        _headers(),
        _runner(other),
        {**_headers(), ORCHESTRATION_SOURCE_HEADER: session_id},
        {**_runner(session_id), RUNNER_TUNNEL_TOKEN_HEADER: "another-runner"},
        {**_runner(session_id), RUNNER_TUNNEL_TOKEN_HEADER: " "},
    ):
        for path, request_body in requests:
            refused = await client.post(path, json=request_body, headers=headers)
            assert refused.status_code == 403, refused.text
    conversations = SqlAlchemyConversationStore(db_uri)
    conversations.clear_runner_id(session_id)
    for path, request_body in requests:
        refused = await client.post(path, json=request_body, headers=_runner(session_id))
        assert refused.status_code == 403, refused.text
    assert not pending_elicitations.snapshot_for(session_id)
    entries = (await client.get(url, headers=_headers())).json()["data"]
    assert [entry["kind"] for entry in entries] == ["plan"]
    conversations.set_runner_id(session_id, token_bound_runner_id(_RUNNER_TOKEN))
    assert (await client.post(url, json=body, headers=_runner(session_id))).status_code == 200


@pytest.mark.parametrize("app", [True], indirect=True)
async def test_single_user_journals_accept_local_runner_identity(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    session_id = _session(db_uri, owner=RESERVED_USER_LOCAL, runner_token=None)
    response = await client.post(
        f"/v1/sessions/{session_id}/journal",
        json=_write("note", {"body": "Local progress"}, "local"),
        headers={ORCHESTRATION_SOURCE_HEADER: session_id},
    )
    assert response.status_code == 200, response.text


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
    assert (
        await client.post(
            url,
            json=body,
            headers={**_headers(CAROL), ORCHESTRATION_SOURCE_HEADER: readable},
        )
    ).status_code == 403
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
            "a plan's content has name and steps, and optionally description",
        ),
        (
            _write("plan", {"steps": [{"id": "a", "name": "A"}]}, "k"),
            400,
            "a plan's content has name and steps, and optionally description",
        ),
        (
            _write(
                "plan",
                {
                    "name": "A",
                    "steps": [{"id": "a", "name": "A"}],
                    "description": _oversized_description(),
                },
                "k",
            ),
            400,
            "stored bytes; the limit is 2000",
        ),
        (_write("dispatch", {"dispatch_id": "x"}, "k"), 422, "kind"),
        (
            {"kind": "note", "content": {"body": "x"}, "idempotency_key": ""},
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


async def test_a_proposed_description_that_isnt_unicode_is_refused_without_writing(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    session_id = _session(db_uri)
    url = f"/v1/sessions/{session_id}/journal"
    plan = {"name": "A", "steps": [{"id": "a", "name": "A"}], "description": "\ud800"}
    # ASCII JSON can carry an escaped lone surrogate that no UTF-8 text holds.
    resp = await client.post(
        url,
        content=json.dumps(_write("plan", plan, "k")).encode("ascii"),
        headers={**_runner(session_id), "content-type": "application/json"},
    )
    assert resp.status_code == 400, resp.text
    assert "description must be valid Unicode text" in resp.text
    assert (await client.get(url, headers=_headers())).json()["data"] == []


async def test_a_missing_session_is_not_found(client: httpx.AsyncClient) -> None:
    session_id = uuid.uuid4().hex
    url = f"/v1/sessions/{session_id}/journal"
    assert (await client.get(url, headers=_headers())).status_code == 404
    body = _write("note", {"body": "x"}, "tool:1")
    assert (await client.post(url, json=body, headers=_runner(session_id))).status_code == 404


async def test_a_full_journal_refuses_new_entries_with_journal_full(
    client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_journal_store, "JOURNAL_MAX_ENTRIES", 1)
    session_id = _session(db_uri)
    url = f"/v1/sessions/{session_id}/journal"
    first = _write("note", {"body": "x"}, "tool:1")
    assert (await client.post(url, json=first, headers=_runner(session_id))).status_code == 200
    second = _write("note", {"body": "y"}, "tool:2")
    full = await client.post(url, json=second, headers=_runner(session_id))
    assert full.status_code == 409 and "journal_full" in full.text
    # A retry of a write that already landed still returns its entry.
    assert (await client.post(url, json=first, headers=_runner(session_id))).status_code == 200


@pytest.mark.parametrize(
    "label",
    [
        SCHEDULED_TASK_RUN_LABEL_KEY,
        "omnigent.scheduled_task_activity",
        "omnigent.scheduled_task_initial_run",
        "omnigent.pending_scheduled_run.reserved",
    ],
)
@pytest.mark.parametrize("user", [ALICE, BOB])
async def test_clients_cannot_set_the_run_label(
    client: httpx.AsyncClient, db_uri: str, label: str, user: str
) -> None:
    session_id = _session(db_uri)
    SqlAlchemyPermissionStore(db_uri).grant(BOB, session_id, LEVEL_EDIT)
    resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"labels": {label: uuid.uuid4().hex}},
        headers=_headers(user),
    )
    assert resp.status_code == 400, resp.text
    assert "server-internal" in resp.text
    assert label not in SqlAlchemyConversationStore(db_uri).get_conversation(session_id).labels


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
    client.headers.update(_runner(session_id))
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
        # A workflow planning chat's proposal also carries the description Save plan stores.
        "description": "Gets the nightly build green, then ships it.",
    }
    assert (await run("sys_journal_write_plan", replan))["version"] == 2
    assert published[-1][1]["entry"]["content"]["description"] == replan["description"]
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


@pytest.mark.parametrize("origin", ["interactive", "automation_source", "interactive_worker"])
async def test_a_workflow_question_marks_its_task_and_returns_the_answer(
    client: httpx.AsyncClient, db_uri: str, origin: str
) -> None:
    """``sys_workflow_request_input`` through the real route, up to the user's answer."""
    import asyncio
    import json

    from omnigent.runner import pending_approvals
    from omnigent.runner.journal_tools import execute_journal_tool
    from omnigent.runtime import pending_elicitations

    labels = {}
    if origin == "automation_source":
        labels[SCHEDULED_TASK_SOURCE_LABEL_KEY] = uuid.uuid4().hex
    elif origin == "interactive_worker":
        labels[CREATED_BY_SESSION_LABEL_KEY] = _session(db_uri)
    session_id = _session(db_uri, labels=labels)
    client.headers.update(_runner(session_id))
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
    # A task that can't start yet takes no question, and none reaches the Inbox.
    build_first = [
        {"id": "build", "name": "Build", "depends_on": []},
        {"id": "deploy", "name": "Deploy to prod", "depends_on": ["build"]},
    ]
    await run("sys_journal_write_plan", {"name": "Ship it", "steps": build_first})
    early = await run("sys_workflow_request_input", ask)
    assert early["error"].startswith("step_not_ready: deploy waits on build, not done yet")
    assert not pending_elicitations.snapshot_for(session_id)
    await run("sys_journal_write_plan", _ONE_TASK_PLAN)

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
    plan = session_journal_store.SessionJournalStore(db_uri).current_plan(session_id)
    assert plan is not None
    assert entry["content"] == {
        "status": "needs_input",
        "text": "Approve the deploy to prod?",
        "graph_revision": plan.content["graph_revision"],
    }

    while not pending_approvals.resolve(
        event["elicitation_id"], True, {"Approve the deploy to prod?": "Approve"}
    ):
        await asyncio.sleep(0.01)
    assert await asking == {
        "answered": True,
        "answers": [{"question": "Approve the deploy to prod?", "answer": "Approve"}],
    }


@pytest.mark.parametrize("settled", [True, False])
async def test_a_human_turn_after_a_continued_automation_restores_interactive_attribution(
    client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch, settled: bool
) -> None:
    from omnigent.server.routes._sessions.helpers import _publish_status
    from tests.server.integration.test_sessions_harness_override import _stub_late_runner_bind

    conversations = SqlAlchemyConversationStore(db_uri)
    session_id = _session(db_uri)
    tasks = SqlAlchemyScheduledTaskStore(db_uri)
    task = tasks.create(
        uuid.uuid4().hex,
        "Reminder",
        "Review the report",
        "FREQ=HOURLY",
        ALICE,
        uuid.uuid4().hex,
        "UTC",
        session_mode="continue",
        session_id=session_id,
    )
    run = tasks.create_run(
        uuid.uuid4().hex,
        task.id,
        "succeeded",
        1,
        conversation_id=session_id,
        fired_at=1,
    )
    conversations.update_scheduled_run_label(session_id, run.id, action="begin")
    if settled:
        conversations.update_scheduled_run_label(session_id, run.id, action="commit")
    journal = f"/v1/sessions/{session_id}/journal"
    automated = await client.post(
        journal,
        json=_write("plan", _ONE_TASK_PLAN, "automated-plan"),
        headers=_runner(session_id),
    )
    assert automated.json()["scheduled_task_run_id"] == run.id
    question = {"step": "deploy", "questions": [_DEPLOY_QUESTION], "idempotency_key": "question"}
    # Continued runs succeed on delivery, before their agent has finished working.
    refused = await client.post(
        f"{journal}/input-requests", json=question, headers=_runner(session_id)
    )
    assert refused.status_code == 409 and "unattended_input_not_allowed" in refused.text

    workers = []
    for ancestry in (
        {"parent_conversation_id": session_id},
        {"labels": {CREATED_BY_SESSION_LABEL_KEY: session_id}},
    ):
        worker = conversations.create_conversation(
            **ancestry,
            runner_id=token_bound_runner_id(_RUNNER_TOKEN),
        )
        SqlAlchemyPermissionStore(db_uri).grant(ALICE, worker.id, 4)
        workers.append(worker.id)
    finished = await client.post(
        journal,
        json=_write("status", {"status": "done", "text": "Reported"}, "done"),
        headers=_runner(session_id),
    )
    assert finished.status_code == 200, finished.text
    _publish_status(session_id, "idle")
    _stub_late_runner_bind(monkeypatch)
    sender = _session(db_uri)
    peer = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "session_message", "data": {"message": "Background worker finished"}},
        headers=_runner(sender),
    )
    assert peer.status_code == 202, peer.text
    assert (
        conversations.get_conversation(session_id).labels[SCHEDULED_TASK_RUN_LABEL_KEY] == run.id
    )
    human = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "text", "text": "Let's review the result"}],
            },
        },
        headers=_headers(ALICE),
    )
    assert human.status_code == 202, human.text
    followup = await client.post(
        journal,
        json=_write("plan", _ONE_TASK_PLAN, "interactive-plan"),
        headers=_runner(session_id),
    )
    assert followup.json()["scheduled_task_run_id"] is None
    asked = await client.post(
        f"{journal}/input-requests", json=question, headers=_runner(session_id)
    )
    assert asked.status_code == 200, asked.text
    assert asked.json()["entry"]["scheduled_task_run_id"] is None
    note = await client.post(
        journal,
        json=_write("note", {"body": "Interactive follow-up"}, "human-note"),
        headers=_runner(session_id),
    )
    assert note.json()["scheduled_task_run_id"] is None
    history = (
        await client.get(f"/v1/scheduled-tasks/{task.id}/run-steps", headers=_headers(ALICE))
    ).json()
    assert [entry["content"]["status"] for entry in history["runs"][0]["statuses"]] == ["done"]
    for worker_id in workers:
        waiting = await client.post(
            f"/v1/sessions/{worker_id}/journal",
            json=_write("status", {"status": "needs_input", "text": "Approve?"}, "worker-wait"),
            headers=_runner(worker_id),
        )
        assert waiting.status_code == 409 and "unattended_input_not_allowed" in waiting.text


@pytest.mark.parametrize(
    "automation_label", [SCHEDULED_TASK_ORIGIN_LABEL_KEY, SCHEDULED_TASK_RUN_LABEL_KEY]
)
@pytest.mark.parametrize(
    "lineage",
    [(), ("parent",), ("creator",), ("creator", "creator"), ("parent", "creator", "parent")],
    ids=["automation", "subagent", "worker", "nested-worker", "mixed-descendant"],
)
async def test_automation_workflows_cannot_wait_for_user_input(
    client: httpx.AsyncClient, db_uri: str, automation_label: str, lineage: tuple[str, ...]
) -> None:
    """Both tools fail before writing a waiting status or putting a question in the Inbox."""
    import asyncio
    import json

    from omnigent.runner.journal_tools import execute_journal_tool
    from omnigent.runtime import pending_elicitations

    session_id = _session(db_uri, labels={automation_label: uuid.uuid4().hex})
    for edge in lineage:
        session_id = _session(
            db_uri,
            parent_conversation_id=session_id if edge == "parent" else None,
            labels={CREATED_BY_SESSION_LABEL_KEY: session_id} if edge == "creator" else None,
        )
    client.headers.update(_runner(session_id))

    async def run(tool: str, args: dict[str, object]) -> dict:
        output = await asyncio.wait_for(
            execute_journal_tool(
                tool, json.dumps(args), server_client=client, conversation_id=session_id
            ),
            timeout=5,
        )
        return json.loads(output)

    plan = {
        **_ONE_TASK_PLAN,
        "steps": [
            *_ONE_TASK_PLAN["steps"],
            {"id": "report", "name": "Report the outcome", "depends_on": []},
        ],
    }
    assert (await run("sys_journal_write_plan", plan))["version"] == 1
    journal = f"/v1/sessions/{session_id}/journal"
    initial_entries = (await client.get(journal)).json()["data"]
    for tool, args in (
        (
            "sys_journal_write_status",
            {"step": "deploy", "status": "needs_input", "text": "Approve the deploy?"},
        ),
        ("sys_workflow_request_input", {"step": "deploy", "questions": [_DEPLOY_QUESTION]}),
    ):
        result = await run(tool, args)
        assert result["error"].startswith("unattended_input_not_allowed:"), result
        assert "failed" in result["error"] and "authorization" in result["error"]
        assert pending_elicitations.snapshot_for(session_id) == []
    entries = (await client.get(journal)).json()["data"]
    assert entries == initial_entries

    # The agent can record the blocker and finish independent work after the rejection.
    for step, status, text in (
        ("deploy", "failed", "Sign-in requires a person"),
        ("report", "done", "Reported the blocker"),
    ):
        assert "error" not in await run(
            "sys_journal_write_status", {"step": step, "status": status, "text": text}
        )
    entries = (await client.get(journal, params={"kind": "status"})).json()["data"]
    assert [(entry["step"], entry["content"]["status"]) for entry in entries] == [
        ("deploy", "failed"),
        ("report", "done"),
    ]


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
    """The workflow tools reach ``/v1/workflows`` as the owner: save, load and list."""
    import json

    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
    from omnigent.runner.journal_tools import execute_journal_tool

    session_id = _session(db_uri, runner_token=None)
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

    notes = {**workflow, "name": "Release notes", "description": "Drafts the notes. " * 100}
    assert (await run("sys_workflow_save", notes))["name"] == "Release notes"
    page = await run("sys_workflow_list", {"limit": 1})
    assert [item["name"] for item in page["workflows"]] == ["Release notes"]
    assert page["workflows"][0]["description"].endswith("…")
    rest = await run("sys_workflow_list", {"cursor": page["next_cursor"]})
    assert rest == {
        "workflows": [
            {
                "id": saved["id"],
                "name": "Triage flaky tests",
                "description": workflow["description"],
            }
        ],
        "next_cursor": None,
    }

    # Running it: the plan names the workflow, and the workflow's page lists the run.
    plan = {"name": by_name["name"], "steps": by_name["steps"], "workflow_id": saved["id"]}
    assert (await run("sys_journal_write_plan", plan))["version"] == 1
    runs = (await client.get(f"/v1/workflows/{saved['id']}/runs")).json()
    assert [run["session_id"] for run in runs["data"]] == [session_id]


async def test_saving_a_workflow_names_the_sessions_plan_after_it(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """The plan a session saves is then the workflow: it names it, with no new chat line."""
    import json

    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
    from omnigent.runner.journal_tools import execute_journal_tool

    session_id = _session(db_uri, runner_token=None)
    token = "save-names-plan-runner"
    conversations = SqlAlchemyConversationStore(db_uri)
    assert conversations.set_runner_id(session_id, token_bound_runner_id(token)) is True
    client.headers.update({**_headers(), RUNNER_TUNNEL_TOKEN_HEADER: token})

    async def run(tool: str, args: dict[str, object]) -> dict:
        output = await execute_journal_tool(
            tool, json.dumps(args), server_client=client, conversation_id=session_id
        )
        return json.loads(output)

    steps = [
        {"id": "explore", "name": "Explore the failing tests", "depends_on": []},
        {"id": "fix", "name": "Fix the failing tests", "depends_on": ["explore"]},
    ]
    plan = {"name": "Fix the upload flakes", "steps": steps}
    assert (await run("sys_journal_write_plan", plan))["version"] == 1
    assert "error" not in await run(
        "sys_journal_write_status",
        {"step": "explore", "status": "done", "text": "Found the failure."},
    )
    workflow = {
        "name": "Triage flaky tests",
        "description": "Finds and fixes them.",
        "steps": steps,
    }
    saved = await run("sys_workflow_save", workflow)
    assert "error" not in await run(
        "sys_journal_write_status", {"step": "fix", "status": "started"}
    )
    runs_url = f"/v1/workflows/{saved['id']}/runs"
    progress = (await client.get(runs_url)).json()["data"][0]["progress"]
    assert progress == {"total": 2, "started": 1, "done": 1, "needs_input": 0, "failed": 0}

    url = f"/v1/sessions/{session_id}/journal"
    plans = (await client.get(url, params={"kind": "plan"})).json()["data"]
    assert [(entry["content"]["version"], entry["workflow_id"]) for entry in plans] == [
        (1, None),
        (2, saved["id"]),
    ]
    assert plans[1]["content"]["name"] == "Fix the upload flakes"
    assert [step["id"] for step in plans[1]["content"]["steps"]] == ["explore", "fix"]
    items = conversations.list_items(session_id, limit=50).data
    markers = [
        item.data.resource
        for item in items
        if item.type == "resource_event" and item.data.event_type == "session.journal.plan"
    ]
    assert [marker.get("change") for marker in markers] == ["made"]
    assert (await run("sys_journal_write_plan", {**plan, "workflow_id": saved["id"]}))[
        "version"
    ] == 3
    blocked = await run("sys_journal_write_status", {"step": "fix", "status": "started"})
    assert "step_not_ready" in blocked["error"]
    runs = (await client.get(runs_url)).json()["data"]
    assert [run["progress"]["done"] for run in runs] == [0, 1]


async def test_a_workflows_runs_list_the_sessions_that_ran_it(
    client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server import session_live_state
    from omnigent.server.routes._sessions.helpers import _publish_status
    from omnigent.stores.session_journal_store import SessionJournalStore
    from tests._helpers.polling import poll_until_async

    steps = [
        {"id": "explore", "name": "Explore", "depends_on": []},
        {"id": "fix", "name": "Fix", "depends_on": ["explore"]},
    ]
    saved = await client.post(
        "/v1/workflows",
        json={"name": "Triage", "description": "Triage a flaky test.", "steps": steps},
        headers=_headers(ALICE),
    )
    workflow_id = saved.json()["id"]
    plan = {"name": "Triage", "steps": steps}
    first = _session(db_uri, title="Fix the upload test")
    second = _session(db_uri, title="Fix the retry test")
    for session_id, status in ((first, "done"), (second, "needs_input")):
        url = f"/v1/sessions/{session_id}/journal"
        started = await client.post(
            url,
            json=_write("plan", plan, "tool:plan", workflow_id=workflow_id),
            headers=_runner(session_id),
        )
        assert started.status_code == 200, started.text
        assert started.json()["workflow_id"] == workflow_id
        explored = await client.post(
            url,
            json=_write("status", {"status": status, "text": "x"}, "tool:s", step="explore"),
            headers=_runner(session_id),
        )
        assert explored.json()["workflow_id"] == workflow_id

    # Only a plan names a workflow, and only one of the caller's own.
    note = _write("note", {"body": "x"}, "tool:note", workflow_id=workflow_id)
    refused = await client.post(f"/v1/sessions/{first}/journal", json=note, headers=_runner(first))
    assert refused.status_code == 400 and "only a plan names a workflow" in refused.text
    bobs = _session(db_uri, title="Bob's session", owner=BOB)
    foreign = await client.post(
        f"/v1/sessions/{bobs}/journal",
        json=_write("plan", plan, "tool:plan", workflow_id=workflow_id),
        headers=_runner(bobs, BOB),
    )
    assert foreign.status_code == 400 and "workflow_not_found" in foreign.text
    # A run in a session alice can't read stays off her list.
    SessionJournalStore(db_uri).append(
        bobs, "plan", plan, idempotency_key="direct", workflow_id=workflow_id
    )

    url = f"/v1/workflows/{workflow_id}/runs"
    runs = (await client.get(url, headers=_headers(ALICE))).json()
    by_session = {run["session_id"]: run for run in runs["data"]}
    assert set(by_session) == {first, second} and runs["next_cursor"] is None
    assert by_session[first]["title"] == "Fix the upload test"
    first_plan = SessionJournalStore(db_uri).current_plan(first)
    assert by_session[first]["id"] == first_plan.id
    assert by_session[first]["session_status"] == "idle"
    assert all(run["is_current"] for run in by_session.values())
    assert by_session[first]["progress"] == {
        "total": 2,
        "started": 0,
        "done": 1,
        "needs_input": 0,
        "failed": 0,
    }
    assert by_session[second]["progress"]["needs_input"] == 1
    # The steps and their newest statuses, for the run's timeline and graph.
    assert [step["id"] for step in by_session[first]["steps"]] == ["explore", "fix"]
    assert [(s["step"], s["content"]["status"]) for s in by_session[first]["statuses"]] == [
        ("explore", "done")
    ]

    # Old journals have no revision metadata, but still have plan chronology.
    legacy_content = {
        key: value for key, value in first_plan.content.items() if key != "graph_revision"
    }
    with sa.create_engine(db_uri).begin() as connection:
        connection.execute(
            sa.update(SqlSessionJournalEntry)
            .where(SqlSessionJournalEntry.id == first_plan.id)
            .values(content=SessionJournalStore(db_uri)._stored_contents([legacy_content])[0])
        )
    legacy_runs = (await client.get(url, headers=_headers(ALICE))).json()["data"]
    legacy_run = next(run for run in legacy_runs if run["id"] == first_plan.id)
    assert legacy_run["plan_graph_revision"] == first_plan.position
    SessionJournalStore(db_uri).append(
        first,
        "plan",
        {**plan, "name": "Renamed triage"},
        idempotency_key="legacy-save",
        start_run=False,
    )
    legacy_runs = (await client.get(url, headers=_headers(ALICE))).json()["data"]
    legacy_run = next(run for run in legacy_runs if run["id"] == first_plan.id)
    assert legacy_run["plan_graph_revision"] == first_plan.position

    journal_url = f"/v1/sessions/{first}/journal"
    latest = await client.get(
        journal_url, params={"kind": "plan", "order": "desc", "limit": 1}, headers=_headers(ALICE)
    )
    assert latest.json()["data"][0]["content"]["graph_revision"] == first_plan.position
    unchanged = await client.post(
        journal_url,
        json=_write("plan", plan, "legacy-post-save"),
        headers=_runner(first),
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["content"]["graph_revision"] == first_plan.position
    assert SessionJournalStore(db_uri).current_plan(first).content["graph_revision"] is None

    conversations = SqlAlchemyConversationStore(db_uri)
    child = _session(db_uri, parent_conversation_id=first)
    for _ in range(60):
        _session(db_uri, parent_conversation_id=first)

    def refuse_child_scan(*args, **kwargs):
        pytest.fail("A workflow run page must not enumerate historical children")

    monkeypatch.setattr(
        SqlAlchemyConversationStore, "list_child_conversation_ids_by_parent", refuse_child_scan
    )
    # A parent's turn can be idle while its child or background work continues.
    for child_status in ("running", "waiting", "idle"):
        _publish_status(child, child_status)
        await poll_until_async(
            lambda expected=child_status != "idle": (
                session_live_state.has_active_child(first) == expected
            )
        )
        page = (await client.get(url, headers=_headers(ALICE))).json()
        parent_run = next(run for run in page["data"] if run["session_id"] == first)
        assert parent_run["session_status"] == ("idle" if child_status == "idle" else "running")
        assert parent_run["background_work_running"] is False
    for background_running in (True, False):
        conversations.set_background_work_running(first, background_running)
        page = (await client.get(url, headers=_headers(ALICE))).json()
        parent_run = next(run for run in page["data"] if run["session_id"] == first)
        assert parent_run["session_status"] == "idle"
        assert parent_run["background_work_running"] is background_running

    # One run a page: three pages, one of them empty where bob's run was left out.
    pages, cursor = [], ""
    while cursor is not None:
        page = (
            await client.get(
                f"{url}?limit=1&before={cursor}" if cursor else f"{url}?limit=1",
                headers=_headers(ALICE),
            )
        ).json()
        pages.append([run["session_id"] for run in page["data"]])
        cursor = page["next_cursor"]
    assert len(pages) == 3
    assert sorted(run for page in pages for run in page) == sorted([first, second])

    assert (await client.get(url, headers=_headers(BOB))).status_code == 404
    bad = await client.get(f"{url}?before=nope", headers=_headers(ALICE))
    assert bad.status_code == 400

    journal = SessionJournalStore(db_uri)
    # A replan can add work after the original final step is done.
    journal.append(
        first, "status", {"status": "done", "text": "Fixed"}, idempotency_key="fixed", step="fix"
    )
    expanded_steps = [*steps, {"id": "publish", "name": "Publish", "depends_on": ["fix"]}]
    journal.append(first, "plan", {**plan, "steps": expanded_steps}, idempotency_key="replan")
    journal.append(
        first, "status", {"status": "started"}, idempotency_key="publishing", step="publish"
    )
    page = (await client.get(url, headers=_headers(ALICE))).json()
    original = next(run for run in page["data"] if run["id"] == first_plan.id)
    assert original["is_current"]
    assert original["plan_graph_revision"] == journal.current_plan(first).content["graph_revision"]
    assert [step["id"] for step in original["steps"]] == ["explore", "fix", "publish"]
    assert original["progress"] == {
        "total": 3,
        "started": 1,
        "done": 2,
        "needs_input": 0,
        "failed": 0,
    }
    assert [(s["step"], s["content"]["status"]) for s in original["statuses"]] == [
        ("explore", "done"),
        ("fix", "done"),
        ("publish", "started"),
    ]
    journal.append(first, "plan", plan, idempotency_key="another-run", workflow_id=workflow_id)
    page = (await client.get(url, headers=_headers(ALICE))).json()
    first_runs = [run for run in page["data"] if run["session_id"] == first]
    assert [run["is_current"] for run in first_runs] == [True, False]
    assert [step["id"] for step in first_runs[0]["steps"]] == ["explore", "fix"]
    assert first_runs[0]["statuses"] == []
    assert first_runs[1]["steps"] == original["steps"]
    assert first_runs[1]["statuses"] == original["statuses"]
    journal.append(
        first, "plan", plan, idempotency_key="another-workflow", workflow_id=uuid.uuid4().hex
    )
    _publish_status(first, "running")
    conversations.set_background_work_running(first, True)
    page = (await client.get(url, headers=_headers(ALICE))).json()
    for historical in (run for run in page["data"] if run["session_id"] == first):
        assert historical["is_current"] is False
        assert historical["session_status"] == "idle"
        assert historical["background_work_running"] is False


@pytest.mark.parametrize("supersede_at", ["before_dispatch", "runner_lookup", "sdk_routing"])
async def test_an_automations_runs_list_the_steps_of_their_latest_plans(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    supersede_at: str,
) -> None:
    from omnigent.server.routes import sessions as sessions_mod
    from tests.server.integration.test_sessions_harness_override import _stub_late_runner_bind

    tasks = SqlAlchemyScheduledTaskStore(db_uri)
    task = tasks.create(
        uuid.uuid4().hex,
        "triage",
        '/workflow Run the saved workflow "Triage" (ID ' + "9" * 32 + ").",
        "FREQ=DAILY;BYHOUR=7;BYMINUTE=0",
        ALICE,
        uuid.uuid4().hex,
        "UTC",
    )
    steps = [
        {"id": "explore", "name": "Explore", "depends_on": []},
        {"id": "fix", "name": "Fix", "depends_on": ["explore"]},
    ]
    planned, idle = _session(db_uri), _session(db_uri)
    run_ids = []
    for index, session_id in enumerate((planned, idle)):
        run = tasks.create_run(
            uuid.uuid4().hex,
            task.id,
            "running",
            1_762_498_800 + index,
            conversation_id=session_id,
            fired_at=1_762_498_801 + index,
        )
        SqlAlchemyConversationStore(db_uri).set_labels(
            session_id, {SCHEDULED_TASK_RUN_LABEL_KEY: run.id}
        )
        run_ids.append(run.id)
    url = f"/v1/sessions/{planned}/journal"
    for body in (
        _write("plan", {"name": "Triage", "steps": steps}, "tool:1"),
        _write("status", {"status": "done", "text": "Found it"}, "tool:2", step="explore"),
    ):
        assert (await client.post(url, json=body, headers=_runner(planned))).status_code == 200

    page = (
        await client.get(f"/v1/scheduled-tasks/{task.id}/run-steps", headers=_headers(ALICE))
    ).json()
    # The run whose session wrote no plan has no steps to show.
    assert [run["run_id"] for run in page["runs"]] == [run_ids[0]]
    (shown,) = page["runs"]
    assert shown["is_current"] is True
    plan_id = shown["plan_id"]
    assert shown["plan_name"] == "Triage"
    assert [step["id"] for step in shown["steps"]] == ["explore", "fix"]
    assert [(s["step"], s["content"]["status"]) for s in shown["statuses"]] == [
        ("explore", "done")
    ]
    assert page["next_cursor"] is None
    other = await client.get(f"/v1/scheduled-tasks/{task.id}/run-steps", headers=_headers(BOB))
    assert other.status_code == 404

    # Exercise dispatch through the real route while capturing the runner transport.
    forwarded = _stub_late_runner_bind(monkeypatch)
    action = {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": "Stop the plan"}]},
        "workflow_run_id": plan_id,
    }
    events_url = f"/v1/sessions/{planned}/events"
    current = await client.post(events_url, json=action, headers=_headers(ALICE))
    assert current.status_code == 202, current.text
    assert forwarded.get("posts")
    assert (
        SqlAlchemyConversationStore(db_uri)
        .get_conversation(planned)
        .labels[SCHEDULED_TASK_RUN_LABEL_KEY]
        == run_ids[0]
    )

    expanded_steps = [*steps, {"id": "publish", "name": "Publish", "depends_on": ["fix"]}]
    for body in (
        _write("status", {"status": "done", "text": "Fixed"}, "tool:fixed", step="fix"),
        _write("plan", {"name": "Triage", "steps": expanded_steps}, "tool:replan"),
        _write("status", {"status": "started"}, "tool:publishing", step="publish"),
    ):
        response = await client.post(url, json=body, headers=_runner(planned))
        assert response.status_code == 200, response.text
    runs_url = f"/v1/scheduled-tasks/{task.id}/run-steps"
    shown = (await client.get(runs_url, headers=_headers(ALICE))).json()["runs"][0]
    assert shown["is_current"]
    assert shown["plan_id"] == plan_id
    latest_plan = session_journal_store.SessionJournalStore(db_uri).current_plan(planned)
    assert latest_plan is not None
    assert shown["plan_graph_revision"] == latest_plan.content["graph_revision"]
    assert [step["id"] for step in shown["steps"]] == ["explore", "fix", "publish"]
    assert [(s["step"], s["content"]["status"]) for s in shown["statuses"]] == [
        ("explore", "done"),
        ("fix", "done"),
        ("publish", "started"),
    ]
    current = await client.post(events_url, json=action, headers=_headers(ALICE))
    assert current.status_code == 202, current.text

    # A newer continuation supersedes the old run even before it writes a plan.
    latest = tasks.create_run(
        uuid.uuid4().hex,
        task.id,
        "running",
        1_762_498_803,
        conversation_id=planned,
        fired_at=1_762_498_804,
    )

    def supersede() -> None:
        SqlAlchemyConversationStore(db_uri).set_labels(
            planned, {SCHEDULED_TASK_RUN_LABEL_KEY: latest.id}
        )

    conversations = SqlAlchemyConversationStore(db_uri)
    if supersede_at == "runner_lookup":
        get_runner_client = sessions_mod._get_runner_client

        async def advance_before_runner(*args, **kwargs):
            supersede()
            return await get_runner_client(*args, **kwargs)

        monkeypatch.setattr(sessions_mod, "_get_runner_client", advance_before_runner)
    elif supersede_at == "sdk_routing":
        from omnigent.server import smart_routing

        conversations.update_conversation(planned, cost_control_mode_override="on")

        async def advance_during_routing(*args, **kwargs):
            supersede()
            return None, None, None

        monkeypatch.setattr(smart_routing, "route_turn_or_decline", advance_during_routing)
    else:
        supersede()
    forwarded.clear()
    before = conversations.list_items(planned, limit=100).data
    stale = await client.post(events_url, json=action, headers=_headers(ALICE))
    assert stale.status_code == 409 and "no longer current" in stale.text
    assert not forwarded.get("posts")
    assert conversations.list_items(planned, limit=100).data == before
    assert (
        SqlAlchemyConversationStore(db_uri)
        .get_conversation(planned)
        .labels[SCHEDULED_TASK_RUN_LABEL_KEY]
        == latest.id
    )
    assert not (await client.get(runs_url, headers=_headers(ALICE))).json()["runs"][0][
        "is_current"
    ]
    # The continued run reuses its plan without an explicit plan write.
    blocked = _write("status", {"status": "started"}, "tool:next-fix", step="fix")
    response = await client.post(url, json=blocked, headers=_runner(planned))
    assert response.status_code == 409 and "step_not_ready" in response.text
    body = _write("status", {"status": "started"}, "tool:next-run", step="explore")
    assert (await client.post(url, json=body, headers=_runner(planned))).status_code == 200
    runs = (await client.get(runs_url, headers=_headers(ALICE))).json()["runs"]
    assert [(run["run_id"], run["is_current"]) for run in runs] == [
        (latest.id, True),
        (run_ids[0], False),
    ]
    assert [(s["step"], s["content"]["status"]) for s in runs[0]["statuses"]] == [
        ("explore", "started")
    ]
    assert runs[1]["plan_id"] == plan_id
    assert runs[1]["steps"] == shown["steps"]
    assert runs[1]["statuses"] == shown["statuses"]
