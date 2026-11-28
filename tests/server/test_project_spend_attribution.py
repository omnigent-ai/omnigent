"""Spend by role and by ticket: the attribution rules, and telemetry's role filter."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

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


def test_helpers_count_for_the_nearest_creator_with_a_role() -> None:
    from omnigent.server.project_spend_attribution import inherit_creator_roles

    creators = {
        "helper": "sub-helper-parent",
        "sub-helper-parent": "pm-session",
        "lead-helper": "lead-session",
        "orphan": "gone",
        "loop-a": "loop-b",
        "loop-b": "loop-a",
    }
    members = {
        "sub-helper-parent": WORKERS_MEMBER_KEY,
        "pm-session": "identity:pm",
        "lead-session": LEAD_MEMBER_KEY,
        "loop-a": WORKERS_MEMBER_KEY,
        "loop-b": WORKERS_MEMBER_KEY,
    }
    rows = [
        ("helper", DAY, 1, WORKERS_MEMBER_KEY),
        ("lead-helper", DAY, 2, WORKERS_MEMBER_KEY),
        ("orphan", DAY, 3, WORKERS_MEMBER_KEY),
        ("loop-a", DAY, 4, WORKERS_MEMBER_KEY),
        ("pm-session", DAY, 5, "identity:pm"),
    ]
    resolved = inherit_creator_roles(
        rows,
        lambda ids: {i: creators[i] for i in ids if i in creators},
        lambda ids: {i: members[i] for i in ids if i in members},
    )
    assert [member for *_, member in resolved] == [
        "identity:pm",  # Two levels up, through another helper.
        LEAD_MEMBER_KEY,
        WORKERS_MEMBER_KEY,  # Its creator is gone.
        WORKERS_MEMBER_KEY,  # A cycle never resolves.
        "identity:pm",
    ]
    assert sum(micros for _, _, micros, _ in resolved) == sum(micros for _, _, micros, _ in rows)


async def test_telemetry_credits_a_helper_to_the_role_that_started_it(
    project_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
    db_uri: str,
) -> None:
    import sqlalchemy as sa

    from omnigent.db.db_models import SqlConversationMetadata
    from omnigent.db.utils import get_or_create_engine
    from omnigent.stores.conversation_store import CREATED_BY_SESSION_LABEL_KEY

    created = (await project_client.post("/v1/projects", json={"name": "zz-helpers"})).json()
    path = f"/v1/projects/{created['id']}"
    pm = (
        await project_client.post(f"{path}/identities", json={"name": "PM", "instructions": "x"})
    ).json()
    conversations = SqlAlchemyConversationStore(db_uri)
    helper = conversations.create_conversation(
        project_id=created["id"],
        labels={CREATED_BY_SESSION_LABEL_KEY: pm["active_session_id"]},
    )
    with get_or_create_engine(db_uri).begin() as connection:
        connection.execute(
            sa.update(SqlConversationMetadata)
            .where(SqlConversationMetadata.id == helper.id)
            .values(family_id=uuid4().hex)
        )
    today = datetime.now(UTC).date().isoformat()
    conversations.add_session_daily_cost(helper.id, today, 2.0)
    conversations.add_session_daily_cost(pm["active_session_id"], today, 1.0)

    spend = (await project_client.get(f"{path}/telemetry")).json()["spend"]
    assert {row["name"]: row["spend_usd"] for row in spend["by_role"]} == {"PM": 3.0}
    assert spend["total_usd"] == 3.0
    role = (await project_client.get(f"{path}/telemetry", params={"identity_id": pm["id"]})).json()
    assert role["throughput"]["spend_usd"] == 3.0


def test_a_turn_keeps_its_ticket_when_the_session_later_works_on_another() -> None:
    noon = NOON
    rows = [("s", DAY, 3_000_000, "identity:pm")]
    turns = [("s", noon + 10, 1_000_000), ("s", noon + 20, 2_000_000)]
    acted_a = [("A", "s", noon)]
    first = spend_by_ticket(rows, {}, acted_a, [], {}, {}, turns=turns)
    assert {row["ticket_id"]: row["spend_usd"] for row in first["tickets"]} == {"A": 3.0}
    # No new spend, but the session then works on B and is linked to C.
    later = spend_by_ticket(
        rows, {}, [*acted_a, ("B", "s", noon + 30)], [("C", "s", noon + 40)], {}, {}, turns=turns
    )
    assert {row["ticket_id"]: row["spend_usd"] for row in later["tickets"]} == {"A": 3.0}
    # The day-level split it replaces would have moved half of A's spend to B.
    old = spend_by_ticket(rows, {}, [*acted_a, ("B", "s", noon + 30)], [], {}, {})
    assert {row["ticket_id"]: row["spend_usd"] for row in old["tickets"]} == {"A": 1.5, "B": 1.5}


def test_turn_attribution_adds_up_with_unlogged_spend_and_open_links() -> None:
    noon = NOON
    rows = [("s", DAY, 5_000_000, "identity:pm"), ("t", DAY, 1_000_000, "identity:pm")]
    turns = [
        # Before any activity: the open linked ticket L takes it.
        ("s", noon - 100, 1_000_000),
        # After acting on A.
        ("s", noon + 10, 2_000_000),
        # Logged more than the day row recorded: scaled down to fit.
        ("t", noon, 1_500_000),
    ]
    links = [("L", "s", noon - 3600), ("D", "s", noon - 3600)]
    # D finished before the first turn, so it isn't open then.
    finished = {"D": noon - 1800}
    split = spend_by_ticket(rows, {}, [("A", "s", noon)], links, finished, {}, turns=turns)
    by_ticket = {row["ticket_id"]: row["spend_usd"] for row in split["tickets"]}
    # s: L $1 and A $2 from turns; its $2 not logged goes by the day rule (A).
    assert by_ticket == {"A": 4.0, "L": 1.0}
    # t: $1 scaled from $1.50 logged, no activity or links, so unattributed.
    assert (split["attributed_usd"], split["unattributed_usd"]) == (5.0, 1.0)


def test_a_priced_turn_logs_its_cost_in_the_usage_log(db_uri: str) -> None:
    from omnigent.server import usage_events
    from omnigent.server.routes._sessions.helpers import _record_daily_cost
    from omnigent.stores.usage_event_store import UsageEventStore

    conversations = SqlAlchemyConversationStore(db_uri)
    project_id = uuid4().hex
    root = conversations.create_conversation(project_id=project_id)
    child = conversations.create_conversation(parent_conversation_id=root.id)
    store = UsageEventStore(db_uri)
    usage_events.set_turn_cost_store(store)
    try:
        _record_daily_cost(child, 0.25, conversations)
        usage_events.record_turn_cost(conversations, child.id, root.id, None, 1).result(timeout=5)
    finally:
        usage_events.set_turn_cost_store(None)
    events = store.events(project_id, since_epoch=None, names=("cost.recorded",))
    # A sub-agent's turn is filed under its root session's project.
    assert [(event.session_id, event.props) for event in events] == [
        (child.id, {"micros": 250_000}),
        (child.id, {"micros": 1}),
    ]


def test_recording_a_turn_cost_never_blocks_or_fails_the_turn(db_uri: str) -> None:
    import threading
    import time

    from omnigent.server import usage_events
    from omnigent.server.routes._sessions.helpers import _record_daily_cost

    conversations = SqlAlchemyConversationStore(db_uri)
    project_id = uuid4().hex
    root = conversations.create_conversation(project_id=project_id)
    child = conversations.create_conversation(parent_conversation_id=root.id)
    release = threading.Event()
    written: list[dict] = []

    class SlowThenFailingStore:
        def record(self, name: str, **fields: object) -> None:
            release.wait(5)
            written.append({"name": name, **fields})
            raise RuntimeError("usage log unavailable")

    usage_events.set_turn_cost_store(SlowThenFailingStore())
    try:
        started = time.monotonic()
        _record_daily_cost(child, 0.25, conversations)
        assert time.monotonic() - started < 1.0  # The slow write didn't hold the turn.
        future = usage_events.record_turn_cost(conversations, child.id, root.id, None, 250_000)
        release.set()
        assert future.result(timeout=5) is None  # The failure was logged and swallowed.
    finally:
        usage_events.set_turn_cost_store(None)
    # A sub-agent's turn is filed under its root session's project.
    assert written[0] == {
        "name": "cost.recorded",
        "project_id": project_id,
        "session_id": child.id,
        "props": {"micros": 250_000},
        "created_at": written[0]["created_at"],
    }
    assert usage_events.record_turn_cost(conversations, child.id, root.id, None, 1) is None
