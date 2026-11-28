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
    MarkTicketSeen,
    TicketActor,
    UpdateTicket,
    UpdateTicketStages,
    default_ticket_stages,
)
from omnigent.errors import OmnigentError
from omnigent.stores import project_ticket_store
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.stores.project_ticket_store import ProjectTicketStore


def _as_read(ticket, *, needs_final_update=False):
    """An edit's ticket as a single-ticket read returns it, with its final-update state."""
    return {**ticket, "final_update": None, "needs_final_update": needs_final_update}


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
    assert (
        _as_read(tickets.update(project.id, ticket["id"], UpdateTicket(labels=None), actor))
        == result
    )
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
    assert history[-2]["payload"] == {"text": "Delivered and tested", "audience": "human"}


def test_unseen_activity_counts_others_after_the_forward_only_marker(board):
    tickets, _, project = board
    owner = TicketActor("alice")
    agent = TicketActor("alice", uuid4().hex, uuid4().hex)
    mine = tickets.create(project.id, CreateTicket(title="Mine"), owner)
    theirs = tickets.create(project.id, CreateTicket(title="Theirs"), agent)
    tickets.comment(project.id, mine["id"], CommentOnTicket(text="Own note"), owner)
    tickets.comment(
        project.id,
        mine["id"],
        CommentOnTicket(
            text="Done",
            kind="execution",
            outputs=[{"type": "link", "url": "https://example.com/evidence"}],
            audience="human",
        ),
        agent,
    )
    tickets.update(project.id, mine["id"], UpdateTicket(stage_id="review"), agent)
    ids = [mine["id"], theirs["id"]]

    unseen = tickets.unseen_activity(project.id, ids, user_id="alice")
    # Only human-facing comments by others count; creation, stage moves and own notes don't.
    assert unseen[theirs["id"]] == {
        "viewer_seen_position": None,
        "new_comment_count": 0,
        "first_new_position": None,
    }
    assert unseen[mine["id"]] == {
        "viewer_seen_position": None,
        "new_comment_count": 1,
        "first_new_position": 2,
    }

    assert tickets.mark_seen(
        project.id, mine["id"], MarkTicketSeen(seen_position=2), user_id="alice"
    ) == {"viewer_seen_position": 2}
    tickets.comment(project.id, mine["id"], CommentOnTicket(text="Agent note"), agent)
    assert tickets.unseen_activity(project.id, ids, user_id="alice")[mine["id"]] == {
        "viewer_seen_position": 2,
        "new_comment_count": 0,
        "first_new_position": None,
    }
    # The marker never moves backwards and clamps to the latest activity: the agent
    # note at position 5 (created, own note, execution, its output, stage move, note).
    assert tickets.mark_seen(
        project.id, mine["id"], MarkTicketSeen(seen_position=0), user_id="alice"
    ) == {"viewer_seen_position": 2}
    assert tickets.mark_seen(
        project.id, mine["id"], MarkTicketSeen(seen_position=99), user_id="alice"
    ) == {"viewer_seen_position": 5}
    with pytest.raises(OmnigentError):
        tickets.mark_seen(project.id, mine["id"], MarkTicketSeen(seen_position=4), user_id="bob")
    with pytest.raises(OmnigentError):
        tickets.unseen_activity(project.id, ids, user_id="bob")


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
            stage_id="review",
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
        assert tickets.get(project.id, ticket["id"], user_id="alice") == _as_read(expected)
        listed = tickets.list(project.id, user_id="alice")["data"]
        assert [{**row, "latest_comment": None} for row in listed] == [
            {**expected, "latest_comment": None}
        ]
        assert listed[0]["latest_comment"]["text"] == expected["latest_update"]
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
        "latest_created_at": None,
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
    assert tickets.summary(project.id, user_id="alice")["needs_response_count"] == 3
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
        summary = tickets.summary(project.id, user_id="alice")
        assert summary["needs_response_count"] == 2
        assert summary["is_complete"] is False
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


def test_summary_reports_ticket_creation_for_new_ticket_badges(board, monkeypatch):
    tickets, projects, project = board
    actor = TicketActor("alice")
    assert (
        tickets.summary(project.id, user_id="alice", created_after=0)["created_after_count"] == 0
    )
    for created_at in (100, 200, 300):
        monkeypatch.setattr(project_ticket_store, "now_epoch", lambda value=created_at: value)
        tickets.create(project.id, CreateTicket(title=str(created_at)), actor)
    other = projects.create(uuid4().hex, "Other", "alice")
    tickets.create(other.id, CreateTicket(title="Elsewhere"), actor)
    summary = tickets.summary(project.id, user_id="alice", created_after=100)
    assert summary["latest_created_at"] == 300
    assert summary["created_after_count"] == 2
    assert "created_after_count" not in tickets.summary(project.id, user_id="alice")
    assert (
        tickets.summary(project.id, user_id="alice", created_after=300)["created_after_count"] == 0
    )
    assert tickets.summary(
        project.id, user_id="alice", created_after=100, include_responses=False
    ) == {"latest_created_at": 300, "created_after_count": 2}
    empty = projects.create(uuid4().hex, "Empty", "alice")
    assert tickets.summary(empty.id, user_id="alice", include_responses=False) == {
        "latest_created_at": None
    }


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
    # The first page counts every match; later pages don't repeat it.
    assert page["total"] == 4 and "total" not in rest
    assert tickets.list(project.id, user_id="alice", stage_id="open")["total"] == 4
    assert tickets.list(project.id, user_id="alice", stage_id="done", limit=2)["total"] == 0
    assert tickets.list(project.id, user_id="alice", limit=1, requested_by="bob")["total"] == 0
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
            stage_id="review",
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

    def fail_comment(session, project_row, ticket_row, who, kind, payload, **options):
        if kind == "comment":
            raise RuntimeError("feedback unavailable")
        append(session, project_row, ticket_row, who, kind, payload, **options)

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
    assert tickets.get(project.id, ticket_id, user_id="alice") == _as_read(review)
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
    assert events[-1]["payload"] == {"text": "Adjust spacing", "audience": "human"}


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
    assert saved["initial_stage_id"] == "in_progress"
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
    # It sits in the completed stage with no final update yet.
    assert tickets.get(project.id, ticket["id"], user_id="alice") == _as_read(
        ticket, needs_final_update=True
    )


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
        assert tickets.get(project.id, ticket["id"], user_id="alice") == _as_read(ticket)
        assert len(tickets.events(project.id, ticket["id"], user_id="alice")["data"]) == 1


def test_stage_pages_paginate_once_with_the_last_stage_newest_first_and_move_together(board):
    tickets, _, project = board
    actor = TicketActor("alice")
    records = [
        tickets.create(project.id, CreateTicket(title=str(index)), actor) for index in range(6)
    ]

    def pages(stage_id):
        found, after = [], None
        for _ in range(3):
            page = tickets.list(
                project.id, user_id="alice", stage_id=stage_id, after=after, limit=2
            )
            found.extend(page["data"])
            after = page["last_id"]
        assert not page["has_more"]
        return [ticket["id"] for ticket in found]

    oldest_first = [
        ticket["id"]
        for ticket in sorted(records, key=lambda ticket: (ticket["created_at"], ticket["id"]))
    ]
    assert pages("open") == oldest_first
    for ticket in records:
        tickets.update(project.id, ticket["id"], UpdateTicket(stage_id="done"), actor)
    assert pages("done") == oldest_first[::-1]
    tickets.update_stages(
        project.id,
        UpdateTicketStages(
            stages=[{"id": "queued", "name": "Queued"}, {"id": "done", "name": "Done"}],
            initial_stage_id="queued",
            moves={"open": "queued", "in_progress": "queued", "review": "queued"},
        ),
        actor,
    )
    for ticket in records:
        tickets.update(project.id, ticket["id"], UpdateTicket(stage_id="queued"), actor)
    assert len(tickets.list(project.id, user_id="alice", stage_id="queued")["data"]) == 6
    for ticket in records:
        assert tickets.get(project.id, ticket["id"], user_id="alice")["stage_id"] == "queued"


def test_requester_filter_pages_only_that_requesters_tickets(board):
    tickets, _, project = board
    actor = TicketActor("alice")
    mine = [
        tickets.create(project.id, CreateTicket(title=f"mine {index}"), actor)["id"]
        for index in range(3)
    ]
    tickets.create(project.id, CreateTicket(title="theirs"), actor, requested_by="bob")

    first = tickets.list(
        project.id, user_id="alice", stage_id="open", limit=2, requested_by="alice"
    )
    rest = tickets.list(
        project.id,
        user_id="alice",
        stage_id="open",
        limit=2,
        after=first["last_id"],
        requested_by="alice",
    )
    paged = [ticket["id"] for ticket in first["data"] + rest["data"]]
    assert first["has_more"] and not rest["has_more"]
    # Same-second creations order by random id, so compare membership, not order.
    assert len(paged) == len(set(paged)) == 3 and set(paged) == set(mine)
    bobs = tickets.list(project.id, user_id="alice", requested_by="bob")["data"]
    assert [ticket["title"] for ticket in bobs] == ["theirs"]
    assert len(tickets.list(project.id, user_id="alice")["data"]) == 4


def test_project_delete_with_tickets_removes_ticket_history_only_when_asked(board, db_uri):
    tickets, projects, project = board
    actor = TicketActor("alice")
    first, second = [
        tickets.create(project.id, CreateTicket(title=title), actor) for title in ("A", "B")
    ]
    tickets.link_ticket(project.id, first["id"], second["id"], "attach", actor)
    worker = SqlAlchemyConversationStore(db_uri).create_conversation(project_id=project.id)
    tickets.link_session(project.id, first["id"], worker.id, "attach", actor)
    tickets.mark_seen(project.id, first["id"], MarkTicketSeen(seen_position=0), user_id="alice")
    other = projects.create(uuid4().hex, "Other", "alice")
    kept = tickets.create(other.id, CreateTicket(title="Kept"), actor)

    with pytest.raises(OmnigentError, match="cannot be deleted"):
        projects.delete(project.id, user_id="alice")
    assert projects.delete(project.id, user_id="alice", delete_tickets=True) is True

    assert projects.get(project.id, user_id="alice") is None
    # Only the other project's ticket and its creation event remain.
    remaining = {"project_tickets": 1, "project_ticket_events": 1}
    with get_or_create_engine(db_uri).connect() as connection:
        for table in (
            "project_tickets",
            "project_ticket_events",
            "project_ticket_sessions",
            "project_ticket_reads",
            "project_ticket_links",
        ):
            count = connection.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar()
            assert count == remaining.get(table, 0), table
    assert tickets.get(other.id, kept["id"], user_id="alice")["title"] == "Kept"


def test_reliability_loop_notes_are_agent_notes_that_never_wake_anyone(board):
    from omnigent.server.project_reliability import LOOP_AGENT_ID

    tickets, _, project = board
    ticket = tickets.create(project.id, CreateTicket(title="Waiting"), TicketActor("alice"))
    tickets.comment(
        project.id,
        ticket["id"],
        CommentOnTicket(text="Reliability check: woke the worker.", audience="agents"),
        TicketActor("alice", agent_id=LOOP_AGENT_ID),
    )
    pending = [
        event
        for event in tickets.project_pending_notifications(project.id)
        if event["kind"] == "comment"
    ]
    assert [(event["urgent"], event["agent_id"]) for event in pending] == [(False, LOOP_AGENT_ID)]


def test_a_routed_persons_comment_clears_needs_human_but_keeps_routing_to_the_asker(board):
    tickets, _, project = board
    worker = TicketActor("alice", uuid4().hex, uuid4().hex)
    owner = TicketActor("alice")
    ticket = tickets.create(project.id, CreateTicket(title="Decide"), worker)
    tickets.update(
        project.id,
        ticket["id"],
        UpdateTicket(labels=[{"key": "kind", "value": "ux"}, {"key": "needs-human"}]),
        worker,
    )
    # An agent's own comment leaves the ask open.
    tickets.comment(project.id, ticket["id"], CommentOnTicket(text="Ship A or B?"), worker)
    assert len(tickets.human_requests(user_id="alice")["data"]) == 1

    answered = tickets.comment(project.id, ticket["id"], CommentOnTicket(text="B"), owner)

    assert answered["labels"] == [{"key": "kind", "value": "ux"}]
    assert tickets.human_requests(user_id="alice")["data"] == []
    events = tickets.events(project.id, ticket["id"], user_id="alice")["data"]
    comment, cleared = events[-2], events[-1]
    assert comment["payload"]["cleared_needs_human"] is True
    assert cleared["payload"]["cleared_by_comment"] is True
    context = tickets.person_comment_context(project.id, ticket["id"], comment["position"])
    assert context["asker"] == worker.session_id
    assert context["completed"] is False
    done = default_ticket_stages().stages[-1].id
    tickets.update(project.id, ticket["id"], UpdateTicket(stage_id=done), owner)
    later = tickets.comment(project.id, ticket["id"], CommentOnTicket(text="One more fix"), owner)
    position = tickets.events(project.id, later["id"], user_id="alice")["data"][-1]["position"]
    assert tickets.person_comment_context(project.id, ticket["id"], position)["completed"] is True

    # A person's follow-up on an ask they raised themselves doesn't clear it.
    tickets.update(
        project.id,
        ticket["id"],
        UpdateTicket(labels=[{"key": "needs-human"}], comment="Asking the team"),
        owner,
    )
    tickets.comment(project.id, ticket["id"], CommentOnTicket(text="Any news?"), owner)
    assert tickets.get(project.id, ticket["id"], user_id="alice")["labels"] == [
        {"key": "needs-human"}
    ]


def test_a_persons_sign_off_label_answers_the_ask_but_an_agents_does_not(board):
    tickets, _, project = board
    worker = TicketActor("alice", uuid4().hex, uuid4().hex)
    owner = TicketActor("alice")
    ticket = tickets.create(project.id, CreateTicket(title="Risky"), worker)
    asked = [{"key": "risk", "value": "auth"}, {"key": "needs-human"}]
    tickets.update(project.id, ticket["id"], UpdateTicket(labels=asked), worker)

    # An agent adding a sign-off (the route refuses it; the store still mustn't treat it
    # as an answer) leaves the ask open.
    tickets.update(
        project.id, ticket["id"], UpdateTicket(labels=[*asked, {"key": "push-ok"}]), worker
    )
    assert len(tickets.human_requests(user_id="alice")["data"]) == 1

    # The owner removes needs-human themselves while adding risk-ok: the asker is still found.
    signed = tickets.update(
        project.id,
        ticket["id"],
        UpdateTicket(labels=[asked[0], {"key": "push-ok"}, {"key": "risk-ok"}]),
        owner,
    )
    assert {label["key"] for label in signed["labels"]} == {"risk", "push-ok", "risk-ok"}
    event = tickets.events(project.id, ticket["id"], user_id="alice")["data"][-1]
    assert event["payload"]["gate_labels_added"] == ["risk-ok"]
    assert event["payload"]["answered_ask"] is True
    assert "cleared_by_label" not in event["payload"]
    context = tickets.person_comment_context(project.id, ticket["id"], event["position"])
    assert context["asker"] == worker.session_id
    assert tickets.human_requests(user_id="alice")["data"] == []


def test_each_requested_sign_off_is_given_independently(board, db_uri):
    tickets, projects, project = board
    projects.update(project.id, user_id="alice", config={"product_approvers": ["carol"]})
    projects.add_share(project.id, "carol", user_id="alice")
    worker = TicketActor("alice", uuid4().hex, uuid4().hex)
    owner, approver = TicketActor("alice"), TicketActor("carol")

    def offers(ticket_id: str, viewer: str) -> list[str]:
        from types import SimpleNamespace

        from omnigent.server.product_approval import sign_offs_for

        read = tickets.get(project.id, ticket_id, user_id="alice")
        config = projects.get(project.id, user_id="alice").config
        routing = SimpleNamespace(user_id="alice", config=config)
        return [offer["key"] for offer in sign_offs_for(routing, read, viewer)]

    def labels_of(ticket_id: str) -> set[str]:
        return {
            label["key"] for label in tickets.get(project.id, ticket_id, user_id="alice")["labels"]
        }

    # Bob's risky feature asks for both risk-ok (the owner's) and product-ok (approvers').
    asked = [{"key": "kind", "value": "feature"}, {"key": "risk", "value": "auth"}]
    ticket = tickets.create(
        project.id, CreateTicket(title="Both", labels=asked), worker, requested_by="bob"
    )["id"]
    tickets.update(project.id, ticket, UpdateTicket(add_labels=[{"key": "needs-human"}]), worker)
    assert offers(ticket, "alice") == ["risk-ok", "product-ok"]

    # Giving risk-ok keeps the ask, and product-ok is still offered.
    tickets.update(project.id, ticket, UpdateTicket(add_labels=[{"key": "risk-ok"}]), owner)
    event = tickets.events(project.id, ticket, user_id="alice")["data"][-1]
    assert event["payload"]["gate_labels_added"] == ["risk-ok"]
    assert "answered_ask" not in event["payload"]
    assert "needs-human" in labels_of(ticket)
    assert offers(ticket, "alice") == ["product-ok"]
    assert len(tickets.human_requests(user_id="alice")["data"]) == 1

    # The last requested sign-off answers the ask.
    tickets.update(project.id, ticket, UpdateTicket(add_labels=[{"key": "product-ok"}]), owner)
    event = tickets.events(project.id, ticket, user_id="alice")["data"][-2]
    assert event["payload"]["gate_labels_added"] == ["product-ok"]
    assert event["payload"]["answered_ask"] is True
    assert "needs-human" not in labels_of(ticket)
    assert tickets.human_requests(user_id="alice")["data"] == []

    # An approver's product-ok, given first, leaves risk-ok offered to the owner and to
    # that approver (who may give it on any ticket).
    other = tickets.create(
        project.id, CreateTicket(title="Approver first", labels=asked), worker, requested_by="bob"
    )["id"]
    tickets.update(project.id, other, UpdateTicket(add_labels=[{"key": "needs-human"}]), worker)
    tickets.update(project.id, other, UpdateTicket(add_labels=[{"key": "product-ok"}]), approver)
    assert "needs-human" in labels_of(other)
    assert offers(other, "alice") == ["risk-ok"] and offers(other, "carol") == ["risk-ok"]
    assert len(tickets.human_requests(user_id="alice")["data"]) == 1


def test_concurrent_single_label_changes_both_survive(board):
    import threading

    tickets, _, project = board
    owner = TicketActor("alice")
    ticket = tickets.create(
        project.id, CreateTicket(title="Race", labels=[{"key": "kind", "value": "fix"}]), owner
    )
    start = threading.Barrier(2)

    def add(key: str) -> None:
        start.wait()
        tickets.update(project.id, ticket["id"], UpdateTicket(add_labels=[{"key": key}]), owner)

    threads = [threading.Thread(target=add, args=(key,)) for key in ("approved", "risk-ok")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    keys = {
        label["key"] for label in tickets.get(project.id, ticket["id"], user_id="alice")["labels"]
    }
    assert keys == {"kind", "approved", "risk-ok"}

    # Removing one key and replacing another by key leave every other label alone.
    after = tickets.update(
        project.id,
        ticket["id"],
        UpdateTicket(add_labels=[{"key": "kind", "value": "ux"}], remove_labels=["approved"]),
        owner,
    )
    assert after["labels"] == [{"key": "kind", "value": "ux"}, {"key": "risk-ok"}]
    # A full stale list still replaces everything, which is why the board sends changes.
    stale = tickets.update(
        project.id, ticket["id"], UpdateTicket(labels=[{"key": "kind", "value": "ux"}]), owner
    )
    assert stale["labels"] == [{"key": "kind", "value": "ux"}]


def test_label_changes_are_validated():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="not both"):
        UpdateTicket(labels=[], add_labels=[{"key": "a"}])
    with pytest.raises(ValidationError, match="both added and removed"):
        UpdateTicket(add_labels=[{"key": "a"}], remove_labels=["a"])
    with pytest.raises(ValidationError, match="unique"):
        UpdateTicket(add_labels=[{"key": "a"}, {"key": "a", "value": "b"}])
    full = [{"key": f"k{index}"} for index in range(20)]
    with pytest.raises(ValueError, match="at most 20"):
        UpdateTicket(add_labels=[{"key": "one-more"}]).labels_after(full)
    assert UpdateTicket(title="t").labels_after(full) is None


def test_a_ticket_says_who_its_open_ask_waits_on_until_it_is_answered(board):
    tickets, _, project = board
    worker = TicketActor("alice", uuid4().hex, uuid4().hex)
    ticket = tickets.create(project.id, CreateTicket(title="Decide"), worker)
    assert tickets.get(project.id, ticket["id"], user_id="alice")["waiting_on"] is None
    tickets.update(
        project.id,
        ticket["id"],
        UpdateTicket(labels=[{"key": "needs-human"}], comment="Weekly digest, yes or no?"),
        worker,
    )
    waiting = tickets.get(project.id, ticket["id"], user_id="alice")["waiting_on"]
    assert {key: waiting[key] for key in ("reason", "responders", "raised_by_user_id")} == {
        "reason": "owner",
        "responders": ["alice"],
        "raised_by_user_id": None,
    }
    # What the ticket's decision box shows: the asker's question, where and when it was raised.
    assert waiting["question"] == "Weekly digest, yes or no?"
    assert waiting["raised_by_session_id"] == worker.session_id
    assert isinstance(waiting["ask_position"], int) and waiting["raised_at"] > 0
    assert tickets.list(project.id, user_id="alice")["data"][0]["waiting_on"] == waiting

    tickets.comment(project.id, ticket["id"], CommentOnTicket(text="B"), TicketActor("alice"))

    assert tickets.list(project.id, user_id="alice")["data"][0]["waiting_on"] is None


def test_list_keeps_the_latest_human_comment_across_later_moves_and_label_edits(board):
    tickets, _, project = board
    person = TicketActor("alice")
    worker = TicketActor("alice", uuid4().hex, uuid4().hex)
    ticket = tickets.create(project.id, CreateTicket(title="Card"), worker)
    untouched = tickets.create(project.id, CreateTicket(title="Quiet"), worker)

    def latest(ticket_id):
        rows = tickets.list(project.id, user_id="alice")["data"]
        return next(row for row in rows if row["id"] == ticket_id)

    assert latest(untouched["id"])["latest_comment"] is None
    tickets.comment(project.id, ticket["id"], CommentOnTicket(text="Ship it Friday"), person)
    shown = latest(ticket["id"])["latest_comment"]
    assert shown["text"] == "Ship it Friday" and shown["by_person"] is True
    assert shown["user_id"] == "alice" and shown["session_id"] is None
    assert shown["created_at"] > 0 and shown["position"] == 1

    # A worker's agent note, a stage move and a label edit don't replace it.
    tickets.comment(project.id, ticket["id"], CommentOnTicket(text="ran tsc"), worker)
    tickets.update(project.id, ticket["id"], UpdateTicket(stage_id="in_progress"), worker)
    tickets.update(project.id, ticket["id"], UpdateTicket(labels=[{"key": "kind"}]), worker)
    row = latest(ticket["id"])
    assert row["latest_update"] != "Ship it Friday"
    assert row["latest_comment"]["text"] == "Ship it Friday"

    # A worker's human-audience comment is the newest one for the human.
    tickets.comment(
        project.id,
        ticket["id"],
        CommentOnTicket(text="Which date works?", audience="human"),
        worker,
    )
    shown = latest(ticket["id"])["latest_comment"]
    assert shown["text"] == "Which date works?" and shown["by_person"] is False
    assert shown["session_id"] == worker.session_id


def test_an_askers_later_agent_note_does_not_replace_the_question(board):
    tickets, _, project = board
    worker = TicketActor("alice", uuid4().hex, uuid4().hex)
    ticket = tickets.create(project.id, CreateTicket(title="Decide"), worker)
    tickets.update(
        project.id,
        ticket["id"],
        UpdateTicket(labels=[{"key": "needs-human"}], comment="Ship on Friday or Monday?"),
        worker,
    )

    def shown():
        waiting = tickets.get(project.id, ticket["id"], user_id="alice")["waiting_on"]
        inbox = tickets.human_requests(user_id="alice")["data"]
        return waiting["question"], [item["question"] for item in inbox]

    asked = tickets.get(project.id, ticket["id"], user_id="alice")["waiting_on"]["ask_position"]
    tickets.comment(
        project.id,
        ticket["id"],
        CommentOnTicket(text="ran the targeted tests", audience="agents"),
        worker,
    )
    # The decision box and the Inbox card keep the real question and its anchor.
    assert shown() == ("Ship on Friday or Monday?", ["Ship on Friday or Monday?"])
    assert (
        tickets.get(project.id, ticket["id"], user_id="alice")["waiting_on"]["ask_position"]
        == asked
    )

    tickets.comment(
        project.id,
        ticket["id"],
        CommentOnTicket(text="Update: Monday is safer. Friday or Monday?", audience="human"),
        worker,
    )
    restated = "Update: Monday is safer. Friday or Monday?"
    assert shown() == (restated, [restated])


def test_an_askers_later_note_that_defaulted_to_the_human_does_not_replace_the_question(board):
    tickets, _, project = board
    worker = TicketActor("alice", uuid4().hex, uuid4().hex)
    ticket = tickets.create(project.id, CreateTicket(title="Decide"), worker)
    tickets.update(
        project.id,
        ticket["id"],
        UpdateTicket(labels=[{"key": "needs-human", "value": "Ship day?"}], comment="Friday?"),
        worker,
    )

    def waiting():
        return tickets.get(project.id, ticket["id"], user_id="alice")["waiting_on"]

    asked = waiting()["ask_position"]
    # While its own ask is open the worker's notes default to the human, but they only
    # report progress: neither a plain comment nor one with an attachment is the question.
    note = tickets.comment(
        project.id, ticket["id"], CommentOnTicket(text="attached verification log"), worker
    )
    assert note["latest_update"] == "attached verification log"
    tickets.comment(
        project.id,
        ticket["id"],
        CommentOnTicket(
            text="screenshot", outputs=[{"type": "link", "url": "https://example.com/log"}]
        ),
        worker,
    )
    tickets.update(project.id, ticket["id"], UpdateTicket(comment="ran tsc"), worker)
    assert (waiting()["question"], waiting()["ask_position"]) == ("Friday?", asked)

    # Changing the needs-human question re-anchors it on the comment sent with the change.
    tickets.update(
        project.id,
        ticket["id"],
        UpdateTicket(
            labels=[{"key": "needs-human", "value": "Ship day, given the freeze?"}],
            comment="Monday, given the freeze?",
        ),
        worker,
    )
    assert waiting()["question"] == "Monday, given the freeze?"
    assert waiting()["ask_position"] > asked


def test_an_asks_question_ignores_an_earlier_resolved_ask(board):
    tickets, _, project = board
    worker = TicketActor("alice", uuid4().hex, uuid4().hex)
    ticket = tickets.create(project.id, CreateTicket(title="Decide"), worker)
    # A question posted just before the label is the ask's question.
    tickets.comment(
        project.id,
        ticket["id"],
        CommentOnTicket(text="Old: which region?", audience="human"),
        worker,
    )
    tickets.update(project.id, ticket["id"], UpdateTicket(labels=[{"key": "needs-human"}]), worker)
    waiting = tickets.get(project.id, ticket["id"], user_id="alice")["waiting_on"]
    assert waiting["question"] == "Old: which region?"

    # That ask resolves; a new one is raised with the comment left as an agent note.
    tickets.update(project.id, ticket["id"], UpdateTicket(labels=[]), worker)
    tickets.update(
        project.id,
        ticket["id"],
        UpdateTicket(
            labels=[{"key": "needs-human"}],
            comment="New: OK to drop the cache?",
            audience="agents",
        ),
        worker,
    )
    waiting = tickets.get(project.id, ticket["id"], user_id="alice")["waiting_on"]
    assert waiting["question"] == "New: OK to drop the cache?"
    inbox = tickets.human_requests(user_id="alice")["data"]
    assert [item["question"] for item in inbox] == ["New: OK to drop the cache?"]
