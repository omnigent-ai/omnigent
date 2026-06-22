#!/usr/bin/env python3
"""Grant an Omnigent Databricks App's service principal access to Lakebase.

A Databricks App runs as an auto-created service principal. For that SP to
connect to the Lakebase Postgres instance — and for
``generate_database_credential`` (the per-connection OAuth token mint in
``omnigent/db/utils.py``) to succeed — the SP must exist as a Postgres role on
the instance. This script looks up the app's SP and registers it as a
database instance role (idempotent).

Run it once after the first deploy creates the app (and thus its SP)::

    python deploy/databricks/grant_sp_perms.py \
        --app-name omnigent-server --instance omnigent-db

Auth uses ambient Databricks credentials (CLI profile / env). The caller needs
permission to administer the Lakebase instance.

Note: attaching the Lakebase *resource* to the app in ``databricks.yml`` already
grants connect at the resource level; this script ensures the matching Postgres
role exists on the instance, which is what the OAuth-token connection path
authenticates against. Verify against your workspace — the Lakebase role API is
Public Preview and may shift.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from databricks.sdk.service import database as db


def _upgrade_role_to_superuser(
    database: db.DatabaseAPI,
    instance_name: str,
    name: str,
    desired_role: db.DatabaseInstanceRole,
) -> None:
    """Crash-safe upgrade of an existing role to DATABRICKS_SUPERUSER.

    The Lakebase role API (databricks-sdk 0.115.0) exposes only
    create/delete/get/list — there is no update/alter/patch verb (verified
    against ``DatabaseAPI``), so the only way to change ``membership_role`` is
    delete + recreate. A naive delete-then-create leaves the role permanently
    gone if the recreate fails for any reason, breaking DB auth until manual
    repair.

    This makes the operation recoverable: it captures the existing role's full
    config first, then performs delete + recreate as a single recoverable
    transaction. Both the delete and the recreate are wrapped: if the recreate
    fails (or the delete fails *after* having removed the role server-side) it
    best-effort restores the original role from the captured config and re-raises
    with a clear error. Invariant: on no path is the role left
    deleted-and-not-recreated WITHOUT raising the explicit MISSING-role guidance
    to the operator.
    """
    # Capture the existing role's full config BEFORE any destructive action so
    # we can restore it verbatim if the recreate fails.
    captured = database.get_database_instance_role(instance_name=instance_name, name=name)
    if captured.membership_role == desired_role.membership_role:
        print("Role already has DATABRICKS_SUPERUSER — nothing to do.", flush=True)
        return

    def _recreate_and_restore(cause: Exception) -> None:
        """Restore the original role after the role has been deleted.

        Called when the role is known/believed gone server-side and the desired
        recreate has not succeeded. Best-effort restores the captured config;
        if THAT also fails the role is genuinely missing, so raise the distinct
        MISSING-role error with manual-repair guidance.
        """
        print(
            f"Restoring the original role {name!r} from captured config "
            f"after failure ({cause!r}).",
            file=sys.stderr,
            flush=True,
        )
        try:
            database.create_database_instance_role(
                instance_name=instance_name, database_instance_role=captured
            )
        except Exception as restore_exc:  # noqa: BLE001
            raise RuntimeError(
                f"Failed to upgrade role {name!r} to DATABRICKS_SUPERUSER AND "
                f"failed to restore the original role ({restore_exc!r}). The "
                "role is now MISSING on the instance — recreate it manually to "
                "restore DB auth."
            ) from cause
        raise RuntimeError(
            f"Failed to upgrade role {name!r} to DATABRICKS_SUPERUSER "
            f"({cause!r}); the original role was restored, so DB auth is intact "
            "but the role was NOT elevated. Re-run once the cause is resolved."
        ) from cause

    print(
        f"Upgrading role {name!r} to DATABRICKS_SUPERUSER (delete + recreate with membership).",
        flush=True,
    )
    try:
        database.delete_database_instance_role(
            instance_name=instance_name, name=name, allow_missing=True
        )
    except Exception as delete_exc:
        # The delete RPC errored — but the role may already be gone server-side
        # (e.g. the delete took effect and then the transport timed out before
        # the response). Probe the actual role state to decide: if the role is
        # gone we're in the same deleted-and-not-recreated state as a post-delete
        # failure, so run the recreate/restore path; if it still exists nothing
        # destructive happened, so raise a clear error without recreating.
        print(
            f"Delete of role {name!r} failed ({delete_exc!r}); "
            "probing whether the role still exists.",
            file=sys.stderr,
            flush=True,
        )
        try:
            database.get_database_instance_role(instance_name=instance_name, name=name)
        except Exception:  # noqa: BLE001
            # Probe says the role is gone despite the delete error — restore it.
            _recreate_and_restore(delete_exc)
            return  # unreachable: _recreate_and_restore always raises.
        # Role still exists; the delete had no effect. Nothing was destroyed.
        raise RuntimeError(
            f"Failed to delete role {name!r} while upgrading to "
            f"DATABRICKS_SUPERUSER ({delete_exc!r}); the role still exists and "
            "was NOT modified, so DB auth is intact. Re-run once the cause is "
            "resolved."
        ) from delete_exc

    try:
        database.create_database_instance_role(
            instance_name=instance_name, database_instance_role=desired_role
        )
    except Exception as exc:  # noqa: BLE001
        # The role is now deleted but not recreated — an irrecoverable state for
        # DB auth. Best-effort restore the original role from the captured
        # config, then re-raise so the operator knows the upgrade failed.
        # (_recreate_and_restore always raises.)
        _recreate_and_restore(exc)
    print("Role upgraded to DATABRICKS_SUPERUSER.", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-name", required=True, help="Databricks App name")
    parser.add_argument("--instance", required=True, help="Lakebase instance name")
    parser.add_argument(
        "--superuser",
        action="store_true",
        help="grant the DATABRICKS_SUPERUSER membership role (needed to run "
        "migrations that create/alter tables on first boot)",
    )
    args = parser.parse_args(argv)

    from databricks.sdk import WorkspaceClient
    from databricks.sdk import errors as db_errors
    from databricks.sdk.service import database as db

    workspace_client = WorkspaceClient()

    app = workspace_client.apps.get(name=args.app_name)
    sp_client_id = app.service_principal_client_id
    if not sp_client_id:
        print(
            f"App {args.app_name!r} has no service principal yet — deploy and "
            "start the app first, then re-run.",
            file=sys.stderr,
        )
        return 1

    print(
        f"Granting Lakebase access on instance {args.instance!r} to app "
        f"{args.app_name!r} (SP client id {sp_client_id}).",
        flush=True,
    )

    # The role's name is the SP's client id — the Postgres role Lakebase maps
    # the service principal onto.
    # First-boot Alembic migrations create the schema/tables, which needs
    # CREATE on the database. The simplest way to guarantee that on a fresh
    # instance is the DATABRICKS_SUPERUSER membership (--superuser); you can
    # drop it to a plain connect role afterward.
    role = db.DatabaseInstanceRole(
        name=sp_client_id,
        instance_name=args.instance,
        identity_type=db.DatabaseInstanceRoleIdentityType.SERVICE_PRINCIPAL,
        membership_role=(
            db.DatabaseInstanceRoleMembershipRole.DATABRICKS_SUPERUSER if args.superuser else None
        ),
    )

    try:
        workspace_client.database.create_database_instance_role(
            instance_name=args.instance, database_instance_role=role
        )
        print("Role created.", flush=True)
        return 0
    except db_errors.ResourceAlreadyExists:
        # Idempotent: the role already exists. Re-raise anything else (a 4xx/5xx
        # that is NOT a duplicate-role error must not be swallowed) by only
        # catching the SDK's typed already-exists error.
        print(f"Role {sp_client_id!r} already exists.", flush=True)

    # The role exists. If the caller asked for --superuser, make sure it actually
    # has the DATABRICKS_SUPERUSER membership — otherwise a role created earlier
    # without it would silently stay un-upgraded and first-boot migrations would
    # fail. The role API has no update verb, so the equivalent of
    # ``ALTER ROLE ... WITH SUPERUSER`` is delete + recreate with the membership,
    # done crash-safely (capture + rollback) so a failed recreate can't leave
    # the role permanently missing.
    if not args.superuser:
        print("Nothing to do (no --superuser requested).", flush=True)
        return 0

    _upgrade_role_to_superuser(
        workspace_client.database,
        instance_name=args.instance,
        name=sp_client_id,
        desired_role=role,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
