"""Tests for :class:`SqlAlchemyPreferenceStore`.

Exercises every public method against a real SQLite database (migrations
applied via :func:`get_or_create_engine`), following the same pattern as
:mod:`tests.stores.test_permission_store`.

The ``db_uri`` fixture in the root conftest creates a fresh per-test SQLite
file and tears it down automatically.
"""

from __future__ import annotations

import pytest

from omnigent.entities import UserPreference
from omnigent.stores.preference_store.sqlalchemy_store import SqlAlchemyPreferenceStore


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyPreferenceStore:
    """A fresh :class:`SqlAlchemyPreferenceStore` backed by the test SQLite DB.

    :param db_uri: Per-test SQLite URI from the root conftest fixture.
    :returns: A ready-to-use :class:`SqlAlchemyPreferenceStore` instance.
    """
    return SqlAlchemyPreferenceStore(db_uri)


def test_get_unknown_user_returns_none(store: SqlAlchemyPreferenceStore) -> None:
    """A user with no rows has no preference for any key."""
    assert store.get("nobody@test.com", "pinned_conversation_ids") is None


def test_get_all_unknown_user_is_empty(store: SqlAlchemyPreferenceStore) -> None:
    """``get_all`` on an unknown user returns an empty mapping, not an error."""
    assert store.get_all("nobody@test.com") == {}


def test_set_then_get_round_trips(store: SqlAlchemyPreferenceStore) -> None:
    """A value written by ``set`` comes back verbatim from ``get``."""
    pref = store.set("alice@test.com", "pinned_conversation_ids", '["a","b"]')
    assert isinstance(pref, UserPreference)
    assert (pref.user_id, pref.key, pref.value) == (
        "alice@test.com",
        "pinned_conversation_ids",
        '["a","b"]',
    )
    assert store.get("alice@test.com", "pinned_conversation_ids") == pref


def test_set_upserts_existing_key(store: SqlAlchemyPreferenceStore) -> None:
    """Writing the same key twice overwrites rather than duplicating."""
    store.set("alice@test.com", "pinned_conversation_ids", '["a"]')
    store.set("alice@test.com", "pinned_conversation_ids", '["a","b"]')
    got = store.get("alice@test.com", "pinned_conversation_ids")
    assert got is not None
    assert got.value == '["a","b"]'
    assert store.get_all("alice@test.com") == {"pinned_conversation_ids": '["a","b"]'}


def test_get_all_returns_every_key_for_the_user(store: SqlAlchemyPreferenceStore) -> None:
    """``get_all`` collects all of one user's keys into a single mapping."""
    store.set("alice@test.com", "pinned_conversation_ids", '["a"]')
    store.set("alice@test.com", "collapsed_sidebar_sections", '["Chats"]')
    assert store.get_all("alice@test.com") == {
        "pinned_conversation_ids": '["a"]',
        "collapsed_sidebar_sections": '["Chats"]',
    }


def test_preferences_are_isolated_per_user(store: SqlAlchemyPreferenceStore) -> None:
    """One user's write is invisible to another user with the same key."""
    store.set("alice@test.com", "pinned_conversation_ids", '["a"]')
    store.set("bob@test.com", "pinned_conversation_ids", '["b"]')
    assert store.get_all("alice@test.com") == {"pinned_conversation_ids": '["a"]'}
    assert store.get_all("bob@test.com") == {"pinned_conversation_ids": '["b"]'}


def test_delete_removes_only_that_key(store: SqlAlchemyPreferenceStore) -> None:
    """``delete`` drops one key and reports whether a row was removed."""
    store.set("alice@test.com", "pinned_conversation_ids", '["a"]')
    store.set("alice@test.com", "collapsed_sidebar_sections", '["Chats"]')
    assert store.delete("alice@test.com", "pinned_conversation_ids") is True
    assert store.get("alice@test.com", "pinned_conversation_ids") is None
    assert store.get_all("alice@test.com") == {"collapsed_sidebar_sections": '["Chats"]'}


def test_delete_missing_key_is_a_no_op(store: SqlAlchemyPreferenceStore) -> None:
    """Deleting a key that was never set returns ``False`` without raising."""
    assert store.delete("alice@test.com", "pinned_conversation_ids") is False


def test_set_stamps_updated_at(store: SqlAlchemyPreferenceStore) -> None:
    """Each write records when it happened so stale rows are prunable."""
    pref = store.set("alice@test.com", "pinned_conversation_ids", "[]")
    assert pref.updated_at > 0
