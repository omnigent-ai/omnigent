import random
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.exc import StatementError

from omnigent.db.db_models import SqlProjectTicket, SqlProjectTicketEvent, workspace_scope
from omnigent.db.utils import get_or_create_engine
from omnigent.entities.project_ticket import (
    CommentOnTicket,
    CreateTicket,
    TicketActor,
    UpdateTicket,
    UpdateTicketStages,
)
from omnigent.errors import OmnigentError
from omnigent.stores import project_ticket_store
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.stores.project_ticket_store import ProjectTicketStore


@pytest.fixture
def board(db_uri):
    projects = SqlAlchemyProjectStore(db_uri)
    project = projects.create(uuid4().hex, "Board", "alice")
    return ProjectTicketStore(db_uri), projects, project


def test_atomic_edits_and_append_only_outputs(board):
    tickets, _, project = board
    actor = TicketActor("alice", uuid4().hex, uuid4().hex)
    ticket = tickets.create(project.id, CreateTicket(title=" Deliver "), actor)
    assert ticket["title"] == "Deliver" and ticket["stage_id"] == "open"
    assert ticket["created_at"] == ticket["updated_at"]
    tickets.update(project.id, ticket["id"], UpdateTicket(title="First"), actor)
    tickets.update(
        project.id,
        ticket["id"],
        UpdateTicket(title="Last", labels=[{"key": "needs-human", "value": "decision"}]),
        actor,
    )
    tickets.comment(
        project.id,
        ticket["id"],
        CommentOnTicket(
            text="Ready for review",
            kind="execution",
            outputs=[
                {"type": "pull_request", "url": "https://git.example.com/org/repo/pull/42/files"},
                {"type": "pull_request", "url": "https://git.example.com/org/repo/pull/42"},
            ],
        ),
        actor,
    )
    result = tickets.get(project.id, ticket["id"], user_id="alice")
    assert result["title"] == "Last" and result["comment_count"] == 1
    assert result["labels"] == [{"key": "needs-human", "value": "decision"}]
    events = tickets.events(project.id, ticket["id"], user_id="alice")["data"]
    assert [e["kind"] for e in events] == [
        "created",
        "updated",
        "updated",
        "execution",
        "output",
        "output",
    ]
    assert [e["position"] for e in events] == list(range(6))
    assert all(
        e["session_id"] == actor.session_id and e["agent_id"] == actor.agent_id for e in events
    )
    assert events[-1]["payload"] == events[-2]["payload"]
    assert events[0]["payload"]["labels"] == []
    assert events[2]["payload"]["changes"]["labels"] == result["labels"]
    assert "tags" not in events[0]["payload"]
    assert "tags" not in events[2]["payload"]["changes"]
    assert tickets.update(project.id, ticket["id"], UpdateTicket(labels=None), actor) == result
    assert tickets.events(project.id, ticket["id"], user_id="alice")["data"] == events
    done = tickets.update(
        project.id,
        ticket["id"],
        UpdateTicket(stage_id="done", comment="Delivered and tested"),
        actor,
    )
    assert not {"plan", "review_summary", "completion_summary"} & done.keys()
    reopened = tickets.update(
        project.id, ticket["id"], UpdateTicket(stage_id="open", labels=[]), actor
    )
    assert reopened["labels"] == []
    history = tickets.events(project.id, ticket["id"], user_id="alice")["data"]
    assert len(history) == 9
    assert history[-2]["payload"] == {"text": "Delivered and tested"}


def test_labels_project_existing_storage_and_history_without_rewriting_it(board, db_uri):
    tickets, _, project = board
    actor = TicketActor("alice", uuid4().hex, uuid4().hex)
    ticket = tickets.create(
        project.id,
        CreateTicket(title="Keep tags in authored text", labels=[{"key": "area"}]),
        actor,
    )
    tickets.link_session(project.id, ticket["id"], actor.session_id, "attach", actor)
    labels = [{"key": "needs-human", "value": "Keep café 🦊 tags"}, {"key": "area", "value": ""}]
    expected = tickets.update(
        project.id,
        ticket["id"],
        UpdateTicket(
            labels=labels,
            stage_id="planning",
            description="Keep `tags` in this description.",
            comment="**Tags**\n\nKeep this authored text.",
        ),
        actor,
    )
    expected_history = tickets.events(project.id, ticket["id"], user_id="alice")["data"]
    table = SqlProjectTicket.__table__
    assert "tags" in table.columns and "labels" not in table.columns
    assert SqlProjectTicket.labels.property.columns[0] is table.c.tags
    engine = get_or_create_engine(db_uri)
    with engine.begin() as connection:
        connection.execute(table.update().where(table.c.id == ticket["id"]).values(tags=labels))
        for entry in expected_history:
            payload = deepcopy(entry["payload"])
            if entry["kind"] == "created":
                payload["tags"] = payload.pop("labels")
            elif entry["kind"] == "updated" and "labels" in payload.get("changes", {}):
                payload["changes"]["tags"] = payload["changes"].pop("labels")
            connection.execute(
                update(SqlProjectTicketEvent)
                .where(SqlProjectTicketEvent.id == entry["id"])
                .values(payload=payload)
            )
    with engine.connect() as connection:
        raw_ticket = connection.exec_driver_sql("SELECT * FROM project_tickets").all()
        raw_events = connection.exec_driver_sql(
            "SELECT * FROM project_ticket_events ORDER BY position"
        ).all()
    for _ in range(2):
        assert tickets.get(project.id, ticket["id"], user_id="alice") == expected
        assert tickets.list(project.id, user_id="alice")["data"] == [expected]
        assert (
            tickets.events(project.id, ticket["id"], user_id="alice")["data"] == expected_history
        )
    with tickets._session("check_history_projection") as session:
        rows = session.scalars(
            select(SqlProjectTicketEvent)
            .where(SqlProjectTicketEvent.ticket_id == ticket["id"])
            .order_by(SqlProjectTicketEvent.position)
            .limit(10)
        ).all()
        for row, expected_event in zip(rows, expected_history, strict=True):
            saved_payload = deepcopy(row.payload)
            assert project_ticket_store._event(row) == expected_event
            assert row.payload == saved_payload
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT * FROM project_tickets").all() == raw_ticket
        assert (
            connection.exec_driver_sql(
                "SELECT * FROM project_ticket_events ORDER BY position"
            ).all()
            == raw_events
        )


def test_failed_event_write_rolls_back_ticket_and_link_changes(board, monkeypatch):
    tickets, _, project = board
    actor = TicketActor("alice")
    ticket = tickets.create(project.id, CreateTicket(title="Original"), actor)
    worker = uuid4().hex
    tickets.link_session(project.id, ticket["id"], worker, "attach", actor, involvement="Planning")

    def reject(*args, **kwargs):
        raise RuntimeError("event storage unavailable")

    monkeypatch.setattr(tickets, "_append", reject)
    with pytest.raises(RuntimeError, match="event storage"):
        tickets.update(project.id, ticket["id"], UpdateTicket(title="Lost edit"), actor)
    with pytest.raises(RuntimeError, match="event storage"):
        tickets.link_session(
            project.id, ticket["id"], worker, "attach", actor, involvement="Building"
        )
    assert tickets.get(project.id, ticket["id"], user_id="alice")["title"] == "Original"
    assert (
        tickets.sessions(project.id, ticket["id"], user_id="alice")["data"][0]["involvement"]
        == "Planning"
    )
    assert len(tickets.events(project.id, ticket["id"], user_id="alice")["data"]) == 2


def test_storage_envelope_bound_rolls_back_the_event(board):
    tickets, _, project = board
    actor = TicketActor("alice")
    ticket = tickets.create(project.id, CreateTicket(title="Original"), actor)
    # Multibyte values are poorly compressible, unlike a repeated test string.
    rng = random.Random(0)
    labels = [
        {
            "key": str(i),
            "value": "".join(chr(0x10000 + rng.randrange(0x10000)) for _ in range(256)),
        }
        for i in range(20)
    ]
    with pytest.raises(StatementError):
        tickets.update(project.id, ticket["id"], UpdateTicket(labels=labels), actor)
    assert tickets.get(project.id, ticket["id"], user_id="alice")["labels"] == []
    assert len(tickets.events(project.id, ticket["id"], user_id="alice")["data"]) == 1


def test_project_owner_tenant_and_ticket_boundaries(board):
    tickets, projects, project = board
    ticket = tickets.create(project.id, CreateTicket(title="Private"), TicketActor("alice"))
    other = projects.create(uuid4().hex, "Other", "alice")
    with pytest.raises(OmnigentError, match="Project not found"):
        tickets.get(project.id, ticket["id"], user_id="bob")
    with pytest.raises(OmnigentError, match="Ticket not found"):
        tickets.get(other.id, ticket["id"], user_id="alice")
    with workspace_scope(9), pytest.raises(OmnigentError, match="Project not found"):
        tickets.get(project.id, ticket["id"], user_id="alice")
    with pytest.raises(OmnigentError, match="not found"):
        tickets.get("invalid", ticket["id"], user_id="alice")
    with pytest.raises(OmnigentError, match="cannot be deleted"):
        projects.delete(project.id, user_id="alice")
    assert projects.get(project.id, user_id="alice") is not None


def test_response_summary_counts_labels_across_stages_and_projects(board):
    tickets, projects, project = board
    actor = TicketActor("alice")
    assert tickets.summary(project.id, user_id="alice") == {
        "needs_response_count": 0,
        "is_complete": True,
    }
    flagged = []
    for stage, value in (("open", "approval"), ("in_progress", ""), ("done", None)):
        ticket = tickets.create(
            project.id,
            CreateTicket(title=stage, labels=[{"key": "needs-human", "value": value}]),
            actor,
        )
        tickets.update(project.id, ticket["id"], UpdateTicket(stage_id=stage), actor)
        flagged.append(ticket["id"])
    tickets.create(
        project.id,
        CreateTicket(title="Unflagged", labels=[{"key": "note", "value": "needs-human"}]),
        actor,
    )
    other = projects.create(uuid4().hex, "Other", "alice")
    tickets.create(other.id, CreateTicket(title="Other", labels=[{"key": "needs-human"}]), actor)
    with workspace_scope(9):
        projects.create(project.id, "Other workspace", "alice")
        tickets.create(
            project.id, CreateTicket(title="Other", labels=[{"key": "needs-human"}]), actor
        )
        assert tickets.summary(project.id, user_id="alice")["needs_response_count"] == 1
    assert tickets.summary(project.id, user_id="alice") == {
        "needs_response_count": 3,
        "is_complete": True,
    }
    tickets.update(project.id, flagged[0], UpdateTicket(labels=[]), actor)
    assert tickets.summary(project.id, user_id="alice")["needs_response_count"] == 2
    with pytest.raises(OmnigentError, match="Project not found"):
        tickets.summary(project.id, user_id="bob")
    with workspace_scope(10), pytest.raises(OmnigentError, match="Project not found"):
        tickets.summary(project.id, user_id="alice")


def test_response_summary_reports_a_bounded_indexed_partial_count(board, db_uri, monkeypatch):
    tickets, _, project = board
    for index in range(3):
        tickets.create(
            project.id,
            CreateTicket(title=str(index), labels=[{"key": "needs-human"}]),
            TicketActor("alice"),
        )
    monkeypatch.setattr(project_ticket_store, "TICKET_SUMMARY_SCAN_MAX", 2)
    engine = get_or_create_engine(db_uri)
    queries = []

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        if "project_tickets" in statement:
            queries.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert tickets.summary(project.id, user_id="alice") == {
            "needs_response_count": 2,
            "is_complete": False,
        }
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(queries) == 1
    statement, parameters = queries[0]
    assert "LIMIT" in statement
    assert parameters[-2] == 3
    with engine.connect() as connection:
        plan = connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters).all()
    assert any("SEARCH" in row[3] and "ix_project_tickets_created" in row[3] for row in plan)
    assert not any("TEMP B-TREE" in row[3] for row in plan)


def test_activity_sessions_are_scoped_bounded_indexed_reads(board, db_uri, monkeypatch):
    tickets, projects, project = board
    actor = TicketActor("alice")
    first, second = [
        tickets.create(project.id, CreateTicket(title=name), actor) for name in ("First", "Second")
    ]
    sessions = sorted(uuid4().hex for _ in range(4))
    for session_id in sessions:
        tickets.link_session(project.id, first["id"], session_id, "attach", actor)
    tickets.link_session(project.id, second["id"], sessions[-1], "attach", actor)
    other = projects.create(uuid4().hex, "Other", "alice")
    foreign = tickets.create(other.id, CreateTicket(title="Elsewhere"), actor)
    tickets.link_session(other.id, foreign["id"], sessions[0], "attach", actor)
    monkeypatch.setattr(project_ticket_store, "TICKET_ACTIVITY_SESSION_MAX", 2)
    engine = get_or_create_engine(db_uri)
    queries = []

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        if "project_ticket_sessions" in statement:
            queries.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = tickets.activity_sessions(
            project.id, [first["id"], second["id"], foreign["id"]], user_id="alice"
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert result[first["id"]].session_ids == sessions[:2]
    assert result[first["id"]].has_more
    assert result[second["id"]].session_ids == [sessions[-1]]
    assert not result[second["id"]].has_more
    assert result[foreign["id"]].session_ids == []
    assert len(queries) == 1
    with engine.connect() as connection:
        plan = connection.exec_driver_sql(
            "EXPLAIN QUERY PLAN " + queries[0][0], queries[0][1]
        ).all()
    scans = [row[3] for row in plan if "project_ticket_sessions" in row[3]]
    assert len(scans) == 3 and all("SEARCH" in scan and "INDEX" in scan for scan in scans)
    with pytest.raises(OmnigentError, match="Project not found"):
        tickets.activity_sessions(project.id, [first["id"]], user_id="bob")
    with workspace_scope(9), pytest.raises(OmnigentError, match="Project not found"):
        tickets.activity_sessions(project.id, [first["id"]], user_id="alice")


def test_pagination_and_outbox_order(board):
    tickets, projects, project = board
    actor = TicketActor("alice")
    created = [tickets.create(project.id, CreateTicket(title=str(i)), actor) for i in range(4)]
    page = tickets.list(project.id, user_id="alice", stage_id="open", limit=2)
    rest = tickets.list(
        project.id, user_id="alice", stage_id="open", after=page["last_id"], limit=2
    )
    assert page["has_more"] and not rest["has_more"]
    assert {row["id"] for row in page["data"] + rest["data"]} == {row["id"] for row in created}
    ticket_id = created[0]["id"]
    for text in ("One", "Two", "Three"):
        tickets.comment(project.id, ticket_id, CommentOnTicket(text=text), actor)
    first = tickets.events(project.id, ticket_id, user_id="alice", limit=2)
    last = tickets.events(
        project.id, ticket_id, user_id="alice", after=first["last_position"], limit=2
    )
    assert [e["position"] for e in first["data"] + last["data"]] == [0, 1, 2, 3]
    comments = tickets.events(project.id, ticket_id, user_id="alice", kind="comment", limit=2)
    assert comments["has_more"] and [e["payload"]["text"] for e in comments["data"]] == [
        "One",
        "Two",
    ]
    orchestrator = uuid4().hex
    projects.set_main_session(project.id, orchestrator, user_id="alice")
    tickets.comment(
        project.id,
        ticket_id,
        CommentOnTicket(text="My own update"),
        TicketActor("alice", orchestrator),
    )
    pending = tickets.pending_notifications()
    assert len(pending) == 7
    assert [e["position"] for e in pending if e["ticket_id"] == ticket_id] == [0, 1, 2, 3]
    head = pending[0]
    cursor = (
        head["workspace_id"],
        head["project_id"],
        head["ticket_id"],
        head["position"],
        head["id"],
    )
    assert tickets.pending_notifications(after=cursor) == pending[1:]
    tickets.mark_notified(project.id, head["ticket_id"], head["id"])
    assert len(tickets.pending_notifications()) == 6


async def test_linked_session_deletion_preserves_ticket_and_history(board, db_uri):
    tickets, _, project = board
    conversations = SqlAlchemyConversationStore(db_uri)
    session = conversations.create_conversation(project_id=project.id)
    actor = TicketActor("alice", session.id)
    ticket = tickets.create(project.id, CreateTicket(title="Persistent"), actor)
    tickets.link_session(
        project.id, ticket["id"], session.id, "attach", actor, involvement="Plan the work"
    )
    tickets.link_session(project.id, ticket["id"], session.id, "attach", actor)
    tickets.link_session(
        project.id, ticket["id"], session.id, "attach", actor, involvement="Plan the work"
    )
    assert len(tickets.events(project.id, ticket["id"], user_id="alice")["data"]) == 2
    assert (
        tickets.sessions(project.id, ticket["id"], user_id="alice")["data"][0]["involvement"]
        == "Plan the work"
    )
    tickets.link_session(
        project.id, ticket["id"], session.id, "attach", actor, involvement="Implementing the plan"
    )
    assert (
        tickets.sessions(project.id, ticket["id"], user_id="alice")["data"][0]["involvement"]
        == "Implementing the plan"
    )
    tickets.link_session(project.id, ticket["id"], session.id, "attach", actor, involvement="")
    assert (
        tickets.sessions(project.id, ticket["id"], user_id="alice")["data"][0]["involvement"] == ""
    )
    history = tickets.events(project.id, ticket["id"], user_id="alice")["data"]
    assert [event["payload"].get("action") for event in history] == [
        None,
        "attach",
        "updated",
        "updated",
    ]
    assert [event["payload"]["involvement"] for event in history[1:]] == [
        "Plan the work",
        "Implementing the plan",
        "",
    ]
    assert all(
        event["session_id"] == session.id and event["user_id"] == "alice" for event in history
    )
    await conversations.delete_conversation(session.id)
    assert tickets.get(project.id, ticket["id"], user_id="alice")["stage_id"] == "open"
    assert (
        tickets.events(project.id, ticket["id"], user_id="alice")["data"][0]["session_id"]
        == session.id
    )
    assert (
        tickets.sessions(project.id, ticket["id"], user_id="alice")["data"][0]["session_id"]
        == session.id
    )
    tickets.link_session(project.id, ticket["id"], session.id, "remove", actor)
    assert tickets.sessions(project.id, ticket["id"], user_id="alice")["data"] == []
    assert tickets.events(project.id, ticket["id"], user_id="alice")["data"][:-1] == history


def test_description_stages_and_feedback_change_atomically(board, monkeypatch):
    tickets, _, project = board
    actor = TicketActor("alice")
    ticket = tickets.create(project.id, CreateTicket(title="Build"), actor)
    ticket_id = ticket["id"]
    planning = tickets.update(
        project.id,
        ticket_id,
        UpdateTicket(
            stage_id="planning",
            description="## Approach\n\n- [ ] Verified outcome",
            comment="Agreed in chat",
        ),
        actor,
    )
    assert planning["description"].startswith("## Approach")
    assert planning["comment_count"] == 1
    review = tickets.update(
        project.id,
        ticket_id,
        UpdateTicket(
            stage_id="review",
            comment="Tests passed; please check the layout",
            labels=[{"key": "needs-human"}],
        ),
        actor,
    )
    assert review["comment_count"] == 2 and review["stage_id"] == "review"
    append = tickets._append

    def fail_comment(session, project_row, ticket_row, who, kind, payload):
        if kind == "comment":
            raise RuntimeError("feedback unavailable")
        append(session, project_row, ticket_row, who, kind, payload)

    with monkeypatch.context() as patch:
        patch.setattr(tickets, "_append", fail_comment)
        with pytest.raises(RuntimeError, match="feedback unavailable"):
            tickets.update(
                project.id,
                ticket_id,
                UpdateTicket(
                    stage_id="in_progress",
                    comment="Adjust spacing",
                    labels=[],
                ),
                actor,
            )
    assert tickets.get(project.id, ticket_id, user_id="alice") == review
    working = tickets.update(
        project.id,
        ticket_id,
        UpdateTicket(
            stage_id="in_progress",
            comment="Adjust spacing",
            labels=[],
        ),
        actor,
    )
    assert working["description"] == planning["description"]
    assert working["comment_count"] == 3 and working["labels"] == []
    events = tickets.events(project.id, ticket_id, user_id="alice")["data"]
    assert [event["payload"]["text"] for event in events if event["kind"] == "comment"] == [
        "Agreed in chat",
        "Tests passed; please check the layout",
        "Adjust spacing",
    ]
    assert [event["kind"] for event in events[-2:]] == ["updated", "comment"]
    assert events[-1]["payload"] == {"text": "Adjust spacing"}


def test_related_links_are_symmetric_idempotent_and_paginated(board):
    tickets, _, project = board
    actor = TicketActor("alice")
    center, *others = [
        tickets.create(project.id, CreateTicket(title=str(i)), actor) for i in range(4)
    ]
    for other in others:
        tickets.link_ticket(project.id, center["id"], other["id"], "attach", actor)
        tickets.link_ticket(project.id, other["id"], center["id"], "attach", actor)
        assert (
            tickets.related(project.id, other["id"], user_id="alice")["data"][0]["id"]
            == center["id"]
        )
        assert len(tickets.events(project.id, other["id"], user_id="alice")["data"]) == 2
    page = tickets.related(project.id, center["id"], user_id="alice", limit=2)
    rest = tickets.related(
        project.id, center["id"], user_id="alice", after=page["last_id"], limit=2
    )
    assert page["has_more"] and not rest["has_more"]
    assert {row["id"] for row in page["data"] + rest["data"]} == {row["id"] for row in others}
    tickets.link_ticket(project.id, others[0]["id"], center["id"], "remove", actor)
    tickets.link_ticket(project.id, center["id"], others[0]["id"], "remove", actor)
    assert tickets.related(project.id, others[0]["id"], user_id="alice")["data"] == []
    assert len(tickets.events(project.id, others[0]["id"], user_id="alice")["data"]) == 3
    assert tickets.get(project.id, center["id"], user_id="alice")["stage_id"] == "open"
    assert len(tickets.pending_notifications()) == 12


def test_related_link_boundaries_and_failed_activity_roll_back(board, monkeypatch):
    tickets, projects, project = board
    actor = TicketActor("alice")
    first, second = [
        tickets.create(project.id, CreateTicket(title=str(i)), actor) for i in range(2)
    ]
    foreign_project = projects.create(uuid4().hex, "Other", "alice")
    foreign = tickets.create(foreign_project.id, CreateTicket(title="Outside"), actor)
    for target, who in ((first, actor), (foreign, actor), (second, TicketActor("bob"))):
        with pytest.raises(OmnigentError):
            tickets.link_ticket(project.id, first["id"], target["id"], "attach", who)
    with workspace_scope(9), pytest.raises(OmnigentError):
        tickets.link_ticket(project.id, first["id"], second["id"], "attach", actor)
    append = tickets._append

    def fail_second(session, project_row, ticket_row, who, kind, payload):
        if ticket_row.id == max(first["id"], second["id"]):
            raise RuntimeError("second event failed")
        append(session, project_row, ticket_row, who, kind, payload)

    monkeypatch.setattr(tickets, "_append", fail_second)
    with pytest.raises(RuntimeError, match="second event"):
        tickets.link_ticket(project.id, first["id"], second["id"], "attach", actor)
    for ticket in (first, second):
        assert tickets.related(project.id, ticket["id"], user_id="alice")["data"] == []
        assert len(tickets.events(project.id, ticket["id"], user_id="alice")["data"]) == 1


def test_session_links_remain_independent_of_ticket_edits(board, db_uri):
    tickets, _, project = board
    actor = TicketActor("alice")
    ticket = tickets.create(project.id, CreateTicket(title="Plan"), actor)
    conversations = SqlAlchemyConversationStore(db_uri)
    first, second = [conversations.create_conversation(project_id=project.id) for _ in range(2)]
    for worker in (first, second):
        tickets.link_session(
            project.id, ticket["id"], worker.id, "attach", actor, involvement="Verify the work"
        )
    kept = tickets.update(project.id, ticket["id"], UpdateTicket(title="Work"), actor)
    assert "lead_session_id" not in kept
    assert len(tickets.sessions(project.id, ticket["id"], user_id="alice")["data"]) == 2
    result = tickets.link_session(project.id, ticket["id"], second.id, "remove", actor)
    assert "lead_session_id" not in result
    assert result["title"] == "Work"
    remaining = tickets.sessions(project.id, ticket["id"], user_id="alice")["data"]
    assert len(remaining) == 1
    assert remaining[0]["session_id"] == first.id
    assert remaining[0]["involvement"] == "Verify the work"
    assert tickets.events(project.id, ticket["id"], user_id="alice")["data"][-1]["payload"] == {
        "session_id": second.id,
        "action": "remove",
    }


def test_stage_edit_reorder_start_and_removal_preserve_ticket_content_and_history(board):
    tickets, projects, project = board
    actor = TicketActor("alice")
    ticket = tickets.create(project.id, CreateTicket(title="Original"), actor)
    tickets.update(
        project.id,
        ticket["id"],
        UpdateTicket(description="Approach", comment="Evidence", labels=[{"key": "needs-human"}]),
        actor,
    )
    tickets.comment(project.id, ticket["id"], CommentOnTicket(text="Keep me"), actor)
    worker = uuid4().hex
    tickets.link_session(project.id, ticket["id"], worker, "attach", actor)
    sibling = tickets.create(project.id, CreateTicket(title="Related"), actor)
    tickets.link_ticket(project.id, ticket["id"], sibling["id"], "attach", actor)
    original = tickets.get(project.id, ticket["id"], user_id="alice")
    history = tickets.events(project.id, ticket["id"], user_id="alice")["data"]
    configuration = {
        "stages": [
            {
                "id": "investigating",
                "name": "Investigating",
                "instructions": "Gather evidence",
                "color": "purple",
                "icon": "search",
            },
            {"id": "open", "name": "New", "color": "orange", "icon": "flag"},
            {"id": "resolved", "name": "Resolved", "color": "blue", "icon": "rocket"},
        ],
    }
    saved = tickets.update_stages(project.id, UpdateTicketStages(**configuration), actor)
    assert [stage["id"] for stage in saved["stages"]] == ["investigating", "open", "resolved"]
    assert saved["initial_stage_id"] == "investigating"
    assert [stage["id"] for stage in saved["stages"] if stage["is_complete"]] == ["resolved"]
    assert tickets.get(project.id, ticket["id"], user_id="alice") == original
    created = tickets.create(project.id, CreateTicket(title="New work"), actor)
    assert created["stage_id"] == "investigating"
    assert tickets.events(project.id, created["id"], user_id="alice")["data"][0]["payload"][
        "stage"
    ] == {
        "id": "investigating",
        "name": "Investigating",
        "is_complete": False,
        "color": "purple",
        "icon": "search",
    }
    assert tickets.stages(project.id, user_id="alice", include_usage=True)[
        "occupied_stage_ids"
    ] == ["investigating", "open"]
    # Saving unrelated project session defaults cannot overwrite the board configuration.
    projects.update(project.id, user_id="alice", config={"workspace": "/work/project"})
    assert tickets.stages(project.id, user_id="alice") == saved
    assert saved["stages"][0]["color"] == "purple" and saved["stages"][0]["icon"] == "search"
    configuration["stages"].pop(1)
    with pytest.raises(OmnigentError, match="contains tickets"):
        tickets.update_stages(project.id, UpdateTicketStages(**configuration), actor)
    assert tickets.stages(project.id, user_id="alice") == saved
    tickets.update_stages(
        project.id, UpdateTicketStages(**configuration, moves={"open": "resolved"}), actor
    )
    moved = tickets.get(project.id, ticket["id"], user_id="alice")
    assert moved["stage_id"] == "resolved"
    assert "completion_summary" not in moved
    assert tickets.get(project.id, sibling["id"], user_id="alice")["stage_id"] == "resolved"
    for field in ("id", "title", "description", "labels", "comment_count"):
        assert moved[field] == original[field]
    assert (
        tickets.sessions(project.id, ticket["id"], user_id="alice")["data"][0]["session_id"]
        == worker
    )
    assert (
        tickets.related(project.id, ticket["id"], user_id="alice")["data"][0]["id"]
        == sibling["id"]
    )
    events = tickets.events(project.id, ticket["id"], user_id="alice")["data"]
    assert events[:-1] == history
    assert events[-1]["payload"]["previous_stage"]["name"] == "New"
    assert events[-1]["payload"]["previous_stage"]["color"] == "orange"
    assert events[-1]["payload"]["previous_stage"]["icon"] == "flag"
    assert events[-1]["payload"]["stage"]["name"] == "Resolved"
    assert events[-1]["payload"]["stage"]["color"] == "blue"
    assert events[-1]["payload"]["stage"]["icon"] == "rocket"
    assert events[-1]["payload"]["reason"] == "stage_removed"
    assert events[-1]["notification_pending"] is True
    renamed = tickets.stages(project.id, user_id="alice")
    renamed["stages"][0]["name"] = "Research"
    for stage in renamed["stages"]:
        stage.update(color=None, icon=None)
    tickets.update_stages(project.id, UpdateTicketStages(**renamed), actor)
    assert all(
        stage["color"] is None and stage["icon"] is None
        for stage in tickets.stages(project.id, user_id="alice")["stages"]
    )
    assert tickets.events(project.id, ticket["id"], user_id="alice")["data"] == events
    assert tickets.get(project.id, ticket["id"], user_id="alice")["stage_id"] == "resolved"


def test_stage_moves_preserve_ordinary_content_and_human_labels(board):
    tickets, _, project = board
    actor = TicketActor("alice")
    configuration = UpdateTicketStages(
        stages=[
            {"id": "done", "name": "Done", "is_complete": False},
            {"id": "verified", "name": "Verified", "is_complete": True},
        ],
        initial_stage_id="done",
    )
    tickets.update_stages(project.id, configuration, actor)
    ticket = tickets.create(
        project.id, CreateTicket(title="Investigate", labels=[{"key": "needs-human"}]), actor
    )
    assert ticket["stage_id"] == "done"
    tickets.update(project.id, ticket["id"], UpdateTicket(stage_id="done"), actor)
    with pytest.raises(OmnigentError):
        tickets.update(
            project.id, ticket["id"], UpdateTicket(stage_id="not-in-this-project"), actor
        )
    completed = tickets.update(project.id, ticket["id"], UpdateTicket(stage_id="verified"), actor)
    assert completed["stage_id"] == "verified"
    assert "completion_summary" not in completed
    assert completed["labels"] == ticket["labels"]
    completed = tickets.update(
        project.id,
        ticket["id"],
        UpdateTicket(stage_id="verified", description="Evidence", comment="Recovery confirmed"),
        actor,
    )
    assert completed["labels"] == ticket["labels"]
    reopened = tickets.update(project.id, ticket["id"], UpdateTicket(stage_id="done"), actor)
    assert reopened["description"] == "Evidence"
    completed = tickets.update(project.id, ticket["id"], UpdateTicket(stage_id="verified"), actor)
    assert completed["description"] == "Evidence"
    assert completed["labels"] == ticket["labels"]
    assert [
        event["payload"]["text"]
        for event in tickets.events(project.id, ticket["id"], user_id="alice")["data"]
        if event["kind"] == "comment"
    ] == ["Recovery confirmed"]


def test_reordering_occupied_stages_needs_no_summary_and_preserves_project_boundaries(board):
    tickets, projects, project = board
    actor = TicketActor("alice")
    ticket = tickets.create(project.id, CreateTicket(title="Not verified"), actor)
    original = tickets.stages(project.id, user_id="alice")
    configuration = {**original, "stages": original["stages"][1:] + original["stages"][:1]}
    saved = tickets.update_stages(project.id, UpdateTicketStages(**configuration), actor)
    assert saved["initial_stage_id"] == "planning"
    assert [stage["id"] for stage in saved["stages"] if stage["is_complete"]] == ["open"]
    other = projects.create(uuid4().hex, "Other", "alice")
    tickets.update_stages(
        other.id,
        UpdateTicketStages(
            stages=[{"id": "elsewhere", "name": "Elsewhere"}], initial_stage_id="elsewhere"
        ),
        actor,
    )
    with pytest.raises(OmnigentError, match="Stage does not belong"):
        tickets.update(project.id, ticket["id"], UpdateTicket(stage_id="elsewhere"), actor)
    for operation in (
        lambda: tickets.stages(project.id, user_id="bob"),
        lambda: tickets.update_stages(
            project.id, UpdateTicketStages(**original), TicketActor("bob")
        ),
        lambda: tickets.list(project.id, user_id="alice", stage_id="elsewhere"),
        lambda: tickets.update_stages(
            project.id, UpdateTicketStages(**original, moves={"unknown": "open"}), actor
        ),
    ):
        with pytest.raises(OmnigentError):
            operation()
    with workspace_scope(9), pytest.raises(OmnigentError):
        tickets.update_stages(project.id, UpdateTicketStages(**original), actor)
    assert tickets.stages(project.id, user_id="alice") == saved
    assert tickets.get(project.id, ticket["id"], user_id="alice") == ticket


def test_failed_stage_move_and_bounded_edit_roll_back_the_entire_configuration(board, monkeypatch):
    tickets, _, project = board
    actor = TicketActor("alice")
    records = [
        tickets.create(project.id, CreateTicket(title=str(index)), actor) for index in range(3)
    ]
    original = tickets.stages(project.id, user_id="alice")
    change = UpdateTicketStages(
        stages=[{"id": "new", "name": "New"}], initial_stage_id="new", moves={"open": "new"}
    )
    with monkeypatch.context() as patch:
        patch.setattr("omnigent.stores.project_ticket_store.STAGE_EDIT_TICKET_MAX", 2)
        with pytest.raises(OmnigentError, match="smaller groups"):
            tickets.update_stages(project.id, change, actor)
    append = tickets._append
    calls = 0

    def fail_second(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("event write failed")
        append(*args)

    with monkeypatch.context() as patch:
        patch.setattr(tickets, "_append", fail_second)
        with pytest.raises(RuntimeError, match="event write"):
            tickets.update_stages(project.id, change, actor)
    assert tickets.stages(project.id, user_id="alice") == original
    for ticket in records:
        assert tickets.get(project.id, ticket["id"], user_id="alice") == ticket
        assert len(tickets.events(project.id, ticket["id"], user_id="alice")["data"]) == 1


def test_mixed_legacy_and_stage_id_tickets_paginate_once_and_move_together(board, db_uri):
    tickets, _, project = board
    actor = TicketActor("alice")
    records = [
        tickets.create(project.id, CreateTicket(title=str(index)), actor) for index in range(6)
    ]
    with get_or_create_engine(db_uri).begin() as connection:
        connection.execute(
            update(SqlProjectTicket)
            .where(
                SqlProjectTicket.project_id == project.id,
                SqlProjectTicket.id.in_([ticket["id"] for ticket in records[::2]]),
            )
            .values(stage_id=None)
        )
    found = []
    after = None
    for _ in range(3):
        page = tickets.list(project.id, user_id="alice", stage_id="open", after=after, limit=2)
        found.extend(page["data"])
        after = page["last_id"]
    assert not page["has_more"]
    assert [ticket["id"] for ticket in found] == sorted(ticket["id"] for ticket in records)
    tickets.update_stages(
        project.id,
        UpdateTicketStages(
            stages=[{"id": "queued", "name": "Queued"}],
            initial_stage_id="queued",
            moves={"open": "queued"},
        ),
        actor,
    )
    assert len(tickets.list(project.id, user_id="alice", stage_id="queued")["data"]) == 6
    for ticket in records:
        assert tickets.get(project.id, ticket["id"], user_id="alice")["stage_id"] == "queued"
