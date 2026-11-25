"""Usage events: enumerated records, retention, the off switch, and what telemetry counts."""

from __future__ import annotations

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.orm import Session

from omnigent.db.utils import now_epoch
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server import usage_events
from omnigent.stores.usage_event_store import UsageEventStore, clean_properties
from tests.server.routes.test_projects_crud import (  # noqa: F401 - pytest fixtures
    PROJECT_AGENT_ID,
    _as_user,
    multi_user_app,
    multi_user_client,
    project_agents,
)

OWNER, MEMBER = "owner@example.com", "member@example.com"
PROJECT = "a" * 32


def test_store_keeps_only_enumerated_fields_and_prunes_old_events(db_uri: str) -> None:
    store = UsageEventStore(db_uri)
    store.record(
        "role.updated",
        project_id=PROJECT,
        user_id=OWNER,
        subject_id="r",
        props={"fields": ["instructions", "secret"], "text": "never stored"},
        created_at=1000,
    )
    store.record("message.sent", project_id=PROJECT, user_id=OWNER, session_id="b" * 32)
    with pytest.raises(ValueError):
        store.record("page.viewed", project_id=PROJECT)
    with pytest.raises(ValueError):
        store.record("session.started", project_id=PROJECT, surface="fax")
    [edit] = store.events(PROJECT, since_epoch=None, names=["role.updated"])
    assert edit.props == {"fields": ["instructions"]}
    assert clean_properties("message.sent", {"fields": ["name"]}) is None
    assert store.first_event_at() == 1000
    assert store.prune(before_epoch=2000) == 1
    assert [
        event.name
        for event in store.events(
            PROJECT, since_epoch=None, names=["role.updated", "message.sent"]
        )
    ] == ["message.sent"]


async def test_recording_points_feed_telemetry(
    multi_user_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
    multi_user_app: FastAPI,  # noqa: F811 - imported fixture
) -> None:
    owner = _as_user(OWNER)
    created = (
        await multi_user_client.post("/v1/projects", json={"name": "Usage"}, headers=owner)
    ).json()
    path = f"/v1/projects/{created['id']}"
    lead = created["main_session_id"]
    role = (
        await multi_user_client.post(
            f"{path}/identities", json={"name": "PM", "instructions": "x"}, headers=owner
        )
    ).json()
    await multi_user_client.patch(
        f"{path}/identities/{role['id']}", json={"instructions": "y"}, headers=owner
    )
    await multi_user_client.post(
        f"{path}/tickets", json={"title": "T"}, headers={**owner, "X-Omnigent-Client": "slack"}
    )
    message = {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": "Hi"}]},
    }
    await multi_user_client.post(f"/v1/sessions/{lead}/events", json=message, headers=owner)
    # An agent's message into the same session isn't a person's.
    await multi_user_client.post(
        f"/v1/sessions/{lead}/events",
        json=message,
        headers={**owner, ORCHESTRATION_SOURCE_HEADER: lead},
    )

    store: UsageEventStore = multi_user_app.state.usage_events
    names = ["message.sent", "role.created", "role.updated", "ticket.filed"]
    events = store.events(created["id"], since_epoch=None, names=names)
    by_name = {name: [event for event in events if event.name == name] for name in names}
    assert len(by_name["message.sent"]) == 1
    assert by_name["role.created"][0].user_id == OWNER
    assert by_name["role.updated"][0].props == {"fields": ["instructions"]}
    assert by_name["ticket.filed"][0].surface == "slack"
    assert all(event.created_at <= now_epoch() for event in events)

    body = (await multi_user_client.get(f"{path}/telemetry", headers=owner)).json()
    assert body["role_interaction"]["messages_to_lead"] == 1
    assert body["role_interaction"]["messages_measured_since"] is not None
    assert (
        body["team_building"]["role_edits_in_window"],
        body["team_building"]["roles_edited_in_window"],
    ) == (1, 1)
    owner_row = next(row for row in body["people"] if row["user_id"] == OWNER)
    assert owner_row["messages_to_lead"] == 1


async def test_session_started_records_project_sessions_only(
    multi_user_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
    multi_user_app: FastAPI,  # noqa: F811 - imported fixture
    db_uri: str,
) -> None:
    """A project session records its client; a session outside any project records nothing."""
    owner = _as_user(OWNER)
    created = (
        await multi_user_client.post("/v1/projects", json={"name": "Started"}, headers=owner)
    ).json()
    store: UsageEventStore = multi_user_app.state.usage_events
    before = store.events(created["id"], since_epoch=None, names=["session.started"])
    in_project = await multi_user_client.post(
        "/v1/sessions",
        json={"agent_id": PROJECT_AGENT_ID, "project_id": created["id"]},
        headers={**owner, "X-Omnigent-Client": "slack"},
    )
    assert in_project.status_code == 201, in_project.text
    personal = await multi_user_client.post(
        "/v1/sessions", json={"agent_id": PROJECT_AGENT_ID}, headers=owner
    )
    assert personal.status_code == 201, personal.text
    after = store.events(created["id"], since_epoch=None, names=["session.started"])
    [started] = [event for event in after if event not in before]
    assert (started.session_id, started.surface, started.user_id) == (
        in_project.json()["id"].replace("-", ""),
        "slack",
        OWNER,
    )
    with Session(sa.create_engine(db_uri)) as session:
        rows = session.execute(
            sa.text("SELECT count(*) FROM usage_events WHERE session_id = :id"),
            {"id": bytes.fromhex(personal.json()["id"].replace("-", ""))},
        ).scalar_one()
    assert rows == 0


async def test_recording_can_be_turned_off(
    multi_user_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
    multi_user_app: FastAPI,  # noqa: F811 - imported fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(usage_events.USAGE_EVENTS_ENV, "off")
    owner = _as_user(OWNER)
    created = (
        await multi_user_client.post("/v1/projects", json={"name": "Off"}, headers=owner)
    ).json()
    path = f"/v1/projects/{created['id']}"
    await multi_user_client.post(f"{path}/tickets", json={"title": "T"}, headers=owner)
    store: UsageEventStore = multi_user_app.state.usage_events
    assert store.events(created["id"], since_epoch=None, names=["ticket.filed"]) == []
    body = (await multi_user_client.get(f"{path}/telemetry", headers=owner)).json()
    assert body["role_interaction"]["messages_to_lead"] is None
    assert body["work_start"]["sessions_by_client"] is None
