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
from databricks.sdk.errors import ResourceDoesNotExist
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
        # ``delete_error`` makes the delete RPC raise; ``delete_removes_role``
        # controls whether the role is actually gone server-side when it does
        # (i.e. delete took effect then the transport failed vs. delete had no
        # effect at all). Default: delete succeeds and removes the role.
        self.delete_error: Exception | None = None
        self.delete_removes_role: bool = True
        # ``probe_error`` makes the *probe* call (the get after a failed delete)
        # raise a non-not-found error, simulating a transient/unrelated probe
        # failure that must NOT be misclassified as "role gone".
        self.probe_error: Exception | None = None

    def get_database_instance_role(
        self, *, instance_name: str, name: str
    ) -> db.DatabaseInstanceRole:
        # The probe runs only after a delete has been attempted; the initial
        # capture (before any delete) must keep working so we can inject a
        # transient failure for the probe alone.
        if self.probe_error is not None and self.delete_calls:
            raise self.probe_error
        try:
            return self.roles[name]
        except KeyError:
            # Model the real SDK: a missing role raises a typed not-found error,
            # not a bare KeyError.
            raise ResourceDoesNotExist(f"role {name!r} not found") from None

    def delete_database_instance_role(
        self, *, instance_name: str, name: str, allow_missing: bool | None = None
    ) -> None:
        self.delete_calls.append(name)
        if self.delete_error is not None:
            if self.delete_removes_role:
                self.roles.pop(name, None)
            raise self.delete_error
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


def test_delete_raises_after_removing_role_restores_it() -> None:
    """Delete RPC errors *after* the role is gone server-side → restore it.

    Models a delete that takes effect then fails on the response (timeout /
    transport error). The probe sees the role is gone, so the function must run
    the recreate/restore path exactly like a post-delete recreate failure: the
    original role is restored, DB auth stays intact, and it re-raises so the
    operator knows the upgrade did not complete.
    """
    original = _role(None)
    fake = _FakeDatabase(original)
    fake.delete_error = RuntimeError("delete timed out after removing role")
    fake.delete_removes_role = True  # role is actually gone server-side

    desired = _role(db.DatabaseInstanceRoleMembershipRole.DATABRICKS_SUPERUSER)

    with pytest.raises(RuntimeError, match="original role was restored"):
        _run(fake, desired)

    # Invariant: the role is NOT left missing — the restore put it back.
    assert ROLE_NAME in fake.roles
    assert fake.roles[ROLE_NAME].membership_role == original.membership_role
    assert fake.delete_calls == [ROLE_NAME]
    # Only the restore (captured/original config) was created — the desired
    # superuser recreate is never attempted on the delete-error path.
    assert len(fake.create_calls) == 1
    assert fake.create_calls[0].membership_role == original.membership_role


def test_delete_raises_after_removing_role_then_restore_fails_is_missing() -> None:
    """Delete removes the role, then the restore also fails → MISSING error.

    Same delete-after-removal scenario, but the best-effort restore can't bring
    the role back. The role is genuinely gone, so the function must raise the
    distinct MISSING-role error with manual-repair guidance.
    """
    original = _role(None)
    fake = _FakeDatabase(original)
    fake.delete_error = RuntimeError("delete timed out after removing role")
    fake.delete_removes_role = True

    def _always_fail(
        *, instance_name: str, database_instance_role: db.DatabaseInstanceRole
    ) -> db.DatabaseInstanceRole:
        fake.create_calls.append(database_instance_role)
        raise RuntimeError("simulated restore failure")

    fake.create_database_instance_role = _always_fail  # type: ignore[method-assign]

    desired = _role(db.DatabaseInstanceRoleMembershipRole.DATABRICKS_SUPERUSER)

    with pytest.raises(RuntimeError, match="role is now MISSING"):
        _run(fake, desired)
    # The restore was attempted (and failed).
    assert len(fake.create_calls) == 1


def test_delete_raises_then_genuine_not_found_probe_restores_role() -> None:
    """Delete errors, then the probe returns a typed not-found → restore runs.

    Companion to ``test_delete_raises_after_removing_role_restores_it`` that
    pins the *typed* signal explicitly: the probe raising the SDK's not-found
    error (``ResourceDoesNotExist``) — not just any exception — is what proves
    the role is genuinely gone and drives the recreate/restore path.
    """
    original = _role(None)
    fake = _FakeDatabase(original)
    fake.delete_error = RuntimeError("delete timed out after removing role")
    fake.delete_removes_role = True  # role gone → probe will raise not-found

    desired = _role(db.DatabaseInstanceRoleMembershipRole.DATABRICKS_SUPERUSER)

    with pytest.raises(RuntimeError, match="original role was restored"):
        _run(fake, desired)

    # The genuine not-found drove the restore: role is back, intact.
    assert ROLE_NAME in fake.roles
    assert fake.roles[ROLE_NAME].membership_role == original.membership_role
    assert fake.delete_calls == [ROLE_NAME]
    assert len(fake.create_calls) == 1
    assert fake.create_calls[0].membership_role == original.membership_role


def test_delete_raises_then_transient_probe_error_is_not_treated_as_gone() -> None:
    """Delete errors, then the probe fails with a NON-not-found error.

    A transient/unrelated probe failure must NOT be misclassified as "role
    gone": doing so would fire a spurious restore (and could even double-create
    an intact role). The function must instead surface a clear INDETERMINATE
    error and attempt no recreate/restore at all.
    """
    original = _role(None)
    fake = _FakeDatabase(original)
    fake.delete_error = RuntimeError("delete RPC failed")
    # The probe itself fails transiently — state of the role is unknowable here.
    fake.probe_error = RuntimeError("probe failed: transient transport error")

    desired = _role(db.DatabaseInstanceRoleMembershipRole.DATABRICKS_SUPERUSER)

    with pytest.raises(RuntimeError, match="INDETERMINATE"):
        _run(fake, desired)

    # No spurious restore: nothing was recreated. The delete was attempted once.
    assert fake.create_calls == []
    assert fake.delete_calls == [ROLE_NAME]


def test_delete_raises_while_role_still_exists_is_non_destructive() -> None:
    """Delete RPC errors but the role still exists → no destructive outcome.

    Models a delete that had no effect at all. The probe sees the role still
    present, so the function must raise a clear error WITHOUT recreating
    anything, and the role must remain intact and unmodified.
    """
    original = _role(None)
    fake = _FakeDatabase(original)
    fake.delete_error = RuntimeError("delete rejected, role untouched")
    fake.delete_removes_role = False  # delete had no effect

    desired = _role(db.DatabaseInstanceRoleMembershipRole.DATABRICKS_SUPERUSER)

    with pytest.raises(RuntimeError, match="still exists and was NOT modified"):
        _run(fake, desired)

    # Invariant: nothing was destroyed — the role is present and unchanged, and
    # no recreate was attempted.
    assert ROLE_NAME in fake.roles
    assert fake.roles[ROLE_NAME].membership_role == original.membership_role
    assert fake.delete_calls == [ROLE_NAME]
    assert fake.create_calls == []
