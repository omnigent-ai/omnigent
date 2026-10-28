"""The skills marketplace tables: created, removed, repaired, and bounded on every write."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlInstalledSkill, SqlSkillSource
from omnigent.db.utils import _build_alembic_config, get_or_create_engine

PREVIOUS = "d0a1f879a01d"
TABLES = {"skill_sources", "installed_skills"}
INDEXES = {
    "skill_sources": {"ix_skill_sources_order", "ix_skill_sources_identity"},
    "installed_skills": {"ux_installed_skills_identity", "ix_installed_skills_owner_order"},
}


def _schema(connection: sa.Connection) -> tuple[set[str], dict[str, set[str]]]:
    inspector = sa.inspect(connection)
    tables = TABLES & set(inspector.get_table_names())
    indexes = {t: {ix["name"] for ix in inspector.get_indexes(t)} for t in tables}
    return tables, indexes


def _assert_at_head(connection: sa.Connection) -> None:
    tables, indexes = _schema(connection)
    assert tables == TABLES
    for table, expected in INDEXES.items():
        assert expected <= indexes[table], (table, indexes[table])


def test_upgrade_creates_the_tables_and_downgrade_removes_them(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'skills-migration.db'}"
    engine = get_or_create_engine(uri)  # migrates to head
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        _assert_at_head(connection)
        inspector = sa.inspect(connection)
        unique = {ix["name"]: ix["unique"] for ix in inspector.get_indexes("installed_skills")}
        assert (
            unique["ux_installed_skills_identity"]
            and not unique["ix_installed_skills_owner_order"]
        )
        columns = {c["name"]: c for c in inspector.get_columns("installed_skills")}
        assert {"workspace_id", "id", "name", "scope", "owner_user_id", "enabled"} <= set(columns)
        assert columns["owner_user_id"]["nullable"] is False

        command.downgrade(config, PREVIOUS)
        assert _schema(connection)[0] == set()
        # Re-running the upgrade is safe.
        command.upgrade(config, "head")
        _assert_at_head(connection)

        # A run interrupted between a table and its index: the next run adds the
        # missing index, and the downgrade still removes everything.
        connection.execute(sa.text("DROP INDEX ix_skill_sources_order"))
        command.stamp(config, PREVIOUS)
        command.upgrade(config, "head")
        _assert_at_head(connection)
        command.downgrade(config, PREVIOUS)
        assert _schema(connection)[0] == set()

        # A pre-existing table with a different shape is refused, not stamped over.
        connection.execute(sa.text("CREATE TABLE skill_sources (workspace_id INTEGER, id BLOB)"))
        with pytest.raises(RuntimeError, match="skill_sources already exists"):
            command.upgrade(config, "head")


def test_declared_lengths_are_enforced_on_every_orm_write(db_uri: str) -> None:
    # SQLite ignores String(n); the models refuse an oversized value before it is written.
    with Session(get_or_create_engine(db_uri)) as session:
        with pytest.raises(ValueError, match="url is longer than 255"):
            SqlSkillSource(
                id=uuid.uuid4().hex,
                url="https://github.com/" + "a" * 20_000,
                ref=None,
                name="x",
                created_by=None,
                created_at=0,
            )
        row = SqlInstalledSkill(
            id=uuid.uuid4().hex,
            name="review",
            display_name=None,
            description="d" * 1024,
            source_url="https://github.com/acme/skills.git",
            ref=None,
            commit=None,
            path="skills/review",
            scope=1,
            owner_user_id="",
            bundle_location="skills/review.tar.gz",
            enabled=True,
            installed_by=None,
            installed_at=0,
        )
        session.add(row)
        session.flush()
        with pytest.raises(ValueError, match="description is longer than 1024"):
            row.description = "名" * 1025
        session.rollback()
