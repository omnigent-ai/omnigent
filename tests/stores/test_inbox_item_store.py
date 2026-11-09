"""Tests for :class:`SqlAlchemyInboxItemStore` against a migrated SQLite database."""

from __future__ import annotations

import dataclasses

import pytest

from omnigent.stores.inbox_item_store import DuplicateInboxItemError, NewInboxItem
from omnigent.stores.inbox_item_store.sqlalchemy_store import SqlAlchemyInboxItemStore

_SESSION = "4e92b5a0c0ee6db3f874f9c4a3f855a5"


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyInboxItemStore:
    return SqlAlchemyInboxItemStore(db_uri)


def _new(dedupe_key: str = "v1:aaa", user_id: str = "alice", **overrides: object) -> NewInboxItem:
    fields = {
        "user_id": user_id,
        "source_session_id": _SESSION,
        "agent_id": "a" * 32,
        "prompt_version": "b" * 64,
        "kind": "followup",
        "title": "Ramp up the flag",
        "body_md": "The PR merged yesterday.",
        "links": [{"type": "pr", "ref": "https://github.com/acme/app/pull/7"}],
        "suggested_prompt": "Ramp up the flag",
        "primary_link": "github.com/acme/app/pull/7",
        "topic": "rampup-flag",
        "dedupe_key": dedupe_key,
    }
    fields.update(overrides)
    return NewInboxItem(**fields)  # type: ignore[arg-type]


def test_create_and_read_back(store: SqlAlchemyInboxItemStore) -> None:
    created = store.create(_new())

    assert created.state == "unread"
    assert created.feedback == "none"
    assert created.links == [{"type": "pr", "ref": "https://github.com/acme/app/pull/7"}]
    assert store.get(created.id, "alice") == created
    assert store.get_by_dedupe_key("alice", "v1:aaa") == created


def test_items_are_scoped_to_their_owner(store: SqlAlchemyInboxItemStore) -> None:
    created = store.create(_new())

    assert store.get(created.id, "bob") is None
    assert store.get_by_dedupe_key("bob", "v1:aaa") is None
    assert store.update(created.id, "bob", state="read", now=10) is None
    assert store.list_for_user("bob") == ([], None)


def test_same_key_for_another_owner_is_allowed(store: SqlAlchemyInboxItemStore) -> None:
    store.create(_new())
    store.create(_new(user_id="bob"))


def test_duplicate_key_raises(store: SqlAlchemyInboxItemStore) -> None:
    store.create(_new())

    with pytest.raises(DuplicateInboxItemError):
        store.create(_new())


def test_retiring_an_expired_item_frees_its_key(store: SqlAlchemyInboxItemStore) -> None:
    expired = store.create(_new(expires_at=5))

    fresh = store.create(_new(), retire_item_id=expired.id)

    retired = store.get(expired.id, "alice")
    assert retired is not None
    assert retired.dedupe_key == f"v1:aaa#{expired.id}"
    assert store.get_by_dedupe_key("alice", "v1:aaa") == fresh


def test_list_pages_newest_first_and_filters(store: SqlAlchemyInboxItemStore) -> None:
    items = [store.create(_new(dedupe_key=f"v1:{n}")) for n in range(3)]
    store.update(items[0].id, "alice", state="dismissed", now=10)

    first, cursor = store.list_for_user("alice", limit=2)
    second, end = store.list_for_user("alice", after=cursor, limit=2)

    listed = [item.id for item in first + second]
    expected = [
        item.id for item in sorted(items, key=lambda i: (i.created_at, i.id), reverse=True)
    ]
    assert listed == expected
    assert end is None
    unread, _ = store.list_for_user("alice", states=frozenset({"unread"}))
    assert {item.id for item in unread} == {items[1].id, items[2].id}
    assert store.list_for_user("alice", created_after=items[0].created_at + 3600) == ([], None)


def test_counts(store: SqlAlchemyInboxItemStore) -> None:
    first = store.create(_new(dedupe_key="v1:1"))
    store.create(_new(dedupe_key="v1:2", source_session_id="f" * 32))
    store.update(first.id, "alice", state="read", now=10)

    assert store.count_unread("alice") == 1
    assert store.count_for_session("alice", _SESSION) == 1


def test_update_stamps_read_and_acted_times(store: SqlAlchemyInboxItemStore) -> None:
    created = store.create(_new())

    read = store.update(created.id, "alice", state="read", now=100)
    assert read is not None and read.read_at == 100 and read.acted_at is None

    rated = store.update(created.id, "alice", feedback="useful", feedback_text="thanks", now=200)
    assert rated is not None
    assert (rated.feedback, rated.feedback_text, rated.acted_at) == ("useful", "thanks", 200)

    dismissed = store.update(created.id, "alice", state="dismissed", now=300)
    assert dismissed is not None
    assert (dismissed.state, dismissed.read_at, dismissed.acted_at) == ("dismissed", 100, 300)
    assert dataclasses.replace(dismissed) == store.get(created.id, "alice")
