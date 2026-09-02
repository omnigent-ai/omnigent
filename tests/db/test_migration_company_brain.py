from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from omnigent.db.db_models import SqlIntegrationConnection
from omnigent.db.utils import _build_alembic_config

_REVISION = "cb1a2b3c4d5e"
_PREVIOUS = "ga1b2c3d4e5f"
_TABLES = {
    "brain_installations",
    "integration_connections",
    "integration_selections",
    "integration_sync_runs",
    "oauth_state_nonces",
}


def test_company_brain_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'migration.db'}"
    config = _build_alembic_config(uri)

    command.upgrade(config, _REVISION)
    engine = sa.create_engine(uri)
    assert set(sa.inspect(engine).get_table_names()) >= _TABLES
    connection_indexes = {
        item["name"] for item in sa.inspect(engine).get_indexes("integration_connections")
    }
    assert "ix_integration_connections_provider" in connection_indexes
    assert sa.inspect(engine).get_pk_constraint("oauth_state_nonces")["constrained_columns"] == [
        "workspace_id",
        "nonce_sha256",
    ]

    command.downgrade(config, _PREVIOUS)
    assert _TABLES.isdisjoint(sa.inspect(engine).get_table_names())
    engine.dispose()


def test_integration_connections_mysql_ddl_omits_text_default() -> None:
    ddl = str(CreateTable(SqlIntegrationConnection.__table__).compile(dialect=mysql.dialect()))
    granted_scopes_definition = next(
        line for line in ddl.splitlines() if "granted_scopes_json" in line
    )

    assert "DEFAULT" not in granted_scopes_definition.upper()
