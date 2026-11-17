"""Tests for the scheduled_tasks agent index migration."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_REVISION = "b4e2d7a91c35"
_PREVIOUS = "fcc79fe2b985"


def _migrate(uri: str, engine: sa.Engine, revision: str, *, down: bool = False) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        (command.downgrade if down else command.upgrade)(config, revision)


def _indexes(engine: sa.Engine) -> dict[str, list[str | None]]:
    return {
        index["name"]: index["column_names"]
        for index in sa.inspect(engine).get_indexes("scheduled_tasks")
        if index["name"] is not None
    }


def test_upgrade_adds_the_index_and_downgrade_drops_it(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'tasks.db'}"
    engine = sa.create_engine(uri)

    _migrate(uri, engine, _REVISION)
    assert _indexes(engine)["ix_scheduled_tasks_agent"] == ["workspace_id", "agent_id", "id"]

    _migrate(uri, engine, _PREVIOUS, down=True)
    assert "ix_scheduled_tasks_agent" not in _indexes(engine)

    engine.dispose()
    clear_engine_cache()
