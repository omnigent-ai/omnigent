"""Tests for :class:`SqlAlchemyInboxItemStore` against a migrated SQLite database."""

from __future__ import annotations

import pytest

from omnigent.db.db_models import INBOX_ITEMS_PER_SESSION
from omnigent.stores.inbox_item_store import (
    DuplicateInboxItemError,
    InboxItemTooLargeError,
    InboxSessionCapError,
    NewInboxItem,
)
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
        "actions": [{"label": "Ramp it up", "prompt": "Ramp up the flag"}],
        "chart": None,
        "primary_link": "github.com/acme/app/pull/7",
        "topic": "rampup-flag",
        "dedupe_key": dedupe_key,
    }
    fields.update(overrides)
    return NewInboxItem(**fields)  # type: ignore[arg-type]


def test_create_and_read_back(store: SqlAlchemyInboxItemStore) -> None:
    created = store.create(_new())

    assert (created.state, created.resolution, created.resolution_action) == (
        "unread",
        "none",
        None,
    )
    assert created.actions == [{"label": "Ramp it up", "prompt": "Ramp up the flag"}]
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


def test_session_cap_is_enforced_in_the_insert(store: SqlAlchemyInboxItemStore) -> None:
    for n in range(INBOX_ITEMS_PER_SESSION):
        store.create(_new(dedupe_key=f"v1:{n}"))

    with pytest.raises(InboxSessionCapError):
        store.create(_new(dedupe_key="v1:over"))
    # A full session reports a repeat of one of its items as a duplicate, not the cap.
    with pytest.raises(DuplicateInboxItemError):
        store.create(_new(dedupe_key="v1:0"))
    store.create(_new(dedupe_key="v1:other-session", source_session_id="f" * 32))


def test_oversized_text_is_rejected(store: SqlAlchemyInboxItemStore) -> None:
    incompressible = "".join(chr(0x4E00 + (n * 7919) % 20000) for n in range(9000))

    with pytest.raises(InboxItemTooLargeError):
        store.create(_new(body_md=incompressible))
    created = store.create(_new())
    with pytest.raises(InboxItemTooLargeError):
        store.update(created.id, "alice", feedback_text=incompressible, now=1)


def test_retiring_an_expired_item_frees_its_key(store: SqlAlchemyInboxItemStore) -> None:
    expired = store.create(_new(expires_at=5))

    fresh = store.create(_new(), retire_item_id=expired.id)

    retired = store.get(expired.id, "alice")
    assert retired is not None
    assert retired.dedupe_key == f"v1:aaa#{expired.id}"
    assert store.get_by_dedupe_key("alice", "v1:aaa") == fresh


def test_list_pages_newest_first_and_filters(store: SqlAlchemyInboxItemStore) -> None:
    items = [store.create(_new(dedupe_key=f"v1:{n}")) for n in range(4)]
    store.update(items[0].id, "alice", state="dismissed", resolution="not_relevant", now=10)
    store.update(items[1].id, "alice", state="read", now=10)
    newest_first = sorted(items, key=lambda i: (i.created_at, i.id), reverse=True)

    first, cursor = store.list_for_user("alice", limit=3)
    second, end = store.list_for_user("alice", after=cursor, limit=3)
    assert [item.id for item in first + second] == [item.id for item in newest_first]
    assert end is None

    open_first, open_cursor = store.list_for_user(
        "alice", states=frozenset({"unread", "read"}), limit=2
    )
    open_rest, open_end = store.list_for_user(
        "alice", states=frozenset({"unread", "read"}), after=open_cursor, limit=2
    )
    expected_open = [item.id for item in newest_first if item.id != items[0].id]
    assert [item.id for item in open_first + open_rest] == expected_open
    assert open_end is None
    assert store.list_for_user("alice", created_after=items[0].created_at + 3600) == ([], None)


def test_unread_count_is_bounded(store: SqlAlchemyInboxItemStore) -> None:
    for n in range(3):
        store.create(_new(dedupe_key=f"v1:{n}"))

    assert store.count_unread("alice", limit=100) == 3
    assert store.count_unread("alice", limit=2) == 2


def test_update_stamps_read_and_acted_times(store: SqlAlchemyInboxItemStore) -> None:
    created = store.create(_new())

    read = store.update(created.id, "alice", state="read", now=100)
    assert read is not None and read.read_at == 100 and read.acted_at is None

    acted = store.update(
        created.id,
        "alice",
        state="done",
        resolution="action_taken",
        resolution_action=0,
        feedback_text="thanks",
        now=200,
    )
    assert acted is not None
    assert (acted.state, acted.resolution, acted.resolution_action) == ("done", "action_taken", 0)
    assert (acted.read_at, acted.acted_at, acted.feedback_text) == (100, 200, "thanks")
    assert store.get(created.id, "alice") == acted
