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
    except Exception as exc:  # noqa: BLE001 — surface a useful message, stay idempotent
        # Already-exists is success for this idempotent grant.
        if "exist" in str(exc).lower() or "already" in str(exc).lower():
            print("Role already exists — nothing to do.", flush=True)
            return 0
        print(f"Failed to create database instance role: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
