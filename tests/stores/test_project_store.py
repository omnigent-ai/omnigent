"""Tests for :class:`SqlAlchemyProjectStore`.

Exercises project CRUD and the project-level ACL (grants + the
``__members__`` / ``__public__`` sentinels) against a real SQLite database,
mirroring :mod:`tests.stores.test_permission_store`.
"""

from __future__ import annotations

import pytest

from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore


@pytest.fixture()
def stores(db_uri: str) -> tuple[SqlAlchemyProjectStore, SqlAlchemyPermissionStore]:
    """A project store plus a permission store (the latter to satisfy the
    ``users`` FK that both grant tables reference)."""
    perm = SqlAlchemyPermissionStore(db_uri)
    for user in ("alice", "bob", "carol", "__members__", "__public__"):
        perm.ensure_user(user)
    return SqlAlchemyProjectStore(db_uri), perm


def test_create_project_grants_owner(stores) -> None:
    store, _ = stores
    project = store.create_project("Client X", "alice")
    assert project.id.startswith("proj_")
    assert project.name == "Client X"
    assert project.created_by == "alice"
    assert store.get_permission_level("alice", project.id) == 4


def test_create_project_duplicate_name_per_owner_rejected(stores) -> None:
    store, _ = stores
    store.create_project("Client X", "alice")
    with pytest.raises(ValueError):
        store.create_project("Client X", "alice")
    # A different owner may reuse the name.
    other = store.create_project("Client X", "bob")
    assert other.created_by == "bob"


def test_rename_and_delete_project(stores) -> None:
    store, _ = stores
    project = store.create_project("Client X", "alice")
    renamed = store.rename_project(project.id, "Client Y")
    assert renamed is not None and renamed.name == "Client Y"
    assert store.delete_project(project.id) is True
    assert store.get_project(project.id) is None
    # Grants are dropped with the project.
    assert store.get_permission_level("alice", project.id) is None


def test_grant_check_and_revoke(stores) -> None:
    store, _ = stores
    project = store.create_project("Client X", "alice")
    store.grant("bob", project.id, 1)
    assert store.check_access("bob", project.id, 1) is True
    assert store.check_access("bob", project.id, 2) is False
    store.revoke("bob", project.id)
    assert store.check_access("bob", project.id, 1) is False


def test_members_sentinel_resolves_for_any_authenticated_user(stores) -> None:
    store, _ = stores
    project = store.create_project("Client X", "alice")
    store.grant("__members__", project.id, 1)
    # carol never got a direct grant, yet the members sentinel resolves.
    assert store.check_access("carol", project.id, 1) is True
    # ...but not for an anonymous caller.
    assert store.check_access(None, project.id, 1) is False


def test_public_sentinel_resolves_for_anonymous(stores) -> None:
    store, _ = stores
    project = store.create_project("Client X", "alice")
    store.grant("__public__", project.id, 1)
    assert store.check_access(None, project.id, 1) is True


def test_list_projects_for_user_includes_owned_and_members(stores) -> None:
    store, _ = stores
    owned = store.create_project("Owned", "alice")
    shared = store.create_project("Shared", "bob")
    store.grant("__members__", shared.id, 1)

    alice_projects = {p.name: level for p, level in store.list_projects_for_user("alice")}
    assert alice_projects["Owned"] == 4
    # Alice sees bob's project via the members sentinel, at read.
    assert alice_projects["Shared"] == 1
    assert owned.id  # sanity


def test_list_for_project_returns_all_grants(stores) -> None:
    store, _ = stores
    project = store.create_project("Client X", "alice")
    store.grant("bob", project.id, 1)
    store.grant("__members__", project.id, 1)
    by_user = {g.user_id: g.level for g in store.list_for_project(project.id)}
    assert by_user == {"alice": 4, "bob": 1, "__members__": 1}
