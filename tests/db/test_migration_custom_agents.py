"""Tests for the owner-scoped custom Agent library migration."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache


def _migrate(uri: str, engine: sa.Engine, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def _downgrade(uri: str, engine: sa.Engine, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, revision)


def test_upgrade_creates_owner_scoped_library_and_downgrade_removes_it(
    tmp_path: Path,
) -> None:
    uri = f"sqlite:///{tmp_path / 'custom-agents.db'}"
    engine = sa.create_engine(uri)

    _migrate(uri, engine, "gb1b2c3d4e5f")
    assert "custom_agents" not in sa.inspect(engine).get_table_names()

    _migrate(uri, engine, "c7a9e2f4b610")
    inspector = sa.inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns("custom_agents")}
    assert set(columns) == {
        "workspace_id",
        "id",
        "owner_id",
        "name",
        "description",
        "harness",
        "model",
        "bundle_location",
        "version",
        "created_at",
        "updated_at",
        "deleted_at",
    }
    assert set(inspector.get_pk_constraint("custom_agents")["constrained_columns"]) == {
        "workspace_id",
        "id",
    }
    indexes = {index["name"]: index for index in inspector.get_indexes("custom_agents")}
    assert indexes["ix_custom_agents_owner"]["column_names"] == [
        "workspace_id",
        "owner_id",
        "deleted_at",
    ]

    _downgrade(uri, engine, "gb1b2c3d4e5f")
    assert "custom_agents" not in sa.inspect(engine).get_table_names()

    engine.dispose()
    clear_engine_cache()
