"""Tests for the SQLAlchemy elicitation store."""

from __future__ import annotations

import pytest

from omnigent.entities import Elicitation
from omnigent.stores.elicitation_store.sqlalchemy_store import SqlAlchemyElicitationStore


@pytest.fixture()
def elicitation_store(db_uri: str) -> SqlAlchemyElicitationStore:
    """
    :returns: A SqlAlchemyElicitationStore backed by the test database.
    """
    return SqlAlchemyElicitationStore(db_uri)


def _make(
    elicitation_id: str = "elicit_abc",
    conversation_id: str = "conv_11111111111111111111111111111111",
    created_at: int = 1_000,
    message: str = "Approve `rm -rf /tmp/x`?",
) -> Elicitation:
    """Build an :class:`Elicitation` with a realistic event payload."""
    return Elicitation(
        id=elicitation_id,
        workspace_id=0,
        conversation_id=conversation_id,
        created_at=created_at,
        event={
            "type": "response.elicitation_request",
            "elicitation_id": elicitation_id,
            "method": "elicitation/create",
            "params": {"mode": "form", "message": message},
        },
    )


def test_put_then_list_round_trips_the_event(
    elicitation_store: SqlAlchemyElicitationStore,
) -> None:
    """The stored payload comes back byte-identical, so a restore replays it.

    ``conversation_id`` is the one field that does not round-trip verbatim:
    like every other id column, it is a ``Uuid16``, which strips the ``conv_``
    prefix on the way in and hands back the bare hex. Both forms resolve to the
    same row on query, and the restore path keys the index off the id it was
    called with rather than the one it read back.
    """
    original = _make()
    elicitation_store.put(original)

    rows = elicitation_store.list_for_conversation(original.conversation_id)

    assert len(rows) == 1
    assert rows[0].id == original.id
    assert rows[0].conversation_id == original.conversation_id.removeprefix("conv_")
    assert rows[0].created_at == original.created_at
    assert rows[0].event == original.event


def test_put_is_an_upsert(elicitation_store: SqlAlchemyElicitationStore) -> None:
    """A re-published prompt overwrites rather than raising or duplicating.

    Native harnesses mint deterministic ids that repeat across polls for the
    same gated tool call, so the same id genuinely arrives more than once.
    """
    elicitation_store.put(_make(message="first"))
    elicitation_store.put(_make(message="second", created_at=2_000))

    rows = elicitation_store.list_for_conversation(_make().conversation_id)

    assert len(rows) == 1
    assert rows[0].event["params"]["message"] == "second"
    assert rows[0].created_at == 2_000


def test_delete_reports_whether_it_won(
    elicitation_store: SqlAlchemyElicitationStore,
) -> None:
    """Two racing resolves must not both believe they resolved the prompt."""
    elicitation = _make()
    elicitation_store.put(elicitation)

    first = elicitation_store.delete(elicitation.conversation_id, elicitation.id)
    second = elicitation_store.delete(elicitation.conversation_id, elicitation.id)

    assert first is True
    assert second is False
    assert elicitation_store.list_for_conversation(elicitation.conversation_id) == []


def test_delete_refuses_a_mismatched_conversation(
    elicitation_store: SqlAlchemyElicitationStore,
) -> None:
    """An id alone must not let one session resolve another session's prompt."""
    elicitation = _make()
    elicitation_store.put(elicitation)

    deleted = elicitation_store.delete(
        "conv_22222222222222222222222222222222",
        elicitation.id,
    )

    assert deleted is False
    assert len(elicitation_store.list_for_conversation(elicitation.conversation_id)) == 1


def test_list_is_scoped_to_one_conversation(
    elicitation_store: SqlAlchemyElicitationStore,
) -> None:
    """A session must not see another session's outstanding prompts."""
    mine = _make(elicitation_id="elicit_mine")
    theirs = _make(
        elicitation_id="elicit_theirs",
        conversation_id="conv_22222222222222222222222222222222",
    )
    elicitation_store.put(mine)
    elicitation_store.put(theirs)

    rows = elicitation_store.list_for_conversation(mine.conversation_id)

    assert [row.id for row in rows] == ["elicit_mine"]


def test_list_orders_oldest_first(elicitation_store: SqlAlchemyElicitationStore) -> None:
    """Restored prompts render in the order they were raised."""
    elicitation_store.put(_make(elicitation_id="elicit_second", created_at=2_000))
    elicitation_store.put(_make(elicitation_id="elicit_first", created_at=1_000))

    rows = elicitation_store.list_for_conversation(_make().conversation_id)

    assert [row.id for row in rows] == ["elicit_first", "elicit_second"]


def test_list_can_skip_prompts_too_old_to_have_an_awaiter(
    elicitation_store: SqlAlchemyElicitationStore,
) -> None:
    """Nothing can still be parked on a prompt older than the maximum park."""
    elicitation_store.put(_make(elicitation_id="elicit_stale", created_at=1_000))
    elicitation_store.put(_make(elicitation_id="elicit_fresh", created_at=5_000))

    rows = elicitation_store.list_for_conversation(
        _make().conversation_id,
        not_before=4_000,
    )

    assert [row.id for row in rows] == ["elicit_fresh"]


def test_delete_for_conversation_clears_the_session(
    elicitation_store: SqlAlchemyElicitationStore,
) -> None:
    """Deleting a conversation must not leave its prompts orphaned.

    The schema has no foreign keys, so this cleanup is the application's job.
    """
    elicitation_store.put(_make(elicitation_id="elicit_one"))
    elicitation_store.put(_make(elicitation_id="elicit_two"))
    survivor = _make(
        elicitation_id="elicit_other_session",
        conversation_id="conv_22222222222222222222222222222222",
    )
    elicitation_store.put(survivor)

    deleted = elicitation_store.delete_for_conversation(_make().conversation_id)

    assert deleted == 2
    assert elicitation_store.list_for_conversation(_make().conversation_id) == []
    assert len(elicitation_store.list_for_conversation(survivor.conversation_id)) == 1
