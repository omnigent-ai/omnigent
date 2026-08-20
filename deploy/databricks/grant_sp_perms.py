"""
Grant a Databricks App service principal Lakebase schema privileges.

Run this after ``wc.apps.create`` creates the app service principal and
before ``wc.apps.deploy`` starts the app. The app needs these grants so
Alembic can create and migrate Omnigent tables on first boot.
"""

from __future__ import annotations

import argparse
import sys
from typing import Protocol, cast

import psycopg
from databricks.sdk import WorkspaceClient


class _GrantArgs(Protocol):
    """
    Parsed CLI arguments for the grant helper.

    :param app_name: Databricks App name, e.g. ``"omnigent"``.
    :param lakebase_endpoint: Full Lakebase endpoint resource path, e.g.
        ``"projects/omnigent/branches/production/endpoints/primary"``.
    :param database: PostgreSQL database name, e.g.
        ``"databricks_postgres"``.
    :param profile: Optional Databricks CLI profile name, e.g.
        ``"<your-profile>"``.
    """

    app_name: str
    lakebase_endpoint: str
    database: str
    profile: str | None


def _parse_args() -> _GrantArgs:
    """
    Parse command-line arguments for the grant helper.

    :returns: Parsed CLI arguments.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app-name",
        required=True,
        help="Databricks App name, e.g. 'omnigent'.",
    )
    parser.add_argument(
        "--lakebase-endpoint",
        required=True,
        help=(
            "Full Lakebase endpoint resource path, e.g. "
            "'projects/omnigent/branches/production/endpoints/primary'."
        ),
    )
    parser.add_argument(
        "--database",
        required=True,
        help="PostgreSQL database name, e.g. 'databricks_postgres'.",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Optional Databricks CLI profile name, e.g. '<your-profile>'.",
    )
    return cast(_GrantArgs, parser.parse_args())


def _grant_sql(sp_uuid: str) -> str:
    """
    Build the GRANT statement block for an app service principal.

    :param sp_uuid: App service principal client ID, e.g.
        ``"00000000-0000-0000-0000-000000000000"``. Lakebase creates a
        PostgreSQL role with this identifier when the app is created.
    :returns: SQL statements granting schema, table, and sequence
        privileges in the PostgreSQL ``public`` schema.
    """
    escaped = sp_uuid.replace('"', '""')
    quoted = f'"{escaped}"'
    return f"""
GRANT ALL ON SCHEMA public TO {quoted};
GRANT ALL ON ALL TABLES IN SCHEMA public TO {quoted};
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {quoted};
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {quoted};
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO {quoted};
"""


def _resolve_endpoint_host(wc: WorkspaceClient, endpoint_name: str) -> str | None:
    """
    Resolve the Lakebase endpoint hostname.

    :param wc: Databricks workspace client.
    :param endpoint_name: Full Lakebase endpoint resource path, e.g.
        ``"projects/omnigent/branches/production/endpoints/primary"``.
    :returns: Endpoint hostname, or ``None`` when the endpoint is not ready.
    """
    endpoint = wc.postgres.get_endpoint(name=endpoint_name)
    if endpoint.status is None or endpoint.status.hosts is None:
        return None
    return endpoint.status.hosts.host


def _build_conn_params(
    wc: WorkspaceClient, host: str, database: str, endpoint_name: str
) -> dict[str, str]:
    """
    Build psycopg connection params using a short-lived Lakebase OAuth token.

    Returned as keyword params (not a hand-built conninfo string) so the
    token — which we don't control the contents of — is never string-
    interpolated into a DSN where whitespace or ``key=value`` metacharacters
    could be mis-parsed.

    :param wc: Databricks workspace client.
    :param host: Lakebase endpoint hostname, e.g.
        ``"example.database.cloud.databricks.com"``.
    :param database: PostgreSQL database name, e.g.
        ``"databricks_postgres"``.
    :param endpoint_name: Full Lakebase endpoint resource path, e.g.
        ``"projects/omnigent/branches/production/endpoints/primary"``.
    :returns: psycopg connection keyword params for the current user.
    """
    cred = wc.postgres.generate_database_credential(endpoint=endpoint_name)
    pg_user = wc.current_user.me().user_name
    return {
        "host": host,
        "port": "5432",
        "dbname": database,
        "user": pg_user,
        "password": cred.token,
        "sslmode": "require",
    }


def main() -> int:
    """
    Resolve the app service principal and apply Lakebase grants.

    :returns: Process exit code, ``0`` on success and ``1`` when the app
        or Lakebase endpoint is not ready.
    """
    args = _parse_args()

    wc = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()

    app = wc.apps.get(name=args.app_name)
    sp_uuid = app.service_principal_client_id
    if not sp_uuid:
        print(
            f"ERROR: app '{args.app_name}' has no service_principal_client_id "
            "yet; wait for wc.apps.create() to finish.",
            file=sys.stderr,
        )
        return 1

    host = _resolve_endpoint_host(wc, args.lakebase_endpoint)
    if not host:
        print(
            f"ERROR: endpoint '{args.lakebase_endpoint}' has no hostname "
            "in status; wait for the endpoint to become ACTIVE.",
            file=sys.stderr,
        )
        return 1

    print(f"==> Granting public schema privileges to app SP {sp_uuid}")
    params = _build_conn_params(wc, host, args.database, args.lakebase_endpoint)
    with psycopg.connect(autocommit=True, **params) as conn, conn.cursor() as cur:
        cur.execute(_grant_sql(sp_uuid))

    print("Done. The app can create and migrate Omnigent tables on first boot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
