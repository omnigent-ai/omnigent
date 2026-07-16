"""Tests for the host permission store (host-sharing grants)."""

from __future__ import annotations

import pytest

from omnigent.stores.host_permission_store.sqlalchemy_store import (
    SqlAlchemyHostPermissionStore,
)
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


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
    for host_id in ("host_x", "host_y"):
        host_store.upsert_on_connect(host_id, host_id, "owner@test.com")
    return SqlAlchemyHostPermissionStore(db_uri)


def test_grant_then_get_roundtrips(store: SqlAlchemyHostPermissionStore) -> None:
    """A granted level reads back exactly, with timestamps and creator.

    If get returns None or a different level, the upsert or the
    entity mapping is broken.
    """
    grant = store.grant("alice@test.com", "host_x", 2, created_by="admin@test.com")
    assert grant.user_id == "alice@test.com"
    assert grant.host_id == "host_x"
    assert grant.level == 2
    assert grant.created_by == "admin@test.com"
    assert grant.created_at > 0
    assert grant.updated_at == grant.created_at

    fetched = store.get("alice@test.com", "host_x")
    assert fetched is not None
    assert fetched.level == 2
    assert fetched.created_by == "admin@test.com"


def test_grant_upsert_overwrites_level_preserving_created(
    store: SqlAlchemyHostPermissionStore,
) -> None:
    """Re-granting changes the level but keeps created_at / created_by.

    The created metadata records who first shared and when; a level
    change (upgrade/downgrade) must not rewrite it.
    """
    first = store.grant("alice@test.com", "host_x", 1, created_by="admin@test.com")
    second = store.grant("alice@test.com", "host_x", 3, created_by="someone-else")

    assert second.level == 3
    # created_at and created_by come from the first insert.
    assert second.created_at == first.created_at
    assert second.created_by == "admin@test.com"
    # updated_at advanced (or at least did not go backwards).
    assert second.updated_at >= first.updated_at


def test_revoke_removes_grant(store: SqlAlchemyHostPermissionStore) -> None:
    """Revoke deletes the row and is idempotent.

    A revoked grant must not authorize subsequent access (SR-004).
    """
    store.grant("alice@test.com", "host_x", 2)
    assert store.revoke("alice@test.com", "host_x") is True
    assert store.get("alice@test.com", "host_x") is None
    # Second revoke is a no-op.
    assert store.revoke("alice@test.com", "host_x") is False


def test_check_access_respects_level_ordering(
    store: SqlAlchemyHostPermissionStore,
) -> None:
    """check_access is satisfied by an equal-or-higher grant only.

    A ``use`` (2) grant must satisfy a ``view`` (1) requirement but
    not a ``manage`` (3) requirement.
    """
    store.grant("alice@test.com", "host_x", 2)
    assert store.check_access("alice@test.com", "host_x", 1) is True
    assert store.check_access("alice@test.com", "host_x", 2) is True
    assert store.check_access("alice@test.com", "host_x", 3) is False
    # Unknown user / host → no access.
    assert store.check_access("bob@test.com", "host_x", 1) is False
    assert store.check_access(None, "host_x", 1) is False


def test_list_for_host_and_user(store: SqlAlchemyHostPermissionStore) -> None:
    """list_for_host / list_for_user return the matching grants.

    Backs the permissions API and the "hosts I can access" query.
    """
    store.grant("alice@test.com", "host_x", 2)
    store.grant("bob@test.com", "host_x", 1)
    store.grant("alice@test.com", "host_y", 3)

    by_host = {g.user_id for g in store.list_for_host("host_x")}
    assert by_host == {"alice@test.com", "bob@test.com"}

    by_user = {g.host_id for g in store.list_for_user("alice@test.com")}
    assert by_user == {"host_x", "host_y"}


def test_get_permission_level(store: SqlAlchemyHostPermissionStore) -> None:
    """get_permission_level returns the user's own grant level or None.

    Owner/admin resolution is layered on in the access helper, not
    here — this store only knows about stored grants.
    """
    store.grant("alice@test.com", "host_x", 2)
    assert store.get_permission_level("alice@test.com", "host_x") == 2
    assert store.get_permission_level("bob@test.com", "host_x") is None
    assert store.get_permission_level(None, "host_x") is None
