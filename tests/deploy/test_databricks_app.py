"""Tests for the Databricks Apps entrypoint env bridge.

Covers ``configure_databricks_env`` — the pure function that maps the
Databricks-Apps runtime contract (DATABRICKS_APP_PORT, injected Lakebase PG*
vars) onto the env vars the generic server entrypoint reads. The function must
compose a *password-less* Lakebase DATABASE_URL (the OAuth token is minted per
connection by ``omnigent.db.utils``) and never clobber values the operator set
explicitly.
"""

from __future__ import annotations

import pytest

from deploy.databricks.src.app import configure_databricks_env


def _lakebase_env() -> dict[str, str]:
    """A minimal Databricks-Apps environment with a bound Lakebase resource."""
    return {
        "DATABRICKS_APP_PORT": "8000",
        "PGHOST": "instance.example.cloud.databricks.com",
        "PGUSER": "1234-5678-sp-client-id",
        "PGDATABASE": "omnigent",
        "PGPORT": "5432",
        "OMNIGENT_LAKEBASE_INSTANCE": "omnigent-db",
    }


def test_port_bridged_from_databricks_app_port() -> None:
    env = _lakebase_env()
    configure_databricks_env(env)
    assert env["PORT"] == "8000"


def test_explicit_port_not_overwritten() -> None:
    env = _lakebase_env()
    env["PORT"] = "9000"
    configure_databricks_env(env)
    assert env["PORT"] == "9000"


def test_lakebase_database_url_composed_without_password() -> None:
    env = _lakebase_env()
    configure_databricks_env(env)
    url = env["DATABASE_URL"]
    # psycopg3 dialect, sslmode=require, and the role/host/db all present.
    assert url == (
        "postgresql+psycopg://1234-5678-sp-client-id"
        "@instance.example.cloud.databricks.com:5432/omnigent?sslmode=require"
    )
    # No password is baked in — the token is minted per connection.
    assert ":@" not in url
    assert "password" not in url.lower()


def test_explicit_database_url_not_overwritten() -> None:
    env = _lakebase_env()
    env["DATABASE_URL"] = "postgresql+psycopg://u:pw@host/db"
    configure_databricks_env(env)
    assert env["DATABASE_URL"] == "postgresql+psycopg://u:pw@host/db"


def test_composed_lakebase_url_requires_instance_name() -> None:
    env = _lakebase_env()
    del env["OMNIGENT_LAKEBASE_INSTANCE"]
    with pytest.raises(RuntimeError, match="OMNIGENT_LAKEBASE_INSTANCE"):
        configure_databricks_env(env)


def test_no_pg_vars_leaves_database_url_unset() -> None:
    env = {"DATABRICKS_APP_PORT": "8000"}
    configure_databricks_env(env)
    assert "DATABASE_URL" not in env
    assert env["PORT"] == "8000"


def test_auth_provider_defaults_to_header() -> None:
    env = _lakebase_env()
    configure_databricks_env(env)
    assert env["OMNIGENT_AUTH_PROVIDER"] == "header"


def test_explicit_auth_provider_not_overwritten() -> None:
    env = _lakebase_env()
    env["OMNIGENT_AUTH_PROVIDER"] = "oidc"
    configure_databricks_env(env)
    assert env["OMNIGENT_AUTH_PROVIDER"] == "oidc"
