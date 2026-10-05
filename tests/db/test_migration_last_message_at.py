"""Migration coverage for the persisted visible-message watermark."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, _create_engine, clear_engine_cache

_REVISION_BEFORE_WATERMARK = "mm1a2b3c4d5e"


def test_last_message_at_is_added_and_conservatively_backfilled(tmp_path: Path) -> None:
    """Existing sessions retain their prior unread baseline after upgrade."""
    uri = f"sqlite:///{tmp_path / 'watermark.db'}"
    engine = _create_engine(uri)
    config = _build_alembic_config(uri)
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, _REVISION_BEFORE_WATERMARK)
            conversation_id = b"\x42" * 16
            connection.execute(
                sa.text(
                    "INSERT INTO conversations "
                    "(workspace_id, id, created_at, updated_at, root_conversation_id) "
                    "VALUES (:workspace_id, :id, :created_at, :updated_at, :root)"
                ),
                {
                    "workspace_id": 0,
                    "id": conversation_id,
                    "created_at": 100,
                    "updated_at": 900,
                    "root": conversation_id,
                },
            )
            connection.commit()

        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

        columns = {column["name"] for column in sa.inspect(engine).get_columns("conversations")}
        assert "last_message_at" in columns
        with engine.connect() as connection:
            value = connection.execute(
                sa.text(
                    "SELECT last_message_at FROM conversations "
                    "WHERE workspace_id = :workspace_id AND id = :id"
                ),
                {"workspace_id": 0, "id": conversation_id},
            ).scalar_one()
        assert value == 900
    finally:
        engine.dispose()
        clear_engine_cache()
