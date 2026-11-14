"""Explicit native-session transitions do not weaken ordinary identity writes."""

import pytest

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.mark.parametrize("fixture", ["conversation_store", "split_db_conversation_store"])
def test_native_session_transition_compare_and_swap(
    request: pytest.FixtureRequest, fixture: str
) -> None:
    store: SqlAlchemyConversationStore = request.getfixturevalue(fixture)
    conversation = store.create_conversation(title="OMP transition")
    store.set_external_session_id(conversation.id, "old")
    with pytest.raises(ValueError):
        store.set_external_session_id(conversation.id, "new")
    updated = store.transition_external_session_id(conversation.id, "old", "new")
    assert updated.external_session_id == "new"
    assert (
        store.transition_external_session_id(conversation.id, "old", "new").external_session_id
        == "new"
    )
    with pytest.raises(ValueError):
        store.transition_external_session_id(conversation.id, "old", "stale")
    assert store.get_conversation(conversation.id).external_session_id == "new"


def test_native_session_transition_from_uncaptured_identity(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conversation = conversation_store.create_conversation(title="early OMP clear")
    updated = conversation_store.transition_external_session_id(conversation.id, None, "new")
    assert updated.external_session_id == "new"
