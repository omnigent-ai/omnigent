"""Durable native Plan snapshots retain session and workspace ownership."""

from __future__ import annotations

import pytest

from omnigent.session_todos import validate_session_todos
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

TODOS = [{"content": "Verify the example", "status": "in_progress", "activeForm": "Verifying"}]


def test_plan_persists_in_a_new_store_and_empty_update_clears_it(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    conversation = conversation_store.create_conversation(title="Plan fixture")
    assert conversation.session_todos == []
    assert conversation_store.set_session_todos(conversation.id, TODOS)
    replacement = SqlAlchemyConversationStore(db_uri)
    assert replacement.get_conversation(conversation.id).session_todos == TODOS
    assert replacement.set_session_todos(conversation.id, [])
    assert conversation_store.get_conversation(conversation.id).session_todos == []


def test_fork_and_agent_switch_start_without_the_previous_native_plan(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conversation = conversation_store.create_conversation(title="Plan fixture")
    conversation_store.set_session_todos(conversation.id, TODOS)
    fork = conversation_store.fork_conversation(conversation.id)
    assert fork.session_todos == []
    assert conversation_store.get_conversation(conversation.id).session_todos == TODOS
    switched = conversation_store.switch_conversation_agent(
        conversation.id,
        new_agent_id="af75a9579488e3520ba6842699e43323",
        new_agent_name="fixture",
        new_agent_bundle_location="fixture/bundle",
        new_agent_description=None,
        copy_model_settings=False,
        carry_history_into_native=False,
        presentation_labels={},
        previous_builtin_id=None,
    )
    assert switched.session_todos == []


@pytest.mark.asyncio
async def test_deleted_session_is_not_recreated_by_late_plan(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conversation = conversation_store.create_conversation(title="Plan fixture")
    assert await conversation_store.delete_conversation(conversation.id)
    assert not conversation_store.set_session_todos(conversation.id, TODOS)
    assert conversation_store.get_conversation(conversation.id) is None


@pytest.mark.parametrize("status", [[], {}, None, 1, "unknown"])
def test_malformed_statuses_are_filtered_without_hash_errors(status: object) -> None:
    assert (
        validate_session_todos([{"content": "bad", "status": status, "activeForm": "Bad"}]) == []
    )


def test_plan_validation_drops_unknown_fields_and_bounds_storage() -> None:
    assert validate_session_todos([{**TODOS[0], "unrelated": "discard"}]) == TODOS
    for payload in (
        TODOS * 101,
        [{**TODOS[0], "content": "x" * 4097}],
        [{**TODOS[0], "content": "字" * 4096, "activeForm": "字" * 4096}] * 11,
    ):
        with pytest.raises(ValueError):
            validate_session_todos(payload)
