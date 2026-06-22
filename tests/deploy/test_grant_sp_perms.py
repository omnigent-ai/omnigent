"""Tests for the crash-safe DATABRICKS_SUPERUSER upgrade in grant_sp_perms.

The Lakebase role API has no update verb, so elevating an existing role to
DATABRICKS_SUPERUSER must delete + recreate. The danger is a recreate that
fails after the delete succeeds — that would leave the app's Postgres role
permanently missing and break DB auth. ``_upgrade_role_to_superuser`` makes the
operation recoverable: it captures the role first and restores it if the
recreate fails. These tests pin that invariant so it can't regress.
"""

from __future__ import annotations

from typing import cast

import pytest
from databricks.sdk.service import database as db

from deploy.databricks.grant_sp_perms import _upgrade_role_to_superuser

ROLE_NAME = "1234-5678-sp-client-id"
INSTANCE = "omnigent-db"


def _role(membership: db.DatabaseInstanceRoleMembershipRole | None) -> db.DatabaseInstanceRole:
    return db.DatabaseInstanceRole(
        name=ROLE_NAME,
        instance_name=INSTANCE,
        identity_type=db.DatabaseInstanceRoleIdentityType.SERVICE_PRINCIPAL,
        membership_role=membership,
    )


class _FakeDatabase:
    """In-memory stand-in for ``WorkspaceClient.database``.

    Tracks the role's presence/config so tests can assert the role is never
    left deleted-and-not-recreated. ``fail_create_membership`` makes any create
    of a role with that membership_role raise, simulating a recreate failure.
    """

    def __init__(self, existing: db.DatabaseInstanceRole) -> None:
        self.roles: dict[str, db.DatabaseInstanceRole] = {existing.name: existing}
        self.fail_create_membership: object = object()  # sentinel: never matches
        self.create_calls: list[db.DatabaseInstanceRole] = []
        self.delete_calls: list[str] = []

    def get_database_instance_role(
        self, *, instance_name: str, name: str
    ) -> db.DatabaseInstanceRole:
        return self.roles[name]

    def delete_database_instance_role(
        self, *, instance_name: str, name: str, allow_missing: bool | None = None
    ) -> None:
        self.delete_calls.append(name)
        self.roles.pop(name, None)

    def create_database_instance_role(
        self,
        *,
        instance_name: str,
        database_instance_role: db.DatabaseInstanceRole,
    ) -> db.DatabaseInstanceRole:
        self.create_calls.append(database_instance_role)
        if database_instance_role.membership_role == self.fail_create_membership:
            raise RuntimeError("simulated recreate failure")
        self.roles[database_instance_role.name] = database_instance_role
        return database_instance_role


def _run(fake: _FakeDatabase, desired: db.DatabaseInstanceRole) -> None:
    # The fake is a structural stand-in for the SDK's DatabaseAPI; cast so the
    # statically-typed helper accepts it.
    _upgrade_role_to_superuser(
        cast("db.DatabaseAPI", fake),
        instance_name=INSTANCE,
        name=ROLE_NAME,
        desired_role=desired,
    )


def test_recreate_failure_restores_original_role() -> None:
    """If the superuser recreate fails, the original role must be restored."""
    original = _role(None)  # plain connect role, no superuser membership
    fake = _FakeDatabase(original)
    # Make the elevated (superuser) recreate fail; the restore (membership=None)
    # must still succeed.
    fake.fail_create_membership = db.DatabaseInstanceRoleMembershipRole.DATABRICKS_SUPERUSER

    desired = _role(db.DatabaseInstanceRoleMembershipRole.DATABRICKS_SUPERUSER)

    with pytest.raises(RuntimeError, match="original role was restored"):
        _run(fake, desired)

    # Invariant: the role is NOT left missing — it was restored.
    assert ROLE_NAME in fake.roles
    restored = fake.roles[ROLE_NAME]
    assert restored.membership_role == original.membership_role
    # The delete happened, the failed upgrade and the restore were both attempted.
    assert fake.delete_calls == [ROLE_NAME]
    assert len(fake.create_calls) == 2
    assert fake.create_calls[0].membership_role == desired.membership_role
    assert fake.create_calls[1].membership_role == original.membership_role


def test_total_failure_raises_clear_missing_role_error() -> None:
    """If both recreate and restore fail, the error must flag the missing role."""
    original = _role(None)
    fake = _FakeDatabase(original)

    # Make ALL creates fail regardless of membership_role — both the upgrade
    # recreate and the rollback restore.
    def _always_fail(
        *, instance_name: str, database_instance_role: db.DatabaseInstanceRole
    ) -> db.DatabaseInstanceRole:
        fake.create_calls.append(database_instance_role)
        raise RuntimeError("simulated total failure")

    fake.create_database_instance_role = _always_fail  # type: ignore[method-assign]

    desired = _role(db.DatabaseInstanceRoleMembershipRole.DATABRICKS_SUPERUSER)

    with pytest.raises(RuntimeError, match="role is now MISSING"):
        _run(fake, desired)
    # Both creates were attempted (upgrade + restore).
    assert len(fake.create_calls) == 2


def test_already_superuser_does_no_destructive_work() -> None:
    """If the role already has superuser, do not delete/recreate anything."""
    original = _role(db.DatabaseInstanceRoleMembershipRole.DATABRICKS_SUPERUSER)
    fake = _FakeDatabase(original)

    desired = _role(db.DatabaseInstanceRoleMembershipRole.DATABRICKS_SUPERUSER)
    _run(fake, desired)

    assert fake.delete_calls == []
    assert fake.create_calls == []
    assert ROLE_NAME in fake.roles


def test_successful_upgrade_elevates_role() -> None:
    """Happy path: a plain role is deleted then recreated with superuser."""
    original = _role(None)
    fake = _FakeDatabase(original)

    desired = _role(db.DatabaseInstanceRoleMembershipRole.DATABRICKS_SUPERUSER)
    _run(fake, desired)

    assert fake.delete_calls == [ROLE_NAME]
    assert len(fake.create_calls) == 1
    assert (
        fake.roles[ROLE_NAME].membership_role
        == db.DatabaseInstanceRoleMembershipRole.DATABRICKS_SUPERUSER
    )
