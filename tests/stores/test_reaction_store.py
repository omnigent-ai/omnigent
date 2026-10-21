"""Tests for :class:`SqlAlchemyReactionStore` against a real SQLite database."""

from __future__ import annotations

import pytest

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.reaction_store.sqlalchemy_store import SqlAlchemyReactionStore

_CONV = "4e92b5a0c0ee6db3f874f9c4a3f855a5"


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyReactionStore:
    return SqlAlchemyReactionStore(db_uri)


def test_set_persists_rating_note_and_author(store: SqlAlchemyReactionStore) -> None:
    reaction = store.set(_CONV, "resp_1", "down", note="Too vague", created_by="alice@example.com")
    assert reaction.rating == "down"
    assert reaction.note == "Too vague"
    assert reaction.created_by == "alice@example.com"
    assert reaction.created_at > 0
    assert reaction.updated_at >= reaction.created_at * 1_000_000
    [listed] = store.list_for_user(_CONV, "alice@example.com")
    assert listed == reaction


def test_set_again_replaces_rating_and_note_in_place(store: SqlAlchemyReactionStore) -> None:
    first = store.set(_CONV, "resp_1", "down", note="meh", created_by="alice@example.com")
    second = store.set(_CONV, "resp_1", "up", created_by="alice@example.com")
    assert second.rating == "up"
    assert second.note is None
    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at
    assert len(store.list_for_user(_CONV, "alice@example.com")) == 1


def test_users_reactions_are_independent(store: SqlAlchemyReactionStore) -> None:
    store.set(_CONV, "resp_1", "up", created_by="alice@example.com")
    store.set(_CONV, "resp_1", "down", created_by="bob@example.com")
    assert [r.rating for r in store.list_for_user(_CONV, "alice@example.com")] == ["up"]
    assert [r.rating for r in store.list_for_user(_CONV, "bob@example.com")] == ["down"]


def test_single_user_mode_round_trips_without_an_author(store: SqlAlchemyReactionStore) -> None:
    reaction = store.set(_CONV, "resp_1", "up")
    assert reaction.created_by is None
    assert [r.response_id for r in store.list_for_user(_CONV)] == ["resp_1"]
    assert store.list_for_user(_CONV, "alice@example.com") == []


def test_clear_removes_only_the_callers_reaction(store: SqlAlchemyReactionStore) -> None:
    store.set(_CONV, "resp_1", "up", created_by="alice@example.com")
    store.set(_CONV, "resp_1", "up", created_by="bob@example.com")
    assert store.clear(_CONV, "resp_1", "alice@example.com") is True
    assert store.clear(_CONV, "resp_1", "alice@example.com") is False
    assert store.list_for_user(_CONV, "alice@example.com") == []
    assert len(store.list_for_user(_CONV, "bob@example.com")) == 1


def test_list_is_scoped_to_the_conversation(store: SqlAlchemyReactionStore) -> None:
    store.set(_CONV, "resp_1", "up", created_by="alice@example.com")
    assert store.list_for_user("9e92b5a0c0ee6db3f874f9c4a3f855a9", "alice@example.com") == []


def test_rejects_an_unknown_rating(store: SqlAlchemyReactionStore) -> None:
    with pytest.raises(ValueError):
        store.set(_CONV, "resp_1", "meh")


async def test_deleting_the_conversation_removes_its_reactions(db_uri: str) -> None:
    conv_store = SqlAlchemyConversationStore(db_uri)
    conversation = conv_store.create_conversation()
    store = SqlAlchemyReactionStore(db_uri)
    store.set(conversation.id, "resp_1", "up", created_by="alice@example.com")
    store.set(conversation.id, "resp_2", "down", created_by="alice@example.com")
    assert await conv_store.delete_conversation(conversation.id) is True
    assert store.list_for_user(conversation.id, "alice@example.com") == []
