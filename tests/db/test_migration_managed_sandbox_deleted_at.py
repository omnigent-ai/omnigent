"""Tests for the managed sandbox logical-deletion migration."""

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


def test_upgrade_adds_hosts_deleted_at_and_downgrade_removes_it(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'managed-sandbox-deleted-at.db'}"
    engine = sa.create_engine(uri)

    _migrate(uri, engine, "gb1b2c3d4e5f")
    assert "deleted_at" not in {
        column["name"] for column in sa.inspect(engine).get_columns("hosts")
    }

    _migrate(uri, engine, "gc1b2c3d4e5f")
    columns = {column["name"]: column for column in sa.inspect(engine).get_columns("hosts")}
    assert columns["deleted_at"]["nullable"] is True
    assert isinstance(columns["deleted_at"]["type"], sa.Integer)

    _downgrade(uri, engine, "gb1b2c3d4e5f")
    assert "deleted_at" not in {
        column["name"] for column in sa.inspect(engine).get_columns("hosts")
    }

    engine.dispose()
    clear_engine_cache()
