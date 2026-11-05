"""Tests for :class:`SessionObjectiveStore` against a migrated database."""

from __future__ import annotations

import asyncio
import json

import pytest
import sqlalchemy as sa

from omnigent.db.utils import get_or_create_engine
from omnigent.stores import session_objective_store
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.session_objective_store import (
    SessionObjectiveLimitError,
    SessionObjectiveNotFoundError,
    SessionObjectiveStore,
    UnreadableSessionObjectiveError,
)

SESSION = "4e92b5a0c0ee6db3f874f9c4a3f855a5"
OTHER = "5d29bee4350489d66feafecfebd94a97"
MISSING = "f" * 32
RUN_1 = "1" * 32
RUN_2 = "2" * 32


@pytest.fixture()
def conversations(db_uri: str) -> SqlAlchemyConversationStore:
    conversations = SqlAlchemyConversationStore(db_uri)
    for session in (SESSION, OTHER):
        conversations.create_conversation(conversation_id=session)
    return conversations


@pytest.fixture()
def store(db_uri: str, conversations: SqlAlchemyConversationStore) -> SessionObjectiveStore:
    return SessionObjectiveStore(db_uri)


def _outcome(store: SessionObjectiveStore, session: str = SESSION, **overrides: object):
    values: dict[str, object] = {"success": True, "emoji": "✅", "text": "Done"}
    values.update(overrides)
    return store.record_outcome(session, **values)  # type: ignore[arg-type]


def test_objectives_are_kept_in_order_with_their_outcomes(store: SessionObjectiveStore) -> None:
    first = store.record_objective(SESSION, "  Fix the\n flaky test ")
    second = store.record_objective(SESSION, "Ship the release")
    assert first.objective == "Fix the flaky test"
    assert first.outcome is None
    resolved = _outcome(store, success=False, emoji=" 🧯 ", text="Blocked:  CI is down")

    # The outcome resolves the latest objective, normalized like the request.
    assert resolved.id == second.id
    assert resolved.outcome is not None
    assert (resolved.outcome.success, resolved.outcome.emoji, resolved.outcome.text) == (
        False,
        "🧯",
        "Blocked: CI is down",
    )
    page = store.list_objectives(SESSION)
    assert [(o.id, o.objective) for o in page.data] == [
        (first.id, "Fix the flaky test"),
        (second.id, "Ship the release"),
    ]
    assert page.data[0].outcome is None
    assert page.data[1].outcome == resolved.outcome
    assert page.next_cursor is None
    assert store.list_objectives(OTHER).data == []


def test_a_named_objective_is_resolved_and_a_second_outcome_replaces_the_first(
    store: SessionObjectiveStore,
) -> None:
    first = store.record_objective(SESSION, "Fix the flaky test")
    store.record_objective(SESSION, "Ship the release")
    _outcome(store, objective_id=first.id, success=False, emoji="🚧", text="Half done")
    replaced = _outcome(store, objective_id=first.id.upper(), emoji="🚀", text="Fixed")

    assert replaced.id == first.id
    assert replaced.outcome is not None
    assert (replaced.outcome.success, replaced.outcome.text) == (True, "Fixed")
    by_id = {o.id: o for o in store.list_objectives(SESSION).data}
    assert by_id[first.id].outcome == replaced.outcome
    assert [o.outcome for o in by_id.values() if o.id != first.id] == [None]


def test_an_outcome_without_an_objective_gets_a_row_of_its_own(
    store: SessionObjectiveStore,
) -> None:
    recorded = _outcome(store, text="Nothing to do")
    assert recorded.objective is None
    assert recorded.outcome is not None
    assert [o.id for o in store.list_objectives(SESSION).data] == [recorded.id]
    # Recording again resolves that same row instead of adding another.
    again = _outcome(store, text="Still nothing to do")
    assert again.id == recorded.id
    assert len(store.list_objectives(SESSION).data) == 1


def test_an_outcome_in_a_run_resolves_only_that_runs_objectives(
    store: SessionObjectiveStore,
) -> None:
    """A continued session's earlier run keeps its outcome when the next run records one."""
    earlier = store.record_objective(SESSION, "Triage issues", scheduled_task_run_id=RUN_1)
    _outcome(store, scheduled_task_run_id=RUN_1, text="Triaged 3")

    # The next run states no objective: its outcome gets its own row, on its run.
    later = _outcome(store, scheduled_task_run_id=RUN_2, success=False, text="API down")

    assert later.id != earlier.id
    assert later.scheduled_task_run_id == RUN_2
    runs = store.list_run_objectives([RUN_1, RUN_2], limit_per_run=5)
    assert [(o.id, o.outcome.text if o.outcome else None) for o in runs[RUN_1][0]] == [
        (earlier.id, "Triaged 3")
    ]
    assert [o.id for o in runs[RUN_2][0]] == [later.id]


def test_a_named_objective_must_belong_to_the_session(store: SessionObjectiveStore) -> None:
    other = store.record_objective(OTHER, "Somebody else's goal")
    with pytest.raises(SessionObjectiveNotFoundError):
        _outcome(store, objective_id=other.id)
    with pytest.raises(SessionObjectiveNotFoundError):
        _outcome(store, objective_id="a" * 32)
    with pytest.raises(ValueError):
        _outcome(store, objective_id="latest")
    assert store.list_objectives(SESSION).data == []
    assert store.list_objectives(OTHER).data[0].outcome is None


def test_writes_need_an_existing_session(store: SessionObjectiveStore) -> None:
    with pytest.raises(SessionObjectiveNotFoundError):
        store.record_objective(MISSING, "Goal")
    with pytest.raises(SessionObjectiveNotFoundError):
        _outcome(store, MISSING)


@pytest.mark.parametrize(
    ("call", "match"),
    [
        (lambda s: s.record_objective(SESSION, " "), "must not be empty"),
        (lambda s: s.record_objective(SESSION, "x" * 201), "at most 200"),
        (lambda s: _outcome(s, emoji="ok"), "exactly one emoji"),
        (lambda s: _outcome(s, text="x" * 81), "at most 80"),
    ],
)
def test_invalid_values_are_rejected_without_writing(
    store: SessionObjectiveStore, call, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        call(store)
    assert store.list_objectives(SESSION).data == []


def test_a_full_session_refuses_more_objectives(
    store: SessionObjectiveStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_objective_store, "MAX_OBJECTIVES_PER_SESSION", 2)
    store.record_objective(SESSION, "One")
    _outcome(store, objective_id=store.record_objective(SESSION, "Two").id)
    with pytest.raises(SessionObjectiveLimitError):
        store.record_objective(SESSION, "Three")
    # Resolving an existing objective still works on a full session.
    assert _outcome(store, text="Updated").objective == "Two"
    # The cap is per session.
    store.record_objective(OTHER, "One")


def test_listing_pages_forward_without_gaps(store: SessionObjectiveStore) -> None:
    recorded = [store.record_objective(SESSION, f"Goal {i}").id for i in range(5)]
    seen: list[str] = []
    cursor = None
    while True:
        page = store.list_objectives(SESSION, limit=2, cursor=cursor)
        seen.extend(o.id for o in page.data)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == recorded
    with pytest.raises(ValueError):
        store.list_objectives(SESSION, cursor="not-a-cursor")
    with pytest.raises(ValueError):
        store.list_objectives(SESSION, limit=0)


def test_ties_on_creation_time_are_ordered_by_id(
    store: SessionObjectiveStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Objectives recorded in the same microsecond still page and resolve deterministically."""
    monkeypatch.setattr(session_objective_store, "now_epoch_us", lambda: 1_000)
    ids = sorted(store.record_objective(SESSION, f"Goal {i}").id for i in range(3))
    first = store.list_objectives(SESSION, limit=2)
    second = store.list_objectives(SESSION, limit=2, cursor=first.next_cursor)
    assert [o.id for o in first.data + second.data] == ids
    assert _outcome(store).id == ids[-1]


def test_run_objectives_are_bounded_per_run(
    store: SessionObjectiveStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run with many objectives cannot crowd out the others, across statement chunks."""
    monkeypatch.setattr(session_objective_store, "_RUN_READS_PER_STATEMENT", 2)
    busy = [
        store.record_objective(SESSION, f"Busy {i}", scheduled_task_run_id=RUN_1).id
        for i in range(4)
    ]
    quiet = store.record_objective(OTHER, "Quiet", scheduled_task_run_id=RUN_2).id
    no_objectives = "3" * 32

    runs = store.list_run_objectives([RUN_1, no_objectives, RUN_2, RUN_1], limit_per_run=2)

    assert set(runs) == {RUN_1, RUN_2}
    # The newest two, in creation order.
    assert [o.id for o in runs[RUN_1][0]] == busy[-2:]
    assert runs[RUN_1][1] is True
    assert [o.id for o in runs[RUN_2][0]] == [quiet]
    assert runs[RUN_2][1] is False
    assert store.list_run_objectives([], limit_per_run=2) == {}


def _stored_payload(db_uri: str, column: str, payload: bytes) -> None:
    with get_or_create_engine(db_uri).begin() as conn:
        conn.execute(sa.text(f"UPDATE session_objectives SET {column} = :p"), {"p": payload})


def test_payloads_are_opaque_envelopes(db_uri: str, store: SessionObjectiveStore) -> None:
    store.record_objective(SESSION, "Ship it")
    _outcome(store, emoji="🚀", text="Shipped")
    with get_or_create_engine(db_uri).connect() as conn:
        objective, outcome = conn.execute(
            sa.text("SELECT objective_payload, outcome_payload FROM session_objectives")
        ).one()
    assert json.loads(bytes(objective)) == {"plaintext": {"text": "Ship it"}}
    assert json.loads(bytes(outcome)) == {"plaintext": {"emoji": "🚀", "text": "Shipped"}}


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (b'{"ciphertext":"AAAA"}', "needs a store that can decrypt it"),
        (b'{"plaintext":{"emoji":1}}', "unrecognized"),
        (b"\xff", "unreadable"),
    ],
)
def test_an_envelope_this_build_cannot_read_is_reported(
    db_uri: str, store: SessionObjectiveStore, payload: bytes, match: str
) -> None:
    _outcome(store)
    _stored_payload(db_uri, "outcome_payload", payload)
    with pytest.raises(UnreadableSessionObjectiveError, match=match):
        store.list_objectives(SESSION)


def test_deleting_a_session_deletes_its_subtrees_objectives_in_bounded_batches(
    store: SessionObjectiveStore,
    conversations: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session and its descendants lose their objectives, a few rows per transaction."""
    from omnigent.stores.conversation_store import sqlalchemy_store as conversation_sql

    monkeypatch.setattr(conversation_sql, "_OBJECTIVE_DELETE_BATCH", 2)
    monkeypatch.setattr(conversation_sql, "_OBJECTIVE_DELETE_SESSIONS", 1)
    child = conversations.create_conversation(kind="sub_agent", parent_conversation_id=SESSION)
    for i in range(5):
        store.record_objective(SESSION, f"Goal {i}")
    for i in range(3):
        store.record_objective(child.id, f"Child goal {i}")
    store.record_objective(OTHER, "Other goal")
    batches: list[int] = []
    original = SqlAlchemyConversationStore._delete_objective_batch

    def counted(self: SqlAlchemyConversationStore, ids: list[str]) -> int:
        deleted = original(self, ids)
        batches.append(deleted)
        return deleted

    monkeypatch.setattr(SqlAlchemyConversationStore, "_delete_objective_batch", counted)

    assert asyncio.run(conversations.delete_conversation(SESSION))

    assert store.list_objectives(SESSION).data == []
    assert store.list_objectives(child.id).data == []
    assert [o.objective for o in store.list_objectives(OTHER).data] == ["Other goal"]
    assert sum(batches) == 8
    assert max(batches) <= 2
