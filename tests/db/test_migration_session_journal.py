"""Tests for the session journal migration (5d872cd6632a)."""

from __future__ import annotations

from importlib import import_module
from io import StringIO
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import mysql

from omnigent.db.db_models import SqlSessionJournalEntry, SqlSessionJournalHead, SqlWorkflow
from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_ENTRIES = "session_journal_entries"
_HEADS = "session_journal_heads"


def test_mysql_journal_content_is_bounded_bytes_the_store_encodes() -> None:
    migration = import_module("omnigent.db.migrations.versions.5d872cd6632a_add_session_journal")
    dialect = mysql.dialect()
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        migration.upgrade()
    column = SqlSessionJournalEntry.__table__.c.content
    assert str(column.type.compile(dialect=dialect)) == "VARBINARY(16383)"
    assert "content VARBINARY(16383) NOT NULL" in output.getvalue()
    # The journal store encodes content itself, so the column passes bytes through.
    bind = column.type.dialect_impl(dialect).bind_processor(dialect)
    stored = b"\x00\x00{}"
    assert bind is not None and bind(stored) == stored
    with pytest.raises(ValueError):
        bind(stored + b"x" * 16_383)


@pytest.mark.parametrize(
    ("revision", "column", "limit"),
    [
        ("ce12af3da168_add_workflows", SqlWorkflow.__table__.c.plan, 16_383),
        ("f7c2d8a4b901_workflow_description_envelope", SqlWorkflow.__table__.c.description, 2000),
    ],
)
def test_mysql_payload_types_are_bounded_and_keep_the_codec(
    revision: str, column: sa.Column, limit: int
) -> None:
    migration = import_module(f"omnigent.db.migrations.versions.{revision}")
    dialect = mysql.dialect()
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql", opts={"as_sql": True, "output_buffer": output}
    )
    if column.name == "description":
        # The data-copy migration needs an online connection; compile its target column.
        output.write(
            str(
                sa.schema.CreateColumn(
                    sa.Column(column.name, migration._BINARY, nullable=False)
                ).compile(dialect=dialect)
            )
        )
    else:
        with Operations.context(context):
            migration.upgrade()
    assert str(column.type.compile(dialect=dialect)) == f"VARBINARY({limit})"
    assert f"{column.name} VARBINARY({limit}) NOT NULL" in output.getvalue()
    codec = column.type.dialect_impl(dialect)
    bind = codec.bind_processor(dialect)
    read = codec.result_processor(dialect, None)
    value = b'\x00\x00{"steps": []}'
    assert bind is not None and read is not None
    assert bind(value) == value
    assert read(bind(value)) == value
    with pytest.raises(ValueError):
        bind(value + b"x" * limit)


def _migrate(uri: str, engine: sa.Engine, revision: str, *, down: bool = False) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        (command.downgrade if down else command.upgrade)(config, revision)


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
        columns = index["column_names"]
        # Every index holds the whole primary key, so index order is a total order.
        assert {"workspace_id", "conversation_id", "position", "created_at"} <= set(columns)
        # One session's entries are read in position order; a workflow's runs,
        # across sessions, by time.
        if "workflow_id" not in columns:
            assert columns[-2:] == ["position", "created_at"], index["name"]

    _migrate(uri, engine, "5d872cd6632a@a7d4c2e9f610", down=True)
    rolled_back = sa.inspect(engine)
    tables = set(rolled_back.get_table_names())
    assert not {_ENTRIES, _HEADS} & tables
    assert {"inbox_items", "scheduled_task_shares"} <= tables
    assert "kind" in {column["name"] for column in rolled_back.get_columns("scheduled_tasks")}
    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalars().all() == ["27d16059fab9"]
    engine.dispose()
    clear_engine_cache()
