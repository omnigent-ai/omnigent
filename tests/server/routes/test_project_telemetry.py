"""Product telemetry from existing data: session classes, counting rules and visibility."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import httpx
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlConversationItem
from omnigent.db.enum_codecs import ITEM_TYPE
from omnigent.db.utils import now_epoch
from omnigent.entities.project import PROJECT_IDENTITY_LABEL_KEY
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.project_status import status_period
from omnigent.server.project_telemetry import (
    PersonMessage,
    ProjectSession,
    TelemetryInputs,
    classify_session,
    compute_telemetry,
    review_rounds_and_stalled,
    visible_people,
)
from tests.server.routes.test_projects_crud import (  # noqa: F401 - pytest fixtures
    _as_user,
    multi_user_app,
    multi_user_client,
    project_agents,
)

NOW = now_epoch()
OWNER, MEMBER = "owner@example.com", "member@example.com"


def _message(text: str, role: str = "user") -> str:
    return json.dumps({"role": role, "content": [{"type": "input_text", "text": text}]})


def test_sessions_are_classified() -> None:
    project = SimpleNamespace(main_session_id="lead", retired_main_session_ids=["old"])

    def kind(session_id, root=None, family=None, labels=None):
        return classify_session(
            project, session_id, root_id=root, family_id=family, labels=labels or {}
        )

    assert kind("lead") == kind("old") == "lead"
    assert kind("r", labels={PROJECT_IDENTITY_LABEL_KEY: "pm"}) == "role"
    assert kind("w", family="lead") == "worker"
    assert kind("sub", root="lead") == "sub_agent"
    assert kind("mine", family="mine") == "personal"


def _inputs(**overrides) -> TelemetryInputs:
    project = SimpleNamespace(user_id=OWNER, main_session_id="lead", retired_main_session_ids=[])
    sessions = [
        ProjectSession("mine", NOW - 60, "personal", OWNER),
        ProjectSession("old-mine", NOW - 40 * 86_400, "personal", OWNER),
        ProjectSession("lead", NOW - 50 * 86_400, "lead", OWNER),
        ProjectSession("pm", NOW - 50 * 86_400, "role", OWNER),
        ProjectSession("chat", NOW - 30, "side_chat", MEMBER, side_chat_of="role"),
        ProjectSession("worker", NOW - 30, "worker", OWNER),
    ]
    messages = [
        PersonMessage("lead", OWNER, NOW - 10),
        PersonMessage("pm", OWNER, NOW - 9),
        PersonMessage("chat", MEMBER, NOW - 8),
        PersonMessage("pm", OWNER, NOW - 40 * 86_400),  # before the window
    ]
    activity = [
        {
            "ticket_id": "a",
            "kind": "created",
            "user_id": OWNER,
            "session_id": None,
            "created_at": NOW - 5,
        },
        {
            "ticket_id": "b",
            "kind": "created",
            "user_id": OWNER,
            "session_id": "mine",
            "created_at": NOW - 5,
        },
        {
            "ticket_id": "c",
            "kind": "created",
            "user_id": OWNER,
            "session_id": "lead",
            "created_at": NOW - 5,
        },
        {
            "ticket_id": "a",
            "kind": "comment",
            "user_id": "third@example.com",
            "session_id": None,
            "created_at": NOW - 4,
        },
        {
            "ticket_id": "a",
            "kind": "comment",
            "user_id": OWNER,
            "session_id": "lead",
            "created_at": NOW - 4,
        },
    ]
    members = {"mine": f"user:{OWNER}", "lead": "identity:orchestrator"}

    def member_of(event):
        session_id = event.get("session_id")
        return members[session_id] if session_id else f"user:{event['user_id']}"

    identities = [
        SimpleNamespace(
            created_by_session_id="lead", created_at=NOW - 3600, updated_at=NOW - 3600
        ),
        SimpleNamespace(created_by_session_id=None, created_at=NOW - 7200, updated_at=NOW - 3600),
        SimpleNamespace(created_by_session_id=None, created_at=NOW - 99 * 86_400, updated_at=None),
    ]
    values = {
        "project": project,
        "period": status_period("7d", NOW),
        "sessions": lambda: sessions,
        "person_messages": lambda found: messages,
        "activity": lambda: activity,
        "identities": lambda: identities,
        "member_of_move": member_of,
    }
    return TelemetryInputs(**{**values, **overrides})


def test_compute_counts_each_kpi_by_its_rule() -> None:
    result = compute_telemetry(_inputs())
    assert result["work_start"] == {
        "sessions_started_by_people": 1,
        "tickets_filed_directly": 1,
        "tickets_filed_from_a_chat": 1,
        "tickets_filed_by_agents": 1,
        "people": {"session_first": 0, "ticket_first": 0, "both": 1},
    }
    interaction = result["role_interaction"]
    assert (interaction["messages_to_lead"], interaction["messages_to_roles"]) == (1, 2)
    assert interaction["people_messaging_roles"] == 2
    assert (interaction["side_chats_opened"], interaction["side_chats_with_roles"]) == (1, 1)
    # An agent's comment doesn't count; the third person only commented.
    assert (interaction["ticket_comments_by_people"], interaction["people_ticket_only"]) == (1, 1)
    team = result["team_building"]
    assert (team["roles"], team["roles_created_by_lead"], team["roles_created_by_hand"]) == (
        3,
        1,
        2,
    )
    assert (team["roles_created_in_window"], team["roles_created_in_window_by_lead"]) == (2, 1)
    assert (team["roles_edited_after_creation"], team["median_hours_to_last_role_edit"]) == (
        1,
        1.0,
    )
    owner_row = next(row for row in result["people_rows"] if row["user_id"] == OWNER)
    assert (owner_row["sessions_started"], owner_row["tickets_filed"]) == (1, 2)


def test_message_counts_are_not_measured_without_authors() -> None:
    result = compute_telemetry(_inputs(person_messages=lambda found: None))
    interaction = result["role_interaction"]
    assert interaction["messages_to_lead"] is None
    assert interaction["people_ticket_only"] is None
    assert interaction["side_chats_opened"] == 1
    # A person's row says "not measured", not "never messaged".
    assert {
        (row["messages_to_lead"], row["messages_to_roles"]) for row in result["people_rows"]
    } == {(None, None)}


def test_review_rounds_and_stalled_work() -> None:
    stages = [
        {"id": "open", "name": "Pending", "is_complete": False},
        {"id": "in_progress", "name": "Working", "is_complete": False},
        {"id": "code_review", "name": "Review", "is_complete": False},
        {"id": "done", "name": "Done", "is_complete": True},
    ]

    def entry(stage, at, created=False):
        return {"stage_id": stage, "is_complete": stage == "done", "at": at, "created": created}

    histories = {
        "twice": [
            entry("open", NOW - 9, True),
            entry("code_review", NOW - 8),
            entry("in_progress", NOW - 7),
            entry("code_review", NOW - 6),
            entry("done", NOW - 5),
        ],
        "once": [
            entry("open", NOW - 9, True),
            entry("code_review", NOW - 8),
            entry("done", NOW - 5),
        ],
        "open": [entry("open", NOW - 9, True)],
    }
    ages = [
        ("twice", "done", NOW),
        ("stuck", "in_progress", NOW - 4 * 86_400),
        ("fresh", "in_progress", NOW),
        ("new", "open", NOW - 9 * 86_400),
    ]
    result = review_rounds_and_stalled(histories, stages, ages, status_period("7d", NOW))
    assert result == {"review_rounds_per_shipped_ticket": 1.5, "stalled_tickets": 1}


def test_people_rows_follow_visibility_rules() -> None:
    rows = [{"user_id": OWNER}, {"user_id": MEMBER}]
    assert visible_people(rows, viewer=OWNER, owner=True, agent=False) == rows
    assert visible_people(rows, viewer=MEMBER, owner=False, agent=False) == [{"user_id": MEMBER}]
    assert visible_people(rows, viewer=OWNER, owner=True, agent=True) is None


async def test_telemetry_endpoints_count_real_activity_and_respect_access(
    multi_user_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
    multi_user_app: FastAPI,  # noqa: F811 - imported fixture
    db_uri: str,
) -> None:
    owner, member, stranger = _as_user(OWNER), _as_user(MEMBER), _as_user("stranger@example.com")
    created = (
        await multi_user_client.post("/v1/projects", json={"name": "Telemetry"}, headers=owner)
    ).json()
    path = f"/v1/projects/{created['id']}"
    await multi_user_client.post(f"{path}/shares", json={"user_id": MEMBER}, headers=owner)
    await multi_user_client.post(
        f"{path}/identities", json={"name": "PM", "instructions": "x"}, headers=owner
    )
    stages = (await multi_user_client.get(f"{path}/tickets", headers=owner)).json()["stages"]
    ticket = (
        await multi_user_client.post(f"{path}/tickets", json={"title": "T"}, headers=owner)
    ).json()
    await multi_user_client.post(
        f"{path}/tickets/{ticket['id']}/comments", json={"text": "Looks good"}, headers=member
    )
    done = next(stage["id"] for stage in stages if stage["is_complete"])
    await multi_user_client.patch(
        f"{path}/tickets/{ticket['id']}", json={"stage_id": done}, headers=owner
    )
    # An automatic notice the server stores like a person's message, attributed to the owner.
    with Session(sa.create_engine(db_uri)) as session:
        for position, text in enumerate(["Automatic ticket update for project p."]):
            session.add(
                SqlConversationItem(
                    workspace_id=0,
                    conversation_id=created["main_session_id"],
                    id=uuid.uuid4().hex,
                    response_id="r",
                    created_at=now_epoch(),
                    status=1,
                    position=1000 + position,
                    type=ITEM_TYPE["message"],
                    data=_message(text),
                    search_text=text,
                    created_by=OWNER,
                )
            )
        session.commit()

    response = await multi_user_client.get(f"{path}/telemetry", headers=owner)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["work_start"]["tickets_filed_directly"] == 1
    # Stored messages can't tell a person's words from the server's notices: not measured.
    assert body["role_interaction"]["messages_to_lead"] is None
    assert body["role_interaction"]["messages_to_roles"] is None
    assert body["role_interaction"]["ticket_comments_by_people"] == 1
    assert body["role_interaction"]["people_ticket_only"] is None
    assert body["team_building"]["roles_created_by_hand"] == 1
    assert body["throughput"]["tickets_shipped"] == 1
    assert {row["user_id"] for row in body["people"]} == {OWNER, MEMBER}

    as_member = (await multi_user_client.get(f"{path}/telemetry", headers=member)).json()
    assert [row["user_id"] for row in as_member["people"]] == [MEMBER]
    assert (await multi_user_client.get(f"{path}/telemetry", headers=stranger)).status_code == 404

    overview = (await multi_user_client.get("/v1/telemetry", headers=owner)).json()
    assert [project["project_id"] for project in overview["projects"]] == [created["id"]]
    # A shared project isn't in the member's own overview.
    assert (await multi_user_client.get("/v1/telemetry", headers=member)).json()["projects"] == []
    # An agent reads its project's totals, without people, and never the overview.
    agent = {**owner, ORCHESTRATION_SOURCE_HEADER: created["main_session_id"]}
    as_agent = (await multi_user_client.get(f"{path}/telemetry", headers=agent)).json()
    assert (as_agent["people"], as_agent["work_start"]["tickets_filed_directly"]) == (None, 1)
    assert (await multi_user_client.get("/v1/telemetry", headers=agent)).status_code == 403
