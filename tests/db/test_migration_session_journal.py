"""Tests for the session journal migration (5d872cd6632a)."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.db_models import (
    JOURNAL_PAYLOAD_MAX_STORED_BYTES,
    SqlSessionJournalEntry,
    SqlSessionJournalHead,
)
from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_ENTRIES = "session_journal_entries"
_HEADS = "session_journal_heads"


def _migrate(uri: str, engine: sa.Engine, revision: str, *, down: bool = False) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        (command.downgrade if down else command.upgrade)(config, revision)


def _entry(position: int, payload: bytes) -> dict[str, object]:
    return {
        "workspace_id": 0,
        "conversation_id": bytes.fromhex("a" * 32),
        "position": position,
        "created_at": 1_762_534_860,
        "id": bytes.fromhex(f"{position:032x}"),
        "kind": 2,
        "idempotency_key": f"tool:call_{position}",
        "payload": payload,
    }


def test_upgrade_matches_models_and_downgrade_drops_tables(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'journal.db'}"
    engine = sa.create_engine(uri)
    _migrate(uri, engine, "head")
    inspector = sa.inspect(engine)

    for model in (SqlSessionJournalEntry, SqlSessionJournalHead):
        table = model.__table__
        columns = {column["name"]: column for column in inspector.get_columns(table.name)}
        assert list(columns) == [column.name for column in table.columns]
        for column in table.columns:
            assert columns[column.name]["nullable"] is column.nullable, column.name
            assert columns[column.name]["default"] is None, column.name
        assert inspector.get_pk_constraint(table.name)["constrained_columns"] == [
            column.name for column in table.primary_key.columns
        ]
        indexes = {index["name"]: index for index in inspector.get_indexes(table.name)}
        assert {name: index["column_names"] for name, index in indexes.items()} == {
            index.name: [column.name for column in index.columns] for index in table.indexes
        }
        assert not any(index["unique"] for index in indexes.values())
        assert inspector.get_unique_constraints(table.name) == []

    assert inspector.get_pk_constraint(_ENTRIES)["constrained_columns"] == [
        "workspace_id",
        "conversation_id",
        "position",
        "created_at",
    ]
    for index in inspector.get_indexes(_ENTRIES):
        assert index["column_names"][-2:] == ["position", "created_at"], index["name"]

    _migrate(uri, engine, "1ad1db53f99d", down=True)
    assert not {_ENTRIES, _HEADS} & set(sa.inspect(engine).get_table_names())
    engine.dispose()
    clear_engine_cache()


def test_payload_check_caps_stored_bytes(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'journal.db'}"
    engine = sa.create_engine(uri)
    _migrate(uri, engine, "head")
    entries = sa.Table(_ENTRIES, sa.MetaData(), autoload_with=engine)
    with engine.begin() as conn:
        conn.execute(entries.insert(), _entry(0, b"x" * JOURNAL_PAYLOAD_MAX_STORED_BYTES))
    with pytest.raises(sa.exc.IntegrityError, match="ck_session_journal_entries_payload_size"):
        with engine.begin() as conn:
            conn.execute(
                entries.insert(), _entry(1, b"x" * (JOURNAL_PAYLOAD_MAX_STORED_BYTES + 1))
            )
    engine.dispose()
    clear_engine_cache()
