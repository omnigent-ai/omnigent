"""Unit tests for the host access resolver.

Covers :func:`omnigent.server.host_permissions.check_host_access` and
:func:`omnigent.server.host_permissions.get_host_permission_level`
against every resolution branch:

1. auth disabled (user_id=None) -> allow
2. admin -> allow
3. host owner -> allow at any level (no grant row)
4. sufficient grant -> allow; insufficient -> deny
5. no grant, not owner, not admin -> deny

Uses real SQLite-backed stores so owner lookup and the admin flag
behave exactly as in production.
"""

from __future__ import annotations

import pytest

from omnigent.server.host_permissions import (
    HOST_LEVEL_MANAGE,
    HOST_LEVEL_OWNER,
    HOST_LEVEL_USE,
    HOST_LEVEL_VIEW,
    check_host_access,
    get_host_permission_level,
)
from omnigent.stores.host_permission_store.sqlalchemy_store import (
    SqlAlchemyHostPermissionStore,
)
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


@pytest.fixture()
def stores(
    db_uri: str,
) -> tuple[HostStore, SqlAlchemyHostPermissionStore, SqlAlchemyPermissionStore]:
    """Host, host-permission, and session-permission stores on one DB.

    :param db_uri: SQLite URI from the shared fixture.
    :returns: ``(host_store, host_permission_store, permission_store)``.
    """
    host_store = HostStore(db_uri)
    host_perm = SqlAlchemyHostPermissionStore(db_uri)
    perm = SqlAlchemyPermissionStore(db_uri)
    # A host owned by the service principal (the CoDA case).
    host_store.upsert_on_connect("host_sp", "coda-app", "app-sp@example")
    # Grantee user rows must exist for the grant FK (users.id) to hold —
    # the route ensures these; the unit test seeds them up front.
    for user in ("viewer@example", "user@example"):
        perm.ensure_user(user)
    return host_store, host_perm, perm


def test_auth_disabled_allows(
    stores: tuple[HostStore, SqlAlchemyHostPermissionStore, SqlAlchemyPermissionStore],
) -> None:
    """user_id=None (auth disabled) is allowed at any level.

    Mirrors the single-user/local escape hatch the host routes use;
    breaking it would 403 every local launch.
    """
    host_store, host_perm, perm = stores
    assert check_host_access(None, "host_sp", HOST_LEVEL_MANAGE, host_perm, host_store, perm)
    assert get_host_permission_level(None, "host_sp", host_perm, host_store, perm) == (
        HOST_LEVEL_OWNER
    )


def test_owner_allowed_without_grant(
    stores: tuple[HostStore, SqlAlchemyHostPermissionStore, SqlAlchemyPermissionStore],
) -> None:
    """The host owner has full access with no grant row (FR-004)."""
    host_store, host_perm, perm = stores
    assert check_host_access(
        "app-sp@example", "host_sp", HOST_LEVEL_MANAGE, host_perm, host_store, perm
    )
    assert (
        get_host_permission_level("app-sp@example", "host_sp", host_perm, host_store, perm)
        == HOST_LEVEL_OWNER
    )


def test_admin_allowed_without_grant(
    stores: tuple[HostStore, SqlAlchemyHostPermissionStore, SqlAlchemyPermissionStore],
) -> None:
    """An admin bypasses the per-host ACCESS check (FR-005).

    Access is granted, but the DISPLAY level is not spuriously "owner":
    an admin who neither owns nor was granted the host shows ``None``
    (no grant), so ``(owned_by_current_user=False, permission_level)``
    stays coherent in the UI. Admin is an access override, not an
    ownership claim.
    """
    host_store, host_perm, perm = stores
    perm.ensure_user("admin@example", is_admin=True)
    # Access: yes, at every level.
    assert check_host_access(
        "admin@example", "host_sp", HOST_LEVEL_MANAGE, host_perm, host_store, perm
    )
    # Display: not "owner" — the admin neither owns nor has a grant here.
    assert (
        get_host_permission_level("admin@example", "host_sp", host_perm, host_store, perm) is None
    )

    # With an explicit grant, the admin's displayed level is that grant.
    host_perm.grant("admin@example", "host_sp", HOST_LEVEL_USE)
    assert (
        get_host_permission_level("admin@example", "host_sp", host_perm, host_store, perm)
        == HOST_LEVEL_USE
    )


def test_grant_level_ordering(
    stores: tuple[HostStore, SqlAlchemyHostPermissionStore, SqlAlchemyPermissionStore],
) -> None:
    """A grantee is allowed up to their level and no higher.

    A ``view`` grantee sees the host but cannot use it; a ``use``
    grantee can use but not manage.
    """
    host_store, host_perm, perm = stores
    host_perm.grant("viewer@example", "host_sp", HOST_LEVEL_VIEW)
    host_perm.grant("user@example", "host_sp", HOST_LEVEL_USE)

    assert check_host_access(
        "viewer@example", "host_sp", HOST_LEVEL_VIEW, host_perm, host_store, perm
    )
    assert not check_host_access(
        "viewer@example", "host_sp", HOST_LEVEL_USE, host_perm, host_store, perm
    )

    assert check_host_access(
        "user@example", "host_sp", HOST_LEVEL_USE, host_perm, host_store, perm
    )
    assert not check_host_access(
        "user@example", "host_sp", HOST_LEVEL_MANAGE, host_perm, host_store, perm
    )

    assert get_host_permission_level("viewer@example", "host_sp", host_perm, host_store, perm) == 1
    assert get_host_permission_level("user@example", "host_sp", host_perm, host_store, perm) == 2


def test_no_grant_denied(
    stores: tuple[HostStore, SqlAlchemyHostPermissionStore, SqlAlchemyPermissionStore],
) -> None:
    """A non-owner, non-admin, non-grantee is denied and has no level."""
    host_store, host_perm, perm = stores
    assert not check_host_access(
        "stranger@example", "host_sp", HOST_LEVEL_VIEW, host_perm, host_store, perm
    )
    assert (
        get_host_permission_level("stranger@example", "host_sp", host_perm, host_store, perm)
        is None
    )
