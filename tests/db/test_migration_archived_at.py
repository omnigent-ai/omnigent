"""Migration coverage for the stable archive timestamp."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache


def test_archived_at_column_and_index_are_present(tmp_path) -> None:
    uri = f"sqlite:///{tmp_path / 'archive.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")

    inspector = sa.inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("conversations")}
    indexes = {index["name"] for index in inspector.get_indexes("conversations")}
    assert "archived_at" in columns
    assert "ix_conversations_archived_archived_at" in indexes

    engine.dispose()
    clear_engine_cache()


def test_archived_at_backfill_prefers_valid_legacy_transition_time(tmp_path) -> None:
    uri = f"sqlite:///{tmp_path / 'archive-backfill.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "gb1b2c3d4e5f")

        metadata = sa.MetaData()
        conversations = sa.Table("conversations", metadata, autoload_with=connection)
        labels = sa.Table("conversation_labels", metadata, autoload_with=connection)
        ids = {
            "legacy": bytes.fromhex("01" * 16),
            "invalid": bytes.fromhex("02" * 16),
            "missing": bytes.fromhex("03" * 16),
            "active": bytes.fromhex("04" * 16),
        }
        connection.execute(
            conversations.insert(),
            [
                {
                    "id": conversation_id,
                    "root_conversation_id": conversation_id,
                    "created_at": 100,
                    "updated_at": updated_at,
                    "archived": archived,
                }
                for conversation_id, updated_at, archived in (
                    (ids["legacy"], 2_000, True),
                    (ids["invalid"], 3_000, True),
                    (ids["missing"], 3_500, True),
                    (ids["active"], 4_000, False),
                )
            ],
        )
        connection.execute(
            labels.insert(),
            [
                {
                    "conversation_id": ids["legacy"],
                    "key": "omnigent.archived_at",
                    "value": "1000",
                    "updated_at": 1_000,
                },
                {
                    "conversation_id": ids["invalid"],
                    "key": "omnigent.archived_at",
                    "value": "not-a-timestamp",
                    "updated_at": 1_000,
                },
                {
                    "conversation_id": ids["active"],
                    "key": "omnigent.archived_at",
                    "value": "500",
                    "updated_at": 500,
                },
            ],
        )

        command.upgrade(config, "head")
        migrated = sa.Table("conversations", sa.MetaData(), autoload_with=connection)
        values = dict(connection.execute(sa.select(migrated.c.id, migrated.c.archived_at)).all())

    assert values[ids["legacy"]] == 1_000
    assert values[ids["invalid"]] == 3_000
    assert values[ids["missing"]] == 3_500
    assert values[ids["active"]] is None

    engine.dispose()
    clear_engine_cache()
