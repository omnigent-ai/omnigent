"""Tests for the host permission store (host-sharing grants)."""

from __future__ import annotations

import uuid

import pytest

from omnigent.stores.host_permission_store.sqlalchemy_store import (
    SqlAlchemyHostPermissionStore,
)
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

# 32-char hex host IDs (the binary-UUID migration z7 requires real UUIDs).
_HOST_X = uuid.uuid4().hex
_HOST_Y = uuid.uuid4().hex


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyHostPermissionStore:
    """Host permission store backed by the per-test SQLite database.

    Seeds the ``users`` and ``hosts`` rows the grant FKs reference
    (``host_permissions.user_id`` → ``users.id`` and ``.host_id`` →
    ``hosts.host_id``), so a grant in these tests behaves like one made
    through the route (which ensures the user and requires the host).

    :param db_uri: SQLite URI from the shared ``db_uri`` fixture.
    :returns: A :class:`SqlAlchemyHostPermissionStore` instance.
    """
    perm = SqlAlchemyPermissionStore(db_uri)
    for user in ("alice@test.com", "bob@test.com"):
        perm.ensure_user(user)
    host_store = HostStore(db_uri)
    for host_id in (_HOST_X, _HOST_Y):
        host_store.upsert_on_connect(host_id, host_id, "owner@test.com")
    return SqlAlchemyHostPermissionStore(db_uri)


def test_grant_then_get_roundtrips(store: SqlAlchemyHostPermissionStore) -> None:
    """A granted level reads back exactly, with timestamps and creator."""
    grant = store.grant("alice@test.com", _HOST_X, 2, created_by="admin@test.com")
    assert grant.user_id == "alice@test.com"
    assert grant.host_id == _HOST_X
    assert grant.level == 2
    assert grant.created_by == "admin@test.com"
    assert grant.created_at > 0
    assert grant.updated_at == grant.created_at

    fetched = store.get("alice@test.com", _HOST_X)
    assert fetched is not None
    assert fetched.level == 2
    assert fetched.created_by == "admin@test.com"


def test_grant_upsert_overwrites_level_preserving_created(
    store: SqlAlchemyHostPermissionStore,
) -> None:
    """Re-granting changes the level but keeps created_at / created_by."""
    first = store.grant("alice@test.com", _HOST_X, 1, created_by="admin@test.com")
    second = store.grant("alice@test.com", _HOST_X, 3, created_by="someone-else")

    assert second.level == 3
    assert second.created_at == first.created_at
    assert second.created_by == "admin@test.com"
    assert second.updated_at >= first.updated_at


def test_revoke_removes_grant(store: SqlAlchemyHostPermissionStore) -> None:
    """Revoke deletes the row and is idempotent."""
    store.grant("alice@test.com", _HOST_X, 2)
    assert store.revoke("alice@test.com", _HOST_X) is True
    assert store.get("alice@test.com", _HOST_X) is None
    assert store.revoke("alice@test.com", _HOST_X) is False


def test_check_access_respects_level_ordering(
    store: SqlAlchemyHostPermissionStore,
) -> None:
    """check_access is satisfied by an equal-or-higher grant only."""
    store.grant("alice@test.com", _HOST_X, 2)
    assert store.check_access("alice@test.com", _HOST_X, 1) is True
    assert store.check_access("alice@test.com", _HOST_X, 2) is True
    assert store.check_access("alice@test.com", _HOST_X, 3) is False
    assert store.check_access("bob@test.com", _HOST_X, 1) is False
    assert store.check_access(None, _HOST_X, 1) is False


def test_list_for_host_and_user(store: SqlAlchemyHostPermissionStore) -> None:
    """list_for_host / list_for_user return the matching grants."""
    store.grant("alice@test.com", _HOST_X, 2)
    store.grant("bob@test.com", _HOST_X, 1)
    store.grant("alice@test.com", _HOST_Y, 3)

    by_host = {g.user_id for g in store.list_for_host(_HOST_X)}
    assert by_host == {"alice@test.com", "bob@test.com"}

    by_user = {g.host_id for g in store.list_for_user("alice@test.com")}
    assert by_user == {_HOST_X, _HOST_Y}


def test_get_permission_level(store: SqlAlchemyHostPermissionStore) -> None:
    """get_permission_level returns the user's own grant level or None."""
    store.grant("alice@test.com", _HOST_X, 2)
    assert store.get_permission_level("alice@test.com", _HOST_X) == 2
    assert store.get_permission_level("bob@test.com", _HOST_X) is None
    assert store.get_permission_level(None, _HOST_X) is None
