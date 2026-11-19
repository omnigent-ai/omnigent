import random
from uuid import uuid4

import pytest
from sqlalchemy.exc import StatementError

from omnigent.db.db_models import workspace_scope
from omnigent.entities.project_ticket import (
    CommentOnTicket,
    CreateTicket,
    TicketActor,
    UpdateTicket,
)
from omnigent.errors import OmnigentError
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
    assert ticket["title"] == "Deliver" and ticket["status"] == "open"
    assert ticket["created_at"] == ticket["updated_at"]
    tickets.update(project.id, ticket["id"], UpdateTicket(title="First"), actor)
    tickets.update(
        project.id,
        ticket["id"],
        UpdateTicket(title="Last", tags=[{"key": "needs-human", "value": "decision"}]),
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
    assert result["tags"] == [{"key": "needs-human", "value": "decision"}]
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
    done = tickets.update(
        project.id,
        ticket["id"],
        UpdateTicket(status="done", completion_summary="Delivered and tested"),
        actor,
    )
    assert done["completion_summary"] == "Delivered and tested"
    reopened = tickets.update(
        project.id, ticket["id"], UpdateTicket(status="open", tags=[]), actor
    )
    assert reopened["completion_summary"] is None and reopened["tags"] == []
    assert len(tickets.events(project.id, ticket["id"], user_id="alice")["data"]) == 8


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
    tags = [
        {
            "key": str(i),
            "value": "".join(chr(0x10000 + rng.randrange(0x10000)) for _ in range(256)),
        }
        for i in range(20)
    ]
    with pytest.raises(StatementError):
        tickets.update(project.id, ticket["id"], UpdateTicket(tags=tags), actor)
    assert tickets.get(project.id, ticket["id"], user_id="alice")["tags"] == []
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


def test_pagination_and_outbox_order(board):
    tickets, projects, project = board
    actor = TicketActor("alice")
    created = [tickets.create(project.id, CreateTicket(title=str(i)), actor) for i in range(4)]
    page = tickets.list(project.id, user_id="alice", status="open", limit=2)
    rest = tickets.list(project.id, user_id="alice", status="open", after=page["last_id"], limit=2)
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
    projects.set_orchestrator(project.id, orchestrator, user_id="alice")
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
    assert tickets.get(project.id, ticket["id"], user_id="alice")["status"] == "open"
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


def test_plan_review_and_feedback_change_atomically(board, monkeypatch):
    tickets, _, project = board
    actor = TicketActor("alice")
    ticket = tickets.create(project.id, CreateTicket(title="Build"), actor)
    ticket_id = ticket["id"]
    planning = tickets.update(
        project.id,
        ticket_id,
        UpdateTicket(
            status="planning",
            plan="## Approach\n\n- [ ] Verified outcome",
            comment="Agreed in chat",
        ),
        actor,
    )
    assert planning["plan"].startswith("## Approach")
    assert planning["comment_count"] == 1
    review = tickets.update(
        project.id,
        ticket_id,
        UpdateTicket(
            status="review",
            review_summary="Tests passed; please check the layout",
            tags=[{"key": "needs-human"}],
        ),
        actor,
    )
    assert review["review_summary"] and review["status"] == "review"
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
                    status="in_progress",
                    comment="Adjust spacing",
                    tags=[],
                ),
                actor,
            )
    assert tickets.get(project.id, ticket_id, user_id="alice") == review
    working = tickets.update(
        project.id,
        ticket_id,
        UpdateTicket(
            status="in_progress",
            comment="Adjust spacing",
            tags=[],
        ),
        actor,
    )
    assert working["review_summary"] == "" and working["plan"] == planning["plan"]
    assert working["comment_count"] == 2 and working["tags"] == []
    events = tickets.events(project.id, ticket_id, user_id="alice")["data"]
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
    assert tickets.get(project.id, center["id"], user_id="alice")["status"] == "open"
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


def test_lead_handoff_requires_links_and_unlink_clears_only_the_lead(board, db_uri):
    tickets, _, project = board
    actor = TicketActor("alice")
    ticket = tickets.create(project.id, CreateTicket(title="Plan"), actor)
    conversations = SqlAlchemyConversationStore(db_uri)
    first, second = [conversations.create_conversation(project_id=project.id) for _ in range(2)]
    with pytest.raises(OmnigentError, match="Link the session"):
        tickets.update(project.id, ticket["id"], UpdateTicket(lead_session_id=first.id), actor)
    for worker in (first, second):
        tickets.link_session(project.id, ticket["id"], worker.id, "attach", actor)
        tickets.update(project.id, ticket["id"], UpdateTicket(lead_session_id=worker.id), actor)
    kept = tickets.update(project.id, ticket["id"], UpdateTicket(title="Work"), actor)
    assert kept["lead_session_id"] == second.id
    assert len(tickets.sessions(project.id, ticket["id"], user_id="alice")["data"]) == 2
    cleared = tickets.update(project.id, ticket["id"], UpdateTicket(lead_session_id=None), actor)
    assert cleared["lead_session_id"] is None
    tickets.update(project.id, ticket["id"], UpdateTicket(lead_session_id=second.id), actor)
    result = tickets.link_session(project.id, ticket["id"], second.id, "remove", actor)
    assert result["lead_session_id"] is None
    assert (
        tickets.sessions(project.id, ticket["id"], user_id="alice")["data"][0]["session_id"]
        == first.id
    )
    assert tickets.events(project.id, ticket["id"], user_id="alice")["data"][-1]["payload"][
        "changes"
    ] == {"lead_session_id": None}
