"""The Name sort key migration keys every existing session and indexes the key."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module

import sqlalchemy as sa
from alembic import command

from omnigent.db.cockroachdb import _crdb_server_version, _prepare_crdb_schema_transaction
from omnigent.db.db_models import title_sort_key
from omnigent.db.utils import _build_alembic_config, get_or_create_engine

_PREVIOUS = "d0a1f879a01d"
_REVISION = "e413195738fb"
_MIGRATION = "omnigent.db.migrations.versions.e413195738fb_add_conversations_title_sort_key"
_INDEX = "ix_conversations_archived_title"


def _migrate(
    engine: sa.Engine, revision: str, alembic_command: Callable[..., None] = command.upgrade
) -> None:
    config = _build_alembic_config(engine.url.render_as_string(hide_password=False))
    with engine.connect() as connection:
        if engine.dialect.name == "cockroachdb":
            _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
        config.attributes["connection"] = connection
        alembic_command(config, revision)
        connection.commit()


def _keys(engine: sa.Engine) -> dict[tuple[int, bytes], str]:
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text("SELECT workspace_id, id, title_sort_key FROM conversations")
        )
        return {(row.workspace_id, bytes(row.id)): row.title_sort_key for row in rows}


def _conversation_ids(engine: sa.Engine) -> list[tuple[int, bytes]]:
    with engine.connect() as connection:
        rows = connection.execute(sa.text("SELECT workspace_id, id FROM conversations"))
        return [(row.workspace_id, bytes(row.id)) for row in rows]


def test_title_sort_key_migration_keys_every_session(db_uri: str) -> None:
    batch_size = import_module(_MIGRATION)._BATCH_SIZE
    engine = get_or_create_engine(db_uri)
    _migrate(engine, _PREVIOUS, command.downgrade)
    conversations = sa.Table("conversations", sa.MetaData(), autoload_with=engine)
    assert "title_sort_key" not in conversations.c
    titles = ["Alpha", "", " padded ", "Éclair", "\U0001f600 smile", "Q" * 600]
    original = {(0, bytes([index + 1]) * 16): title for index, title in enumerate(titles)}
    # Enough rows to cross batch boundaries, and a second workspace reusing an id.
    original.update(
        {(0, index.to_bytes(16, "big")): f"Batch {index}" for index in range(2 * batch_size + 1)}
    )
    original[(17, bytes([1]) * 16)] = "Other Workspace"
    legacy_id = bytes([99]) * 16
    try:
        with engine.begin() as connection:
            connection.execute(
                conversations.insert(),
                [
                    {
                        "workspace_id": workspace_id,
                        "id": row_id,
                        "root_conversation_id": row_id,
                        "created_at": 1,
                        "updated_at": 2,
                        "title": title,
                    }
                    for (workspace_id, row_id), title in original.items()
                ],
            )

        _migrate(engine, _REVISION)
        expected = {key: title_sort_key(title) for key, title in original.items()}
        assert _keys(engine) == expected
        assert expected[(0, bytes([6]) * 16)] == "0" + "q" * 511
        indexes = {
            index["name"]: index["column_names"]
            for index in sa.inspect(engine).get_indexes("conversations")
        }
        assert indexes[_INDEX] == ["workspace_id", "archived", "title_sort_key", "id"]

        # Builds that predate the column still insert; their rows list as untitled.
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO conversations "
                    "(workspace_id, id, root_conversation_id, created_at, updated_at, title) "
                    "VALUES (0, :id, :id, 1, 2, 'Legacy')"
                ),
                {"id": legacy_id},
            )
        assert _keys(engine)[(0, legacy_id)] == "1"

        # A rerun after a partial upgrade keeps the column and index and fixes keys.
        _migrate(engine, _PREVIOUS, command.stamp)
        _migrate(engine, _REVISION)
        assert _keys(engine) == {**expected, (0, legacy_id): "0legacy"}

        _migrate(engine, _PREVIOUS, command.downgrade)
        inspector = sa.inspect(engine)
        assert "title_sort_key" not in {c["name"] for c in inspector.get_columns("conversations")}
        assert _INDEX not in {index["name"] for index in inspector.get_indexes("conversations")}
        if engine.dialect.name == "sqlite":
            # The SQLite downgrade rebuilds the table around this DESC index.
            with engine.connect() as connection:
                parent_index = connection.execute(
                    sa.text("SELECT sql FROM sqlite_master WHERE name = :name"),
                    {"name": "idx_conversations_parent"},
                ).scalar_one()
            assert "created_at DESC" in parent_index and "id DESC" in parent_index
        assert set(_conversation_ids(engine)) == set(original) | {(0, legacy_id)}
    finally:
        _migrate(engine, "head")
