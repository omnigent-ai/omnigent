"""The Plan snapshot migration adds only nullable compressed metadata."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config


def test_plan_metadata_upgrade_and_downgrade(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'plan-migration.db'}"
    config = _build_alembic_config(uri)
    command.upgrade(config, "gb1b2c3d4e5f")
    engine = sa.create_engine(uri)
    try:
        assert "session_todos" not in {
            column["name"]
            for column in sa.inspect(engine).get_columns("omnigent_conversation_metadata")
        }
        command.upgrade(config, "9f6a21d47c83")
        column = next(
            column
            for column in sa.inspect(engine).get_columns("omnigent_conversation_metadata")
            if column["name"] == "session_todos"
        )
        assert column["nullable"]
        assert isinstance(column["type"], sa.LargeBinary)
        command.downgrade(config, "gb1b2c3d4e5f")
        assert "session_todos" not in {
            column["name"]
            for column in sa.inspect(engine).get_columns("omnigent_conversation_metadata")
        }
        assert sa.inspect(engine).has_table("conversation_items")
    finally:
        engine.dispose()
