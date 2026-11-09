"""Tests for the inbox_items migration."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_REVISION = "fcc79fe2b985"
_PREVIOUS = "f1c7a9e2b406"


def _migrate(uri: str, engine: sa.Engine, revision: str, *, down: bool = False) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        (command.downgrade if down else command.upgrade)(config, revision)


def test_upgrade_creates_table_and_downgrade_drops_it(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'inbox.db'}"
    engine = sa.create_engine(uri)

    _migrate(uri, engine, _REVISION)
    inspector = sa.inspect(engine)
    assert "inbox_items" in inspector.get_table_names()
    assert inspector.get_pk_constraint("inbox_items")["constrained_columns"] == [
        "workspace_id",
        "id",
    ]
    assert inspector.get_foreign_keys("inbox_items") == []
    indexes = {
        index["name"]: (index["column_names"], bool(index["unique"]))
        for index in inspector.get_indexes("inbox_items")
    }
    assert indexes == {
        "ix_inbox_items_user_created": (["workspace_id", "user_id", "created_at", "id"], False),
        "ix_inbox_items_user_state_created": (
            ["workspace_id", "user_id", "state", "created_at", "id"],
            False,
        ),
        "uq_inbox_items_user_dedupe_key": (["workspace_id", "user_id", "dedupe_key"], True),
        "uq_inbox_items_session_slot": (
            ["workspace_id", "user_id", "source_session_id", "session_slot"],
            True,
        ),
    }
    # The application supplies every value on this new table.
    assert all(column["default"] is None for column in inspector.get_columns("inbox_items"))

    _migrate(uri, engine, _PREVIOUS, down=True)
    assert "inbox_items" not in sa.inspect(engine).get_table_names()

    engine.dispose()
    clear_engine_cache()
