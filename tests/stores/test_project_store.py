"""Tests for :class:`SqlAlchemyProjectStore`.

Exercises ``create``, ``get``, ``list``, ``update`` and ``delete`` against a
real SQLite database, covering owner scoping and per-owner name uniqueness.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore


# projects.id is a Uuid16 column (16 raw bytes) read back as bare 32-char hex.
# ``_uid`` maps a readable seed to a deterministic bare-hex UUID so tests stay
# legible while the store still round-trips real UUIDs.
def _uid(seed: str) -> str:
    """Deterministic bare 32-char hex UUID string from a short readable seed."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyProjectStore:
    """A fresh :class:`SqlAlchemyProjectStore` backed by the test SQLite DB.

    :param db_uri: Per-test SQLite URI from the root conftest fixture.
    :returns: A ready-to-use :class:`SqlAlchemyProjectStore` instance.
    """
    return SqlAlchemyProjectStore(db_uri)


# ── create / get ──────────────────────────────────────────────────────────


def test_create_returns_project(store: SqlAlchemyProjectStore) -> None:
    """``create`` echoes the fields back and stamps ``created_at``."""
    project = store.create(_uid("p1"), "My Project", "alice@example.com")
    assert project.id == _uid("p1")
    assert project.name == "My Project"
    assert project.user_id == "alice@example.com"
    assert project.created_at > 0
    assert project.updated_at is None


def test_get_returns_created_project(store: SqlAlchemyProjectStore) -> None:
    """``get`` reads back a created project for its owner."""
    store.create(_uid("p1"), "My Project", "alice@example.com")
    got = store.get(_uid("p1"), user_id="alice@example.com")
    assert got is not None
    assert got.name == "My Project"


def test_get_missing_returns_none(store: SqlAlchemyProjectStore) -> None:
    """``get`` returns ``None`` for an unknown id."""
    assert store.get(_uid("nope"), user_id="alice@example.com") is None


def test_get_scoped_to_owner(store: SqlAlchemyProjectStore) -> None:
    """A project owned by someone else reads back as not found."""
    store.create(_uid("p1"), "Alice Project", "alice@example.com")
    assert store.get(_uid("p1"), user_id="bob@example.com") is None


# ── list ──────────────────────────────────────────────────────────────────


def test_list_orders_by_created_at_then_id(store: SqlAlchemyProjectStore) -> None:
    """``list`` orders by ``created_at ASC, id ASC``.

    Both rows are created in the same second here, so the ``id`` tiebreaker
    decides the order — assert against that rather than insertion order.
    """
    store.create(_uid("p1"), "First", "alice@example.com")
    store.create(_uid("p2"), "Second", "alice@example.com")
    listed = store.list(user_id="alice@example.com")
    assert {p.name for p in listed} == {"First", "Second"}
    # Whatever the tie order, it is ascending by (created_at, id).
    keys = [(p.created_at, p.id) for p in listed]
    assert keys == sorted(keys)


def test_list_scoped_to_owner(store: SqlAlchemyProjectStore) -> None:
    """``list`` only returns the requesting owner's projects."""
    store.create(_uid("p1"), "Alice Project", "alice@example.com")
    store.create(_uid("p2"), "Bob Project", "bob@example.com")
    alice = store.list(user_id="alice@example.com")
    assert [p.name for p in alice] == ["Alice Project"]


def test_list_empty(store: SqlAlchemyProjectStore) -> None:
    """``list`` returns an empty list when the owner has no projects."""
    assert store.list(user_id="alice@example.com") == []


# ── single-user (None owner) vs multi-user isolation ────────────────────────


def test_none_owner_and_named_owner_are_isolated(store: SqlAlchemyProjectStore) -> None:
    """The single-user ``None`` owner is a distinct scope from any named user.

    A project created in single-user mode (``user_id=None``) must not be
    visible to a named multi-user identity, and vice versa — the same DB can
    hold both without cross-leaking.
    """
    store.create(_uid("solo"), "Solo Project", None)
    store.create(_uid("alice"), "Alice Project", "alice@example.com")

    # Each scope lists only its own.
    assert [p.name for p in store.list(user_id=None)] == ["Solo Project"]
    assert [p.name for p in store.list(user_id="alice@example.com")] == ["Alice Project"]


def test_named_owner_cannot_get_none_owner_project(store: SqlAlchemyProjectStore) -> None:
    """A ``None``-owner project is not found for a named user (and vice versa)."""
    store.create(_uid("solo"), "Solo", None)
    assert store.get(_uid("solo"), user_id="alice@example.com") is None
    assert store.get(_uid("solo"), user_id=None) is not None


def test_named_owner_cannot_mutate_none_owner_project(store: SqlAlchemyProjectStore) -> None:
    """update / delete on a ``None``-owner project are no-ops for a named user."""
    store.create(_uid("solo"), "Solo", None)
    updated = store.update(_uid("solo"), user_id="alice@example.com", name="Hacked")
    assert updated is None
    deleted = store.delete(_uid("solo"), user_id="alice@example.com")
    assert deleted is False
    # Untouched for the real (None) owner.
    assert store.get(_uid("solo"), user_id=None).name == "Solo"


# ── name uniqueness ────────────────────────────────────────────────────────


def test_create_rejects_duplicate_name_per_owner(store: SqlAlchemyProjectStore) -> None:
    """Two projects with the same name for one owner are rejected."""
    store.create(_uid("p1"), "Dup", "alice@example.com")
    with pytest.raises(OmnigentError) as exc:
        store.create(_uid("p2"), "Dup", "alice@example.com")
    assert exc.value.code == ErrorCode.ALREADY_EXISTS


def test_same_name_allowed_across_owners(store: SqlAlchemyProjectStore) -> None:
    """Two different owners may each have a project with the same name."""
    a = store.create(_uid("p1"), "Shared Name", "alice@example.com")
    b = store.create(_uid("p2"), "Shared Name", "bob@example.com")
    assert a.name == b.name == "Shared Name"


def test_duplicate_name_rejected_for_null_owner(store: SqlAlchemyProjectStore) -> None:
    """Single-user mode (NULL owner) enforces name uniqueness in the store.

    ``_name_taken`` is the sole guard for every owner, NULL included — no unique
    index backs it.
    """
    store.create(_uid("p1"), "Solo", None)
    with pytest.raises(OmnigentError) as exc:
        store.create(_uid("p2"), "Solo", None)
    assert exc.value.code == ErrorCode.ALREADY_EXISTS


def test_duplicate_name_lands_when_precheck_is_bypassed(
    store: SqlAlchemyProjectStore,
) -> None:
    """A duplicate name is accepted once ``_name_taken`` is bypassed.

    No unique index covers (workspace_id, user_id, name), so the pre-check is
    the only guard and a concurrent create racing past it lands. Pins that
    accepted cost: both rows exist, each addressable by its own id.

    Monkeypatching ``_name_taken`` to always-miss simulates two concurrent
    creates both passing the check.
    """
    store.create(_uid("p1"), "Dup", "alice@example.com")
    store._name_taken = lambda *a, **k: False  # type: ignore[method-assign]
    store.create(_uid("p2"), "Dup", "alice@example.com")

    assert [p.name for p in store.list(user_id="alice@example.com")] == ["Dup", "Dup"]
    assert store.get(_uid("p1"), user_id="alice@example.com") is not None
    assert store.get(_uid("p2"), user_id="alice@example.com") is not None


def test_primary_key_collision_is_not_masked(store: SqlAlchemyProjectStore) -> None:
    """Reusing an id surfaces as ``IntegrityError``, not ``ALREADY_EXISTS``.

    The PK is the only remaining constraint on this table, and the store must
    not dress its violation up as a name collision.
    """
    store.create(_uid("p1"), "Original", "alice@example.com")
    store._name_taken = lambda *a, **k: False  # type: ignore[method-assign]
    with pytest.raises(IntegrityError):
        store.create(_uid("p1"), "Different name", "alice@example.com")


# ── config (default session settings) ──────────────────────────────────────


def test_create_defaults_to_empty_config(store: SqlAlchemyProjectStore) -> None:
    """A project created without config reads back an empty dict (SQL NULL)."""
    project = store.create(_uid("p1"), "No Config", "alice@example.com")
    assert project.config == {}
    assert store.get(_uid("p1"), user_id="alice@example.com").config == {}


def test_create_persists_config(store: SqlAlchemyProjectStore) -> None:
    """A config passed to ``create`` round-trips through ``get``/``list``."""
    cfg = {"host_id": "host_abc", "workspace": "/work/repo", "model": "claude-opus-4-8"}
    store.create(_uid("p1"), "Configured", "alice@example.com", cfg)
    assert store.get(_uid("p1"), user_id="alice@example.com").config == cfg
    assert store.list(user_id="alice@example.com")[0].config == cfg


def test_update_replaces_config_and_stamps_updated_at(store: SqlAlchemyProjectStore) -> None:
    """Passing a new ``config`` replaces the stored one and stamps updated_at."""
    store.create(_uid("p1"), "P", "alice@example.com", {"host_id": "old"})
    updated = store.update(
        _uid("p1"), user_id="alice@example.com", config={"host_id": "new", "model": "m"}
    )
    assert updated is not None
    assert updated.config == {"host_id": "new", "model": "m"}
    assert updated.updated_at is not None


def test_update_config_none_leaves_it_unchanged(store: SqlAlchemyProjectStore) -> None:
    """``config=None`` (the default) leaves the stored config untouched."""
    store.create(_uid("p1"), "P", "alice@example.com", {"host_id": "keep"})
    # Rename only — config omitted — must not wipe the stored defaults.
    updated = store.update(_uid("p1"), user_id="alice@example.com", name="Renamed")
    assert updated is not None
    assert updated.config == {"host_id": "keep"}


def test_update_empty_config_clears_defaults(store: SqlAlchemyProjectStore) -> None:
    """An explicit ``config={}`` clears the stored defaults (distinct from None)."""
    store.create(_uid("p1"), "P", "alice@example.com", {"host_id": "drop"})
    updated = store.update(_uid("p1"), user_id="alice@example.com", config={})
    assert updated is not None
    assert updated.config == {}
    assert updated.updated_at is not None


def test_update_same_config_is_noop(store: SqlAlchemyProjectStore) -> None:
    """Re-setting the identical config changes nothing, leaving updated_at None."""
    store.create(_uid("p1"), "P", "alice@example.com", {"host_id": "x"})
    updated = store.update(_uid("p1"), user_id="alice@example.com", config={"host_id": "x"})
    assert updated is not None
    assert updated.updated_at is None


def test_create_rejects_oversized_config(store: SqlAlchemyProjectStore) -> None:
    """A config whose serialized form exceeds the cap is rejected on create.

    The value is persisted verbatim and reflected back on every read, so an
    unbounded blob is capped as defense-in-depth (INVALID_INPUT → HTTP 400).
    """
    huge = {"blob": "x" * (64 * 1024 + 1)}
    with pytest.raises(OmnigentError) as exc:
        store.create(_uid("p1"), "Big", "alice@example.com", huge)
    assert exc.value.code == ErrorCode.INVALID_INPUT


def test_update_rejects_oversized_config(store: SqlAlchemyProjectStore) -> None:
    """An oversized config is rejected on update, leaving the row unchanged."""
    store.create(_uid("p1"), "P", "alice@example.com", {"host_id": "keep"})
    huge = {"blob": "x" * (64 * 1024 + 1)}
    with pytest.raises(OmnigentError) as exc:
        store.update(_uid("p1"), user_id="alice@example.com", config=huge)
    assert exc.value.code == ErrorCode.INVALID_INPUT
    # The prior config is untouched (the encode guard fires before any write).
    assert store.get(_uid("p1"), user_id="alice@example.com").config == {"host_id": "keep"}


def test_decode_coerces_non_object_blob_to_empty(store: SqlAlchemyProjectStore) -> None:
    """A stored non-object blob (manual edit / future writer) reads back as {}.

    The encode path only ever writes JSON objects, but ``_decode_config`` must
    stay defensive so callers can always treat config as a mapping.
    """
    from sqlalchemy import text as sa_text

    store.create(_uid("p1"), "P", "alice@example.com")
    # Bypass the store to plant a scalar JSON blob directly.
    with store._engine.begin() as conn:
        conn.execute(
            sa_text("UPDATE projects SET config = :c WHERE id = :i"),
            {"c": '"just a string"', "i": _uid("p1")},
        )
    got = store.get(_uid("p1"), user_id="alice@example.com")
    assert got is not None
    assert got.config == {}


# ── update ─────────────────────────────────────────────────────────────────


def test_update_renames_and_stamps_updated_at(store: SqlAlchemyProjectStore) -> None:
    """Renaming changes ``name`` and sets ``updated_at``."""
    store.create(_uid("p1"), "Old", "alice@example.com")
    updated = store.update(_uid("p1"), user_id="alice@example.com", name="New")
    assert updated is not None
    assert updated.name == "New"
    assert updated.updated_at is not None


def test_update_noop_leaves_updated_at_none(store: SqlAlchemyProjectStore) -> None:
    """An update that changes nothing leaves ``updated_at`` untouched."""
    store.create(_uid("p1"), "Same", "alice@example.com")
    updated = store.update(_uid("p1"), user_id="alice@example.com", name="Same")
    assert updated is not None
    assert updated.updated_at is None


def test_update_missing_returns_none(store: SqlAlchemyProjectStore) -> None:
    """Updating an unknown project returns ``None``."""
    updated = store.update(_uid("nope"), user_id="alice@example.com", name="X")
    assert updated is None


def test_update_scoped_to_owner(store: SqlAlchemyProjectStore) -> None:
    """A non-owner cannot rename another user's project."""
    store.create(_uid("p1"), "Alice Project", "alice@example.com")
    updated = store.update(_uid("p1"), user_id="bob@example.com", name="Hacked")
    assert updated is None
    # Unchanged for the real owner.
    assert store.get(_uid("p1"), user_id="alice@example.com").name == "Alice Project"


def test_update_rejects_duplicate_name(store: SqlAlchemyProjectStore) -> None:
    """Renaming onto another of the owner's project names is rejected."""
    store.create(_uid("p1"), "First", "alice@example.com")
    store.create(_uid("p2"), "Second", "alice@example.com")
    with pytest.raises(OmnigentError) as exc:
        store.update(_uid("p2"), user_id="alice@example.com", name="First")
    assert exc.value.code == ErrorCode.ALREADY_EXISTS


# ── delete ─────────────────────────────────────────────────────────────────


def test_delete_removes_project(store: SqlAlchemyProjectStore) -> None:
    """``delete`` removes the project and is idempotent."""
    store.create(_uid("p1"), "Doomed", "alice@example.com")
    deleted = store.delete(_uid("p1"), user_id="alice@example.com")
    assert deleted is True
    assert store.get(_uid("p1"), user_id="alice@example.com") is None
    deleted_again = store.delete(_uid("p1"), user_id="alice@example.com")
    assert deleted_again is False


def test_delete_scoped_to_owner(store: SqlAlchemyProjectStore) -> None:
    """A non-owner cannot delete another user's project."""
    store.create(_uid("p1"), "Alice Project", "alice@example.com")
    deleted = store.delete(_uid("p1"), user_id="bob@example.com")
    assert deleted is False
    assert store.get(_uid("p1"), user_id="alice@example.com") is not None


def test_order_owner_isolation_and_last_write_wins(store: SqlAlchemyProjectStore) -> None:
    """Local and authenticated owners have independent orders and resets."""
    a = store.create(_uid("order-a"), "A", None)
    b = store.create(_uid("order-b"), "B", None)
    other = store.create(_uid("order-other"), "Other", "alice")
    assert store.get_order(user_id=None) is None
    store.save_order([b.id, a.id], user_id=None)
    store.save_order([other.id], user_id="alice")
    assert store.get_order(user_id=None) == [b.id, a.id]
    assert store.get_order(user_id="alice") == [other.id]
    store.save_order([a.id, b.id], user_id=None)
    assert store.get_order(user_id=None) == [a.id, b.id]
    for invalid in ([a.id, a.id], [other.id], ["missing"]):
        with pytest.raises(OmnigentError):
            store.save_order(invalid, user_id=None)
        assert store.get_order(user_id=None) == [a.id, b.id]
    store.save_order([], user_id=None)
    assert store.get_order(user_id=None) == []
    store.save_order(None, user_id=None)
    assert store.get_order(user_id=None) is None
    assert store.get_order(user_id="alice") == [other.id]


def test_order_workspace_isolation(store: SqlAlchemyProjectStore) -> None:
    """An owner's saved IDs and write validation stay inside their workspace."""
    from omnigent.db.db_models import workspace_scope

    with workspace_scope(101):
        project = store.create(_uid("scoped-order"), "A", "alice")
        store.save_order([project.id], user_id="alice")
    with workspace_scope(102):
        assert store.get_order(user_id="alice") is None
        with pytest.raises(OmnigentError):
            store.save_order([project.id], user_id="alice")
        store.save_order([], user_id="alice")
    with workspace_scope(101):
        assert store.get_order(user_id="alice") == [project.id]


def test_order_local_alias_does_not_change_project_ownership(
    store: SqlAlchemyProjectStore,
) -> None:
    """None and local share a preference, but keep their original project scopes."""
    project = store.create(_uid("no-auth"), "Local project", None)
    assert store.get_order(user_id="local") is None
    store.save_order([project.id], user_id=None)
    assert store.get_order(user_id="local") == [project.id]
    with pytest.raises(OmnigentError):
        store.save_order([project.id], user_id="local")
    assert store.get(project.id, user_id=None) is not None
    assert store.get(project.id, user_id="local") is None
    store.save_order(None, user_id="local")
    assert store.get_order(user_id=None) is None


@pytest.mark.parametrize("user_id", [None, "alice"])
def test_order_preserves_authentication_fields(
    store: SqlAlchemyProjectStore, user_id: str | None
) -> None:
    """Saving/resetting preferences leaves an existing account's identity intact."""
    from sqlalchemy.orm import Session

    from omnigent.db.db_models import SqlUser

    preference_user_id = "local" if user_id is None else user_id
    with Session(store._engine) as session:
        session.merge(
            SqlUser(
                workspace_id=0,
                id=preference_user_id,
                is_admin=True,
                password_hash="test-hash",
                created_at=123,
                last_login_at=456,
            )
        )
        session.commit()
    project = store.create(_uid("auth-order"), "A", user_id)
    for order in ([project.id], [], None):
        store.save_order(order, user_id=user_id)
        with Session(store._engine) as session:
            user = session.get(SqlUser, (0, preference_user_id))
            assert user is not None
            assert user.is_admin is True
            assert user.password_hash == "test-hash"
            assert user.created_at == 123
            assert user.last_login_at == 456


def test_order_preferences_do_not_create_accounts(
    store: SqlAlchemyProjectStore,
) -> None:
    """Project preferences can be saved without creating authentication rows."""
    from sqlalchemy.orm import Session

    from omnigent.db.db_models import SqlPreference, SqlUser

    with Session(store._engine) as session:
        user = session.get(SqlUser, (0, "local"))
        if user is not None:
            session.delete(user)
            session.commit()
    assert store.get_order(user_id=None) is None
    store.save_order(None, user_id=None)
    with Session(store._engine) as session:
        assert session.get(SqlUser, (0, "local")) is None
        assert session.get(SqlPreference, (0, "local", "project_order")) is None
    store.save_order([], user_id=None)
    with Session(store._engine) as session:
        assert session.get(SqlUser, (0, "local")) is None
        assert session.get(SqlPreference, (0, "local", "project_order")) is not None


def test_order_updates_preserve_other_preferences(store: SqlAlchemyProjectStore) -> None:
    from sqlalchemy.orm import Session

    from omnigent.db.db_models import SqlPreference

    with Session(store._engine) as session:
        session.add(SqlPreference(workspace_id=0, user_id="local", key="theme", value='"dark"'))
        session.commit()
    assert store.get_order(user_id=None) is None
    for order in (None, [], None, []):
        store.save_order(order, user_id=None)
        with Session(store._engine) as session:
            preference = session.get(SqlPreference, (0, "local", "theme"))
            assert preference is not None
            assert preference.value == '"dark"'


def test_alphabetical_mode_retains_manual_order(store: SqlAlchemyProjectStore) -> None:
    a = store.create(_uid("remember-a"), "A", None)
    b = store.create(_uid("remember-b"), "B", None)
    store.save_order([b.id, a.id], user_id=None)
    for _ in range(2):
        preference = store.save_order(None, user_id=None)
        assert preference == {"sort_mode": "alphabetical", "ordered_project_ids": [b.id, a.id]}
        assert store.get_order(user_id=None) is None
        reopened = SqlAlchemyProjectStore(store.storage_location)
        assert reopened.get_order_preference(user_id=None) == preference
        reopened.save_order(preference["ordered_project_ids"], user_id=None)
        assert reopened.get_order(user_id=None) == [b.id, a.id]


def test_original_array_format_preserves_manual_order(store: SqlAlchemyProjectStore) -> None:
    import json

    from sqlalchemy import update
    from sqlalchemy.orm import Session

    from omnigent.db.db_models import SqlPreference

    project = store.create(_uid("original-format"), "A", None)
    store.save_order([], user_id=None)
    with Session(store._engine) as session:
        session.execute(
            update(SqlPreference)
            .where(
                SqlPreference.workspace_id == 0,
                SqlPreference.user_id == "local",
                SqlPreference.key == "project_order",
            )
            .values(value=json.dumps([project.id]))
        )
        session.commit()
    assert store.get_order(user_id=None) == [project.id]
    assert store.save_order(None, user_id=None) == {
        "sort_mode": "alphabetical",
        "ordered_project_ids": [project.id],
    }


def test_concurrent_order_saves_and_mode_changes_retain_manual_ids(
    store: SqlAlchemyProjectStore,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    a = store.create(_uid("concurrent-order-a"), "A", "new-owner")
    b = store.create(_uid("concurrent-order-b"), "B", "new-owner")
    orders = [[a.id, b.id], [b.id, a.id]]
    barrier = Barrier(2)

    def save(ids: list[str]) -> None:
        barrier.wait(timeout=5)
        for _ in range(3):
            store.save_order(ids, user_id="new-owner")
            store.save_order(None, user_id="new-owner")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(save, orders))
    preference = store.get_order_preference(user_id="new-owner")
    assert preference["sort_mode"] == "alphabetical"
    assert preference["ordered_project_ids"] in orders


def test_order_enforces_compressed_blob_capacity(store: SqlAlchemyProjectStore) -> None:
    """Limit stored bytes while retaining orders whose larger JSON compresses to fit."""
    import json

    from sqlalchemy import func, insert, select
    from sqlalchemy.orm import Session

    from omnigent.db.db_models import SqlPreference, SqlProject
    from omnigent.server.schemas import ProjectOrderRequest

    ids = [_uid(f"large-order-{i}") for i in range(10000)]
    with Session(store._engine) as session:
        session.execute(
            insert(SqlProject),
            [
                {"workspace_id": 0, "id": id, "name": id, "user_id": "large", "created_at": 0}
                for id in ids
            ],
        )
        session.commit()
    request = ProjectOrderRequest(ordered_project_ids=ids)
    with pytest.raises(OmnigentError, match="too large") as exc:
        store.save_order(request.ordered_project_ids, user_id="large")
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert store.get_order(user_id="large") is None

    fitting_ids = ids[:3000]
    assert len(json.dumps(fitting_ids).encode()) > 65535
    store.save_order(fitting_ids, user_id="large")
    assert store.get_order(user_id="large") == fitting_ids
    with Session(store._engine) as session:
        size = session.scalar(
            select(func.length(SqlPreference.value)).where(
                SqlPreference.workspace_id == 0,
                SqlPreference.user_id == "large",
                SqlPreference.key == "project_order",
            )
        )
    assert size is not None and size <= 65535

    with pytest.raises(OmnigentError, match="too large") as exc:
        store.save_order(request.ordered_project_ids, user_id="large")
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert store.get_order(user_id="large") == fitting_ids
    assert store.save_order(None, user_id="large") == {
        "sort_mode": "alphabetical",
        "ordered_project_ids": fitting_ids,
    }


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"null",
        b"\xff",
        b"\x00",
        b"\x00\x01invalid",
        b"\x00\x03{}",
        b'{"sort_mode":"invalid","ordered_project_ids":[]}',
        b'{"sort_mode":"manual","ordered_project_ids":[5]}',
        b'{"sort_mode":"manual","ordered_project_ids":["invalid"]}',
        b'{"sort_mode":"manual","ordered_project_ids":{}}',
        b'{"sort_mode":"manual","ordered_project_ids":null}',
    ],
)
def test_invalid_order_falls_back_and_can_be_replaced(
    store: SqlAlchemyProjectStore, raw: bytes
) -> None:
    from sqlalchemy import LargeBinary, bindparam, update

    from omnigent.db.db_models import SqlPreference

    store.save_order([], user_id=None)
    with store._engine.begin() as connection:
        connection.execute(
            update(SqlPreference)
            .where(
                SqlPreference.workspace_id == 0,
                SqlPreference.user_id == "local",
                SqlPreference.key == "project_order",
            )
            .values(value=bindparam("raw", type_=LargeBinary)),
            {"raw": raw},
        )
    default = {"sort_mode": "alphabetical", "ordered_project_ids": None}
    assert store.get_order_preference(user_id=None) == default
    assert store.get_order(user_id=None) is None
    assert store.save_order(None, user_id=None) == default
    assert store.get_order_preference(user_id=None) == default
    store.save_order([], user_id=None)
    assert store.get_order(user_id=None) == []


def test_order_decoder_bounds_size_and_normalizes_duplicate_ids() -> None:
    import json

    from omnigent.db.compression import encode
    from omnigent.stores.project_store.sqlalchemy_store import _decode_order

    a, b = _uid("decode-a"), _uid("decode-b")
    for preference in ([b, a, b], {"sort_mode": "manual", "ordered_project_ids": [b, a, b]}):
        assert _decode_order(encode(json.dumps(preference))) == {
            "sort_mode": "manual",
            "ordered_project_ids": [b, a],
        }
    for preference in ([a] * 10001, ["x" * (512 * 1024)]):
        assert _decode_order(encode(json.dumps(preference))) == {
            "sort_mode": "alphabetical",
            "ordered_project_ids": None,
        }


def test_apply_order_deduplicates_and_appends_unranked_projects() -> None:
    from omnigent.stores.project_store import apply_project_order

    assert apply_project_order(
        ["b", "c", "a"], ["a", "deleted", "a"], project_id=str, project_name=str
    ) == ["a", "b", "c"]
