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


def test_failed_event_write_rolls_back_ticket_change(board, monkeypatch):
    tickets, _, project = board
    actor = TicketActor("alice")
    ticket = tickets.create(project.id, CreateTicket(title="Original"), actor)

    def reject(*args, **kwargs):
        raise RuntimeError("event storage unavailable")

    monkeypatch.setattr(tickets, "_append", reject)
    with pytest.raises(RuntimeError, match="event storage"):
        tickets.update(project.id, ticket["id"], UpdateTicket(title="Lost edit"), actor)
    assert tickets.get(project.id, ticket["id"], user_id="alice")["title"] == "Original"
    assert len(tickets.events(project.id, ticket["id"], user_id="alice")["data"]) == 1


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
    tickets.link_session(project.id, ticket["id"], session.id, "attach", actor)
    tickets.link_session(project.id, ticket["id"], session.id, "attach", actor)
    assert len(tickets.events(project.id, ticket["id"], user_id="alice")["data"]) == 2
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
