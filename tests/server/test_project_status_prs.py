"""PRs-merged status source: deduplication, attribution and the window, on real stores."""

from __future__ import annotations

import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import update

from omnigent.db.db_models import SqlProjectTicketEvent, current_workspace_id
from omnigent.db.utils import get_or_create_engine
from omnigent.entities.project import PROJECT_IDENTITY_LABEL_KEY
from omnigent.entities.project_ticket import (
    CommentOnTicket,
    CreateTicket,
    TicketActor,
    TicketStage,
    UpdateTicket,
    UpdateTicketStages,
    default_ticket_stages,
)
from omnigent.server import project_status_prs
from omnigent.server.project_status import LEAD_MEMBER_KEY, status_period, user_member_key
from omnigent.server.project_status_cache import project_status_cache
from omnigent.server.project_status_prs import (
    MergedTicket,
    SessionMergedPr,
    build_source,
    canonical_pr_url,
    combine,
    totals,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.stores.project_ticket_store import ProjectTicketStore
from omnigent.stores.session_pull_request_store import (
    FetchedState,
    PullRequestReport,
    SessionPullRequestStore,
)

DAY = 86_400
NOW = int(time.time())
PR = "https://github.com/example/repo/pull/7"


def _session_pr(session_id: str, relationship: str, merged_at: float | None, seen: float = 1.0):
    return SessionMergedPr(session_id, PR, relationship, seen, merged_at)


def test_canonical_url_drops_subpages_query_and_case() -> None:
    assert canonical_pr_url("HTTPS://GitHub.com/example/repo/pull/7/files?x=1#y") == PR
    assert canonical_pr_url(PR + "/") == PR


def test_one_pr_in_both_sources_counts_once_for_its_creator() -> None:
    members = {"creator": "user:a", "attacher": "user:b", "worker": "identity:w"}
    items = combine(
        [_session_pr("attacher", "attached", 50.0), _session_pr("creator", "created", 60.0)],
        [MergedTicket("t1", 70, (PR + "/files",), ("worker",), counts_without_pr=True)],
        members.get,
    )
    assert [(item.key, item.merged_at, item.member) for item in items] == [(PR, 50.0, "user:a")]
    # Both sessions and the ticket claim it, so any of them can count it as theirs.
    assert items[0].claimed_by == ("attacher", "creator", "t1")


def test_tickets_credit_their_first_non_lead_session_or_nobody() -> None:
    members = {"lead": LEAD_MEMBER_KEY, "worker": "identity:w"}
    items = combine(
        [],
        [
            MergedTicket("t1", 10, (), ("lead", "worker"), counts_without_pr=True),
            MergedTicket("t2", 10, (), ("lead",), counts_without_pr=True),
            MergedTicket("t3", 10, (), ("worker",), counts_without_pr=False),
        ],
        members.get,
    )
    assert [(item.key, item.member) for item in items] == [
        ("ticket:t1", "identity:w"),
        ("ticket:t2", None),
    ]


def test_window_counts_dated_items_and_undated_ones_only_for_all_time() -> None:
    items = combine(
        [_session_pr("s", "created", None)],
        [
            MergedTicket("old", NOW - 30 * DAY, (), (), True),
            MergedTicket("new", NOW, (), (), True),
        ],
        {"s": "user:a"}.get,
    )
    week = totals(items, status_period("7d", NOW))
    assert (week.total, week.by_member) == (1, {})
    every = totals(items, status_period("all", NOW))
    assert (every.total, every.by_member) == (3, {"user:a": 1})


def test_build_source_without_sql_stores_reports_unavailable() -> None:
    source = build_source(SimpleNamespace())
    assert source.prs_merged(None, status_period("7d", NOW)).available is False  # type: ignore[arg-type]


@pytest.fixture
def setup(db_uri: str):
    projects = SqlAlchemyProjectStore(db_uri)
    project = projects.create(uuid4().hex, "Status", "alice")
    tickets = ProjectTicketStore(db_uri)
    stages = [*default_ticket_stages().stages, TicketStage(id="merged", name="Merged")]
    tickets.update_stages(project.id, UpdateTicketStages(stages=stages), TicketActor("alice"))
    conversations = SqlAlchemyConversationStore(db_uri)
    state = SimpleNamespace(
        project_ticket_notifications=SimpleNamespace(
            tickets=tickets, projects=projects, inputs=SimpleNamespace(conversations=conversations)
        ),
        permission_store=None,
    )
    return projects.get(project.id, user_id="alice"), tickets, conversations, state, db_uri


def _merge_ticket(tickets, project, session_id, *, pr_url=None):
    actor = TicketActor("alice", session_id)
    ticket = tickets.create(project.id, CreateTicket(title="Work"), actor)
    tickets.link_session(project.id, ticket["id"], session_id, "attach", actor)
    if pr_url:
        outputs = [{"type": "pull_request", "url": pr_url}]
        tickets.comment(
            project.id, ticket["id"], CommentOnTicket(text="PR", outputs=outputs), actor
        )
    tickets.update(project.id, ticket["id"], UpdateTicket(stage_id="merged"), TicketActor("alice"))
    return ticket["id"]


def test_source_counts_session_prs_and_merged_tickets_per_member(setup) -> None:
    project, tickets, conversations, state, db_uri = setup
    person = uuid4().hex
    reviewer = uuid4().hex
    conversations.create_conversation(conversation_id=person, project_id=project.id)
    conversations.create_conversation(
        conversation_id=reviewer,
        project_id=project.id,
        labels={PROJECT_IDENTITY_LABEL_KEY: "reviewer-id"},
    )
    pull_requests = SessionPullRequestStore(db_uri)
    report = PullRequestReport(PR, "github", "github.com", "example/repo", 7, "created", 1.0, 1.0)
    pull_requests.apply_report(
        person, [report], fetched=FetchedState(PR, 7, "OPEN", False), requested_at=1.0
    )
    pull_requests.apply_report(
        person, [], fetched=FetchedState(PR, 7, "MERGED", False), requested_at=NOW - DAY
    )
    # The same PR on a merged ticket, plus a PR-less merged ticket the reviewer worked.
    _merge_ticket(tickets, project, person, pr_url=PR + "/files")
    reviewed = _merge_ticket(tickets, project, reviewer)
    old = _merge_ticket(tickets, project, reviewer)
    with get_or_create_engine(db_uri).begin() as connection:
        connection.execute(
            update(SqlProjectTicketEvent)
            .where(
                SqlProjectTicketEvent.workspace_id == current_workspace_id(),
                SqlProjectTicketEvent.ticket_id == old,
            )
            .values(created_at=NOW - 40 * DAY)
        )

    source = build_source(state)
    week = source.prs_merged(project, status_period("7d", NOW + 60))
    assert week.available
    assert (week.total, week.by_member) == (
        2,
        {user_member_key("alice"): 1, "identity:reviewer-id": 1},
    )
    every = source.prs_merged(project, status_period("all", NOW + 60))
    assert every.total == 3 and reviewed


def _seed(setup):
    """Session PRs and merged tickets across members, dates and an undated PR."""
    project, tickets, conversations, _state, db_uri = setup
    person, other = uuid4().hex, uuid4().hex
    for session_id in (person, other):
        conversations.create_conversation(conversation_id=session_id, project_id=project.id)
    pull_requests = SessionPullRequestStore(db_uri)
    for number, merged_at in ((7, NOW - DAY), (8, NOW - 20 * DAY), (9, None)):
        url = f"https://github.com/example/repo/pull/{number}"
        report = PullRequestReport(
            url, "github", "github.com", "example/repo", number, "created", 1.0, 1.0
        )
        pull_requests.apply_report(
            person, [report], fetched=FetchedState(url, number, "OPEN", False), requested_at=1.0
        )
        pull_requests.apply_report(
            person,
            [],
            fetched=FetchedState(url, number, "MERGED", False),
            requested_at=merged_at if merged_at is not None else NOW,
        )
    _merge_ticket(tickets, project, other, pr_url="https://github.com/example/repo/pull/9")
    _merge_ticket(tickets, project, other)
    old = _merge_ticket(tickets, project, other)
    with get_or_create_engine(db_uri).begin() as connection:
        connection.execute(
            update(SqlProjectTicketEvent)
            .where(
                SqlProjectTicketEvent.workspace_id == current_workspace_id(),
                SqlProjectTicketEvent.ticket_id == old,
            )
            .values(created_at=NOW - 40 * DAY)
        )
    return person, other


def test_stored_rows_match_a_fresh_computation_in_every_window(setup) -> None:
    project, _tickets, _conversations, state, _db_uri = setup
    _seed(setup)
    source = build_source(state)
    computed = source.compute_items(project)
    for window in ("7d", "30d", "all"):
        period = status_period(window, NOW + 60)
        assert source.prs_merged(project, period) == totals(computed, period), window
    assert sorted(source.merged_items(project), key=lambda item: item.key) == sorted(
        computed, key=lambda item: item.key
    )
    since = NOW - 7 * DAY
    assert {item.key for item in source.merged_items(project, since)} == {
        item.key for item in computed if item.merged_at is None or item.merged_at >= since
    }


def test_a_merge_after_a_read_shows_on_the_next_read(setup) -> None:
    project, tickets, _conversations, state, _db_uri = setup
    _person, other = _seed(setup)
    source = build_source(state)
    period = status_period("all", NOW + 60)
    before = source.prs_merged(project, period).total
    _merge_ticket(tickets, project, other)
    assert source.prs_merged(project, period).total == before + 1


def test_only_pr_and_ticket_invalidations_leave_rows_stale(setup) -> None:
    project, _tickets, _conversations, state, _db_uri = setup
    source = build_source(state)
    source.prs_merged(project, status_period("all", NOW))
    workspace = current_workspace_id()
    project_status_cache.invalidate(project.id, ("spend",))
    assert not project_status_prs._is_stale(workspace, project.id)
    source.refresh(project)
    assert not project_status_prs._is_stale(workspace, project.id)
    project_status_cache.invalidate(project.id, ("tickets",))
    assert project_status_prs._is_stale(workspace, project.id)
    source.prs_merged(project, status_period("all", NOW))
    assert not project_status_prs._is_stale(workspace, project.id)


def test_the_background_refresher_recomputes_a_stale_project(setup, monkeypatch) -> None:
    project, tickets, _conversations, state, db_uri = setup
    monkeypatch.setattr(project_status_prs, "_REFRESH_COALESCE_S", 0.05)
    source = build_source(state)
    period = status_period("all", NOW + 60)
    assert source.prs_merged(project, period).total == 0
    other = uuid4().hex
    _merge_ticket(tickets, project, other)
    # Poll the table itself: the stale flag clears when a refresh starts, before it commits.
    deadline = time.monotonic() + 10
    while True:
        with get_or_create_engine(db_uri).connect() as connection:
            rows = connection.execute(
                sa.text("SELECT COUNT(*) FROM project_merged_prs WHERE project_id = :id"),
                {"id": bytes.fromhex(project.id.replace("-", ""))},
            ).scalar_one()
        if rows == 1:
            break
        assert time.monotonic() < deadline, "the refresher never ran"
        time.sleep(0.05)


def test_overlong_pr_keys_are_stored_hashed() -> None:
    long_key = "https://git.example.com/" + "a" * 600
    stored = project_status_prs._row_key(long_key)
    assert stored.startswith("sha256:") and len(stored) <= 512
    assert project_status_prs._row_key(PR) == PR
