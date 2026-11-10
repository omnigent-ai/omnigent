"""Tests for :class:`SqlAlchemyInboxItemStore` against a migrated SQLite database."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

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
        "media": [],
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


def test_replacing_an_expired_item_in_a_full_session_reports_the_cap(
    store: SqlAlchemyInboxItemStore,
) -> None:
    expired = store.create(_new(dedupe_key="v1:k", expires_at=1))
    for n in range(INBOX_ITEMS_PER_SESSION - 1):
        store.create(_new(dedupe_key=f"v1:{n}"))

    # The expired item permits a fresh one; the cap, not a duplicate, refuses it.
    with pytest.raises(InboxSessionCapError):
        store.create(_new(dedupe_key="v1:k"), retire_item_id=expired.id)


def test_a_lost_slot_is_retried_when_replacing_an_expired_item(
    store: SqlAlchemyInboxItemStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    expired = store.create(_new(dedupe_key="v1:k", expires_at=1))
    real_insert = store._insert
    calls = {"n": 0}

    def lose_the_first_slot(item: NewInboxItem, retire: str | None) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            # What a concurrent post taking the chosen slot looks like on PostgreSQL/MySQL.
            raise IntegrityError("INSERT", {}, Exception("duplicate session slot"))
        return real_insert(item, retire)

    monkeypatch.setattr(store, "_insert", lose_the_first_slot)

    fresh = store.create(_new(dedupe_key="v1:k"), retire_item_id=expired.id)

    assert fresh.id != expired.id
    assert calls["n"] == 2


def test_a_post_outlasts_successive_slot_conflicts(
    store: SqlAlchemyInboxItemStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent posts that keep taking the chosen slot do not fail a post while slots remain."""
    real_insert = store._insert
    calls = {"n": 0}

    def lose_to_a_competitor(item: NewInboxItem, retire: str | None) -> object:
        calls["n"] += 1
        if calls["n"] <= 4:
            # A competitor commits into the slot this attempt picked.
            real_insert(_new(dedupe_key=f"v1:competitor-{calls['n']}"), None)
            raise IntegrityError("INSERT", {}, Exception("duplicate session slot"))
        return real_insert(item, retire)

    monkeypatch.setattr(store, "_insert", lose_to_a_competitor)

    created = store.create(_new(dedupe_key="v1:target"))

    assert created.dedupe_key == "v1:target"
    assert calls["n"] == 5


def test_content_is_stored_in_envelopes(store: SqlAlchemyInboxItemStore) -> None:
    """The agent's text, media, and the owner's comment are plaintext envelopes in the database."""
    from sqlalchemy import text

    from omnigent.db.envelope import ENVELOPE_FORMAT

    created = store.create(_new(title="Private title"))
    store.update(created.id, "alice", feedback_text="Private comment", now=1)

    with store._engine.connect() as conn:
        row = conn.execute(text("SELECT content, media, feedback FROM inbox_items")).one()
    for column in row:
        assert bytes(column)[:2] == bytes((ENVELOPE_FORMAT, 0x00))
    read = store.get(created.id, "alice")
    assert read is not None
    assert (read.title, read.feedback_text) == ("Private title", "Private comment")


def test_oversized_text_is_rejected(store: SqlAlchemyInboxItemStore) -> None:
    incompressible = "".join(chr(0x4E00 + (n * 7919) % 20000) for n in range(9000))

    with pytest.raises(InboxItemTooLargeError):
        store.create(_new(body_md=incompressible))
    created = store.create(_new())
    with pytest.raises(InboxItemTooLargeError):
        store.update(created.id, "alice", feedback_text=incompressible, now=1)


def test_create_refreshes_expired_collision_after_stale_preflight(
    store: SqlAlchemyInboxItemStore,
) -> None:
    """A post whose duplicate check missed a newer expired same-key item replaces it."""
    winner = store.create(_new(expires_at=1))

    replacement = store.create(_new(source_session_id="b" * 32))

    assert replacement.dedupe_key == "v1:aaa"
    retired = store.get(winner.id, "alice")
    assert retired is not None
    assert retired.dedupe_key == f"v1:aaa#{winner.id}"


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


def test_mysql_deep_pages_seek_to_the_cursor(store: SqlAlchemyInboxItemStore) -> None:
    import re

    from sqlalchemy import event, insert

    from omnigent.db.db_models import SqlInboxItem

    if store._engine.dialect.name != "mysql":
        pytest.skip("MySQL execution-plan regression")
    rows = [
        {
            "workspace_id": 0,
            "id": f"{index:032x}",
            "user_id": "plan-owner",
            "source_session_id": f"{index // 10:032x}",
            "session_slot": index % 10,
            "agent_id": "a" * 32,
            "kind": 1,
            "content": {
                "title": "t",
                "body_md": "b",
                "links": [],
                "actions": [],
                "topic": "t",
                "primary_link": None,
            },
            "media": [],
            "dedupe_key": f"v1:{index}",
            "state": 1 + index % 2,
            "resolution": 0,
            "created_at": index // 10,
        }
        for index in range(10_000)
    ]
    with store._engine.begin() as connection:
        connection.execute(insert(SqlInboxItem), rows)
        connection.exec_driver_sql("ANALYZE TABLE inbox_items").all()
    plans: list[str] = []

    def explain(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT inbox_items."):
            plans.append(
                conn.exec_driver_sql("EXPLAIN ANALYZE " + statement, parameters).scalar_one()
            )

    event.listen(store._engine, "before_cursor_execute", explain)
    try:
        store.list_for_user("plan-owner", after=(100, f"{1005:032x}"), limit=50)
        store.list_for_user(
            "plan-owner", states=frozenset({"unread"}), after=(100, f"{1005:032x}"), limit=50
        )
    finally:
        event.remove(store._engine, "before_cursor_execute", explain)
    for plan in plans:
        # Rows each step actually read; the planner's estimates are not bounded.
        examined = [int(n) for n in re.findall(r"actual time=[\d.]+\.\.[\d.]+ rows=(\d+)", plan)]
        assert max(examined) <= 200, plan
