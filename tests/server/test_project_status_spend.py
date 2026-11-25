"""Project spend in cents, attributed per member through each root session."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import httpx

from omnigent.entities.project import PROJECT_IDENTITY_LABEL_KEY
from omnigent.server import session_live_state
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.project_status import (
    LEAD_MEMBER_KEY,
    UNAVAILABLE,
    WORKERS_MEMBER_KEY,
    status_period,
)
from omnigent.server.project_status_spend import (
    ProjectSpend,
    build_source,
    legacy_window_rows,
    usd_to_cents,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from tests.server.routes.test_projects_crud import (  # noqa: F401 - pytest fixtures
    project_agents,
    project_app,
    project_client,
)

# 2026-06-10T12:00:00Z
NOW = 1781092800


def test_spend_sums_cents_per_member_and_day_through_root_sessions(db_uri: str) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    project = SqlAlchemyProjectStore(db_uri).create(uuid.uuid4().hex, "zz-spend", "owner")
    other_project = SqlAlchemyProjectStore(db_uri).create(uuid.uuid4().hex, "zz-other", "owner")

    lead = conversations.create_conversation(project_id=project.id)
    project = dataclasses.replace(project, main_session_id=lead.id)
    persona = conversations.create_conversation(
        project_id=project.id, labels={PROJECT_IDENTITY_LABEL_KEY: "reviewer-id"}
    )
    human = conversations.create_conversation(project_id=project.id)
    permissions.grant("alice", human.id, LEVEL_OWNER)
    # A sub-agent with no project of its own still counts for its root's member.
    sub_agent = conversations.create_conversation(parent_conversation_id=human.id)
    elsewhere = conversations.create_conversation(project_id=other_project.id)

    conversations.add_session_daily_cost(lead.id, "2026-06-10", 0.105)
    conversations.add_session_daily_cost(persona.id, "2026-06-09", 0.333)
    conversations.add_session_daily_cost(persona.id, "2026-06-10", 0.333)
    conversations.add_session_daily_cost(human.id, "2026-06-04", 1.00)
    conversations.add_session_daily_cost(sub_agent.id, "2026-06-10", 2.004)
    # Outside the 7-day window (it starts 2026-06-04) and in another project.
    conversations.add_session_daily_cost(human.id, "2026-06-03", 50.0)
    conversations.add_session_daily_cost(elsewhere.id, "2026-06-10", 9.0)

    source = ProjectSpend(conversations)
    period = status_period("7d", NOW)
    breakdown = source.breakdown(project, period)
    assert breakdown is not None
    # 0.105 + 0.666 + 1.00 + 2.004 = 3.775, rounded once.
    assert breakdown.total == 378
    assert breakdown.by_member == {
        LEAD_MEMBER_KEY: 11,
        "identity:reviewer-id": 67,
        "user:alice": 300,
    }
    assert breakdown.by_day == {"2026-06-04": 100, "2026-06-09": 33, "2026-06-10": 244}
    assert breakdown.by_member_day["user:alice"] == {"2026-06-04": 100, "2026-06-10": 200}

    totals = source.spend(project, period)
    assert (totals.total, totals.by_member, totals.available) == (
        378,
        breakdown.by_member,
        True,
    )
    # "all" has no lower bound, so the older day counts too.
    assert source.spend(project, status_period("all", NOW)).total == 5378


def test_family_sessions_count_for_the_workers_pool(db_uri: str) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    project = SqlAlchemyProjectStore(db_uri).create(uuid.uuid4().hex, "zz-spend", "owner")
    worker = conversations.create_conversation(project_id=project.id)
    worker = dataclasses.replace(worker, family_id=uuid.uuid4().hex)
    conversations.add_session_daily_cost(worker.id, "2026-06-10", 1.0)

    class FamilyStore:
        """Reports the worker as part of a session family."""

        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            return getattr(conversations, name)

        def get_conversation(self, conversation_id: str):  # type: ignore[no-untyped-def]
            return worker if conversation_id == worker.id else None

        def get_conversations(self, conversation_ids: list[str]):  # type: ignore[no-untyped-def]
            return {worker.id: worker} if worker.id in conversation_ids else {}

    totals = ProjectSpend(FamilyStore()).spend(project, status_period("7d", NOW))  # type: ignore[arg-type]
    assert totals.by_member == {WORKERS_MEMBER_KEY: 100}


def test_spend_is_unavailable_without_a_store(db_uri: str, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class NoStores:
        """App state without a conversation store."""

    monkeypatch.setattr(session_live_state, "conversation_store", lambda: None)
    project = SqlAlchemyProjectStore(db_uri).create(uuid.uuid4().hex, "zz-spend", "owner")
    source = build_source(NoStores())
    assert source.spend(project, status_period("7d", NOW)) is UNAVAILABLE


def test_usd_to_cents_rounds_to_the_nearest_cent() -> None:
    # Halves round up, including amounts floats can't represent exactly.
    assert [usd_to_cents(value) for value in (0.0, 0.004, 0.005, 0.006, 1.0, 53.775)] == [
        0,
        0,
        1,
        1,
        100,
        5378,
    ]


async def test_header_cost_matches_the_summary_including_sub_agents(
    project_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
    db_uri: str,
) -> None:
    created = (await project_client.post("/v1/projects", json={"name": "zz-header"})).json()
    conversations = SqlAlchemyConversationStore(db_uri)
    root = conversations.create_conversation(project_id=created["id"])
    sub_agent = conversations.create_conversation(parent_conversation_id=root.id)
    today = datetime.now(UTC).date()
    conversations.add_session_daily_cost(root.id, today.isoformat(), 1.771)
    conversations.add_session_daily_cost(sub_agent.id, today.isoformat(), 2.004)
    old_day = (today - timedelta(days=7)).isoformat()
    conversations.add_session_daily_cost(root.id, old_day, 50.0)

    project = f"/v1/projects/{created['id']}"
    header = (await project_client.get(f"{project}/tickets/cost", params={"days": 7})).json()
    summary = (
        await project_client.get(f"{project}/status/summary", params={"window": "7d"})
    ).json()
    assert header["cost_usd"] == 3.78
    assert round(header["cost_usd"] * 100) == summary["totals"]["spend_cents"] == 378


def _by_root_day(rows) -> dict[tuple[str, str], int]:  # type: ignore[no-untyped-def]
    totals: dict[tuple[str, str], int] = {}
    for root_id, day, micros in rows:
        totals[(root_id, day)] = totals.get((root_id, day), 0) + micros
    return totals


def _assert_rollup_matches(conversations: SqlAlchemyConversationStore, project_id: str) -> None:
    """The rollup holds exactly the micro-dollars the per-session rows sum to."""
    rollup = _by_root_day(conversations.list_project_daily_costs(project_id, None, "9999-12-31"))
    legacy = _by_root_day(legacy_window_rows(conversations, project_id, None, "9999-12-31"))
    assert rollup == legacy


async def test_rollup_matches_session_rows_through_moves_and_deletes(db_uri: str) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    projects = SqlAlchemyProjectStore(db_uri)
    first = projects.create(uuid.uuid4().hex, "zz-first", "owner")
    second = projects.create(uuid.uuid4().hex, "zz-second", "owner")
    root = conversations.create_conversation(project_id=first.id)
    sub_agent = conversations.create_conversation(parent_conversation_id=root.id)
    # A sub-agent filed elsewhere counts for both its own project and its root's.
    filed_sub_agent = conversations.create_conversation(
        parent_conversation_id=root.id, project_id=second.id
    )
    other = conversations.create_conversation(project_id=first.id)
    # Many small deltas on one session-day: the rollup tracks the rounded total.
    for _ in range(7):
        conversations.add_session_daily_cost(root.id, "2026-06-10", 0.0000004)
    conversations.add_session_daily_cost(sub_agent.id, "2026-06-10", 1.2345675)
    conversations.add_session_daily_cost(filed_sub_agent.id, "2026-06-09", 0.5)
    conversations.add_session_daily_cost(other.id, "2026-06-08", 3.0)
    for project in (first, second):
        _assert_rollup_matches(conversations, project.id)
    assert conversations.list_project_daily_costs(second.id, None, "9999-12-31") == [
        (root.id, "2026-06-09", 500_000)
    ]

    # Moving the root's family to the second project moves its spend with it.
    assert conversations.set_conversation_project(root.id, second.id)
    for project in (first, second):
        _assert_rollup_matches(conversations, project.id)
    assert {
        root_id
        for root_id, _, _ in conversations.list_project_daily_costs(first.id, None, "9999-12-31")
    } == {other.id}

    # Deleting a session drops its spend, as the per-session read did.
    assert await conversations.delete_conversation(other.id)
    _assert_rollup_matches(conversations, first.id)
    assert conversations.list_project_daily_costs(first.id, None, "9999-12-31") == []

    # A rebuild reproduces what the incremental writes kept.
    kept = conversations.list_project_daily_costs(second.id, None, "9999-12-31")
    conversations.rebuild_project_daily_cost(second.id)
    assert sorted(conversations.list_project_daily_costs(second.id, None, "9999-12-31")) == sorted(
        kept
    )
