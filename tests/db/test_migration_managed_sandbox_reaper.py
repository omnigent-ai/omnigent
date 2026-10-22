"""Tests for the managed sandbox pending-termination migration."""

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


def test_upgrade_adds_pending_termination_column_and_downgrade_removes_it(
    tmp_path: Path,
) -> None:
    uri = f"sqlite:///{tmp_path / 'managed-sandbox-reaper.db'}"
    engine = sa.create_engine(uri)

    _migrate(uri, engine, "ga1b2c3d4e5f")
    assert "terminating_sandbox_id" not in {
        column["name"] for column in sa.inspect(engine).get_columns("hosts")
    }

    _migrate(uri, engine, "gb1b2c3d4e5f")
    columns = {column["name"]: column for column in sa.inspect(engine).get_columns("hosts")}
    assert columns["terminating_sandbox_id"]["nullable"] is True
    assert columns["terminating_sandbox_id"]["type"].length == 256

    _downgrade(uri, engine, "ga1b2c3d4e5f")
    assert "terminating_sandbox_id" not in {
        column["name"] for column in sa.inspect(engine).get_columns("hosts")
    }

    engine.dispose()
    clear_engine_cache()
