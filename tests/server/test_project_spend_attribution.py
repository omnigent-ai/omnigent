"""Spend by role and by ticket: the attribution rules, and telemetry's role filter."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from omnigent.server.project_spend_attribution import spend_by_role, spend_by_ticket
from omnigent.server.project_status import LEAD_MEMBER_KEY, WORKERS_MEMBER_KEY
from omnigent.server.project_status_spend import micros_to_cents
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.routes.test_projects_crud import (  # noqa: F401 - pytest fixtures
    _bind_runner,
    project_agents,
    project_app,
    project_client,
)

DAY = "2026-06-10"
NOON = int(datetime(2026, 6, 10, 12, tzinfo=UTC).timestamp())


def test_roles_pool_people_and_keep_each_agent_role() -> None:
    rows = [
        ("lead", DAY, 1_000_000, LEAD_MEMBER_KEY),
        ("pm", DAY, 2_500_000, "identity:pm"),
        ("ann", DAY, 300_000, "user:ann@example.com"),
        ("bob", DAY, 200_000, "user:bob@example.com"),
        ("worker", DAY, 100_000, WORKERS_MEMBER_KEY),
    ]
    assert [
        (row["name"], row["spend_usd"]) for row in spend_by_role(rows, {"identity:pm": "PM"})
    ] == [
        ("PM", 2.5),
        ("Lead", 1.0),
        ("People's own chats", 0.5),
        ("Other agents", 0.1),
    ]


def test_a_session_day_goes_to_tickets_it_acted_on_else_its_open_links() -> None:
    rows = [
        # Acted on A twice and B once that day: split 2:1.
        ("pm", DAY, 3_000_000, "identity:pm"),
        # A sub-agent's events count for its root.
        ("lead", DAY, 1_000_000, LEAD_MEMBER_KEY),
        # No activity: split over linked tickets still open that day (C and D, not done E).
        ("reviewer", DAY, 2_000_001, "identity:reviewer"),
        # Nothing to credit.
        ("idle", DAY, 500_000, "identity:idle"),
    ]
    root_of = {"lead-sub": "lead"}
    activity = [
        ("A", "pm", NOON),
        ("A", "pm", NOON + 1),
        ("B", "pm", NOON),
        ("F", "lead-sub", NOON),
    ]
    links = [
        ("C", "reviewer", NOON - 86_400),
        ("D", "reviewer", NOON - 86_400),
        ("E", "reviewer", NOON - 86_400),
        # Linked only after the day ended.
        ("G", "idle", NOON + 86_400),
    ]
    finished = {"E": NOON - 86_400, "D": NOON}
    split = spend_by_ticket(rows, root_of, activity, links, finished, {"A": "Alpha"}, top=3)
    assert [(row["ticket_id"], row["title"], row["spend_usd"]) for row in split["tickets"]] == [
        ("A", "Alpha", 2.0),
        # The odd micro-dollar of an even split goes to one ticket, so D edges out B, C and F.
        ("D", None, 1.0),
        ("B", None, 1.0),
    ]
    assert (split["tickets_with_spend"], split["other_tickets_usd"]) == (5, 2.0)
    assert (split["attributed_usd"], split["unattributed_usd"]) == (6.0, 0.5)
    # No double counting: every session-day is split once, so the parts add up to all spend.
    listed = sum(row["spend_usd"] for row in split["tickets"])
    assert listed + split["other_tickets_usd"] == split["attributed_usd"]
    assert (
        split["attributed_usd"] + split["unattributed_usd"]
        == micros_to_cents(sum(row[2] for row in rows)) / 100
    )


async def test_telemetry_splits_spend_and_filters_to_one_role(
    project_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
    db_uri: str,
) -> None:
    created = (await project_client.post("/v1/projects", json={"name": "zz-spend"})).json()
    path = f"/v1/projects/{created['id']}"
    pm = (
        await project_client.post(f"{path}/identities", json={"name": "PM", "instructions": "x"})
    ).json()
    stages = (await project_client.get(f"{path}/tickets")).json()["stages"]
    done = next(stage["id"] for stage in stages if stage["is_complete"])
    mine = (
        await project_client.post(
            f"{path}/tickets",
            json={"title": "PM work", "labels": [{"key": "assignee", "value": "PM"}]},
        )
    ).json()
    other = (await project_client.post(f"{path}/tickets", json={"title": "Other"})).json()
    for ticket in (mine, other):
        await project_client.patch(f"{path}/tickets/{ticket['id']}", json={"stage_id": done})
    worker = _bind_runner(db_uri, pm["active_session_id"], "pm-secret")
    commented = await project_client.post(
        f"{path}/tickets/{mine['id']}/comments", json={"text": "On it"}, headers=worker
    )
    assert commented.status_code < 300, commented.text
    conversations = SqlAlchemyConversationStore(db_uri)
    today = datetime.now(UTC).date().isoformat()
    conversations.add_session_daily_cost(pm["active_session_id"], today, 3.0)
    conversations.add_session_daily_cost(created["main_session_id"], today, 1.25)

    body = (await project_client.get(f"{path}/telemetry")).json()
    spend = body["spend"]
    assert body["throughput"]["spend_usd"] == spend["total_usd"] == 4.25
    assert {row["name"]: row["spend_usd"] for row in spend["by_role"]} == {"PM": 3.0, "Lead": 1.25}
    by_ticket = spend["by_ticket"]
    assert by_ticket["tickets"] == [
        {"ticket_id": mine["id"], "title": "PM work", "spend_usd": 3.0}
    ]
    assert by_ticket["attributed_usd"] + by_ticket["unattributed_usd"] == 4.25
    assert body["role"] is None and body["throughput"]["tickets_shipped"] == 2

    role = (await project_client.get(f"{path}/telemetry", params={"identity_id": pm["id"]})).json()
    assert role["role"] == {"identity_id": pm["id"], "name": "PM"}
    assert (role["throughput"]["tickets_shipped"], role["throughput"]["spend_usd"]) == (1, 3.0)
    assert [row["name"] for row in role["spend"]["by_role"]] == ["PM"]
    assert role["spend"]["total_usd"] == 3.0
    missing = await project_client.get(f"{path}/telemetry", params={"identity_id": "nope"})
    assert missing.status_code == 404
