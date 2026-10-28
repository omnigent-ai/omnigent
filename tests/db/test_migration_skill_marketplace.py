"""The skills marketplace tables: created on upgrade, removed on downgrade, safe to re-run."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, get_or_create_engine

PREVIOUS = "b2e4f6a8c013"
TABLES = {"skill_sources", "installed_skills"}
INDEXES = {
    "skill_sources": {"ix_skill_sources_order"},
    "installed_skills": {"ix_installed_skills_owner_order", "ix_installed_skills_owner_name"},
}


def _schema(connection: sa.Connection) -> tuple[set[str], dict[str, set[str]]]:
    inspector = sa.inspect(connection)
    tables = TABLES & set(inspector.get_table_names())
    indexes = {t: {ix["name"] for ix in inspector.get_indexes(t)} for t in tables}
    return tables, indexes


def test_upgrade_creates_the_tables_and_downgrade_removes_them(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'skills-migration.db'}"
    engine = get_or_create_engine(uri)  # migrates to head
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        tables, indexes = _schema(connection)
        assert tables == TABLES
        for table, expected in INDEXES.items():
            assert expected <= indexes[table], (table, indexes[table])
        columns = {c["name"] for c in sa.inspect(connection).get_columns("installed_skills")}
        assert {
            "workspace_id",
            "id",
            "name",
            "scope",
            "owner_user_id",
            "bundle_location",
            "enabled",
        } <= columns

        command.downgrade(config, PREVIOUS)
        assert _schema(connection)[0] == set()

        # Re-running the upgrade is safe, and so is upgrading over tables that already exist.
        command.upgrade(config, "head")
        assert _schema(connection)[0] == TABLES
        command.downgrade(config, PREVIOUS)
        connection.execute(sa.text("CREATE TABLE skill_sources (workspace_id INTEGER, id BLOB)"))
        command.upgrade(config, "head")
        tables, indexes = _schema(connection)
        assert tables == TABLES and INDEXES["installed_skills"] <= indexes["installed_skills"]
