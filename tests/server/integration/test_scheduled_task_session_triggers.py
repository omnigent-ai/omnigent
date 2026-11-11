"""Session-triggered automations through the ``/v1/scheduled-tasks`` routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.scheduled import session_triggers
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.conftest import ControllableMockClient
from tests.server.integration import test_scheduled_tasks_routes as routes_tests
from tests.server.integration.test_scheduled_tasks_routes import (
    _build_app,
    _create_body,
    _headers,
    _make_user,
    _register_host,
    _serve,
    _stub_host_workspace_validation,  # noqa: F401  (autouse: stubs the host workspace check)
)

pytestmark = pytest.mark.asyncio

_HOST = "4b653f6031f35d168cc0b37caa1306d1"
_TRIGGER = {"provider": "omnigent", "events": ["turn.completed", "session.created"]}


def _trigger_body(**overrides: object) -> dict[str, object]:
    body = _create_body(**{"trigger_config": _TRIGGER, **overrides})
    body.pop("rrule")
    return body


@pytest_asyncio.fixture()
async def client(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    mock_llm: ControllableMockClient,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    # param: (database scheduler, session_triggers feature)
    persistent, triggers = getattr(request, "param", (True, True))
    resolve = routes_tests.resolve_feature_flags

    def with_triggers(env: dict[str, str]) -> object:
        suffix = ",session_triggers" if triggers else ""
        return resolve({"OMNIGENT_FEATURES": env["OMNIGENT_FEATURES"] + suffix})

    monkeypatch.setattr(routes_tests, "resolve_feature_flags", with_triggers)
    app: FastAPI = _build_app(db_uri, tmp_path, persistent=persistent)
    _make_user(db_uri)
    _register_host(app, _HOST, "alice@example.com")
    async with _serve(app, mock_llm, tmp_path) as client:
        yield client


async def test_create_a_session_triggered_automation(client: httpx.AsyncClient) -> None:
    info = (await client.get("/v1/info", headers=_headers())).json()
    assert info["scheduled_task_session_triggers"] is True
    created = await client.post("/v1/scheduled-tasks", json=_trigger_body(), headers=_headers())
    assert created.status_code == 200, created.text
    task = created.json()
    assert task["rrule"] is None
    assert task["next_run_at"] is None
    # Stored normalized: events sorted, defaults filled in.
    assert task["trigger_config"] == {
        "provider": "omnigent",
        "events": ["session.created", "turn.completed"],
        "include_own_sessions": False,
    }
    listed = (await client.get("/v1/scheduled-tasks?event_triggers=1", headers=_headers())).json()[
        "scheduled_tasks"
    ]
    assert [t["trigger_config"] for t in listed] == [task["trigger_config"]]
    url = f"/v1/scheduled-tasks/{task['id']}"
    assert (await client.get(f"{url}?event_triggers=1", headers=_headers())).status_code == 200
    # Clients that predate event triggers expect a rule on every task, so they don't see it.
    older = (await client.get("/v1/scheduled-tasks", headers=_headers())).json()
    assert older["scheduled_tasks"] == []
    assert (await client.get(url, headers=_headers())).status_code == 404


@pytest.mark.parametrize(
    ("body", "status", "message"),
    [
        (_create_body(trigger_config=_TRIGGER), 422, "exactly one of rrule"),
        ({k: v for k, v in _create_body().items() if k != "rrule"}, 422, "exactly one of rrule"),
        (_trigger_body(trigger_config={"provider": "jira", "events": []}), 400, "provider"),
        (
            _trigger_body(trigger_config={"provider": "omnigent", "events": ["turn.started"]}),
            400,
            "invalid trigger_config: events.0",
        ),
        (
            _trigger_body(
                trigger_config={"provider": "omnigent", "events": ["turn.failed"], "x": 1}
            ),
            400,
            "invalid trigger_config: x",
        ),
    ],
    ids=["both", "neither", "provider", "event-type", "unknown-field"],
)
async def test_create_rejects_invalid_triggers(
    client: httpx.AsyncClient, body: dict[str, object], status: int, message: str
) -> None:
    response = await client.post("/v1/scheduled-tasks", json=body, headers=_headers())
    assert response.status_code == status, response.text
    assert message in response.text


@pytest.mark.parametrize(
    "client", [(False, True), (True, False)], indirect=True, ids=["legacy-timers", "flag-off"]
)
async def test_event_triggers_need_the_scheduler_and_the_feature(
    client: httpx.AsyncClient,
) -> None:
    # Older servers would pause event tasks, so the feature stays off until all have it.
    info = (await client.get("/v1/info", headers=_headers())).json()
    assert info["scheduled_task_session_triggers"] is False
    response = await client.post("/v1/scheduled-tasks", json=_trigger_body(), headers=_headers())
    assert response.status_code == 400, response.text
    assert "not enabled on this server" in response.text


async def test_patch_switches_between_schedule_and_events(client: httpx.AsyncClient) -> None:
    created = await client.post("/v1/scheduled-tasks", json=_trigger_body(), headers=_headers())
    url = f"/v1/scheduled-tasks/{created.json()['id']}"
    # Removing the trigger without a schedule to fall back on is refused.
    refused = await client.patch(url, json={"trigger_config": None}, headers=_headers())
    assert refused.status_code == 400, refused.text
    scheduled = await client.patch(url, json={"rrule": "FREQ=DAILY"}, headers=_headers())
    assert scheduled.status_code == 200, scheduled.text
    assert (scheduled.json()["rrule"], scheduled.json()["trigger_config"]) == ("FREQ=DAILY", None)
    assert scheduled.json()["next_run_at"] is not None
    evented = await client.patch(
        url,
        json={"trigger_config": {"provider": "omnigent", "events": ["turn.failed"]}},
        headers=_headers(),
    )
    assert evented.status_code == 200, evented.text
    assert evented.json()["rrule"] is None
    assert evented.json()["trigger_config"]["events"] == ["turn.failed"]
    assert evented.json()["next_run_at"] is None
    both = await client.patch(
        url, json={"rrule": "FREQ=DAILY", "trigger_config": _TRIGGER}, headers=_headers()
    )
    assert both.status_code == 422, both.text


async def test_a_session_event_shows_as_a_pending_run(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    created = await client.post("/v1/scheduled-tasks", json=_trigger_body(), headers=_headers())
    task_id = created.json()["id"]
    conversations = SqlAlchemyConversationStore(db_uri)
    conv = conversations.create_conversation(agent_id=created.json()["agent_id"])
    SqlAlchemyPermissionStore(db_uri).grant("alice@example.com", conv.id, LEVEL_OWNER)
    future = session_triggers.notify("turn.completed", conv.id, occurrence="resp_1")
    assert future is not None
    future.result(timeout=10)
    runs = (await client.get(f"/v1/scheduled-tasks/{task_id}/runs", headers=_headers())).json()
    [run] = runs["runs"]
    assert run["status"] == "scheduled"
    [event] = run["event"]["events"]
    assert (event["type"], event["session_id"]) == ("turn.completed", conv.id)


async def test_a_sandbox_session_trigger_must_continue_one_session(
    client: httpx.AsyncClient,
) -> None:
    body = _trigger_body(execution_target="managed_sandbox")
    body.pop("host_id")
    body.pop("workspace")
    response = await client.post("/v1/scheduled-tasks", json=body, headers=_headers())
    assert response.status_code == 400, response.text
    assert "must continue one session" in response.text
    created = await client.post(
        "/v1/scheduled-tasks", json=_trigger_body(session_mode="continue"), headers=_headers()
    )
    assert created.status_code == 200, created.text
    switched = await client.patch(
        f"/v1/scheduled-tasks/{created.json()['id']}",
        json={"session_mode": "new", "execution_target": "managed_sandbox"},
        headers=_headers(),
    )
    assert switched.status_code == 400, switched.text
    assert "must continue one session" in switched.text


async def test_a_failed_reader_lookup_does_not_stop_a_session_delete(
    client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await client.post(
        "/v1/scheduled-tasks",
        json=_trigger_body(trigger_config={"provider": "omnigent", "events": ["session.deleted"]}),
        headers=_headers(),
    )
    task_id = created.json()["id"]
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(
        agent_id=created.json()["agent_id"]
    )
    SqlAlchemyPermissionStore(db_uri).grant("alice@example.com", conv.id, LEVEL_OWNER)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("task store unavailable")

    sent: list[str] = []
    monkeypatch.setattr(session_triggers, "readers_of", fail)
    monkeypatch.setattr(session_triggers, "notify", lambda kind, *a, **kw: sent.append(kind))
    response = await client.delete(f"/v1/sessions/{conv.id}", headers=_headers())
    assert response.status_code == 200, response.text
    assert SqlAlchemyConversationStore(db_uri).get_conversation(conv.id) is None
    # Without who could see it, the deletion is not reported.
    assert sent == []
    runs = (await client.get(f"/v1/scheduled-tasks/{task_id}/runs", headers=_headers())).json()
    assert runs["runs"] == []
