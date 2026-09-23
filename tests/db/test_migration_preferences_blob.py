"""Preference narrowing discards oversized rows and preserves fitting bytes."""

from __future__ import annotations

import io
import random
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations

from omnigent.db.compression import encode
from omnigent.db.utils import _build_alembic_config, get_or_create_engine
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

_PREVIOUS_REVISION = "jj1a2b3c4d5e"
_REVISION = "kk1a2b3c4d5e"
_MIGRATION = "omnigent.db.migrations.versions.kk1a2b3c4d5e_preferences_value_blob"


def _migrate(engine: sa.Engine, uri: str, revision: str, *, downgrade: bool = False) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        (command.downgrade if downgrade else command.upgrade)(config, revision)


def _values(engine: sa.Engine, table: sa.Table) -> dict[tuple[int, str, str], bytes | str]:
    with engine.connect() as connection:
        return {
            (row.workspace_id, row.user_id, row.key): row.value
            for row in connection.execute(table.select())
        }


def test_preferences_blob_migration_drops_only_oversized_rows(db_uri: str) -> None:
    engine = get_or_create_engine(db_uri)
    _migrate(engine, db_uri, _PREVIOUS_REVISION, downgrade=True)
    preferences = sa.Table("preferences", sa.MetaData(), autoload_with=engine)
    compressed_large = encode(random.Random(0).randbytes(100_000).hex())
    compressed_small = encode('"' + "x" * 100_000 + '"')
    assert compressed_large is not None and len(compressed_large) > 65_535
    assert compressed_small is not None and len(compressed_small) < 65_535
    original = {
        (0, "legacy", "project_order"): b'["' + b"a" * 32 + b'"]',
        (0, "boundary", "project_order"): b"x" * 65_535,
        (0, "boundary", "theme"): b"\x00\x00" + b"x" * 65_533,
        (17, "boundary", "project_order"): b"x" * 65_536,
        (0, "oversized", "project_order"): compressed_large,
        (0, "large-plaintext", "theme"): compressed_small,
        (0, "invalid", "theme"): b"\x00\x01invalid compression frame",
        (0, "empty", "theme"): b"",
    }
    try:
        with engine.begin() as connection:
            connection.execute(
                preferences.insert(),
                [
                    {"workspace_id": ws, "user_id": user, "key": key, "value": value}
                    for (ws, user, key), value in original.items()
                ],
            )
            if engine.dialect.name == "sqlite":
                # Legacy TEXT must be measured in bytes, including bytes after a NUL.
                for user, value in [
                    ("legacy-text", "[]"),
                    ("multibyte", "é" * 32_768),
                    ("nul-text", "\x00" + "x" * 65_535),
                ]:
                    connection.execute(
                        sa.text(
                            'INSERT INTO preferences (workspace_id, user_id, "key", value) '
                            "VALUES (0, :user, 'theme', :value)"
                        ),
                        {"user": user, "value": value},
                    )

        expected: dict[tuple[int, str, str], bytes | str] = {
            key: value for key, value in original.items() if len(value) <= 65_535
        }
        if engine.dialect.name == "sqlite":
            expected[(0, "legacy-text", "theme")] = "[]"
        _migrate(engine, db_uri, _REVISION)
        assert _values(engine, preferences) == expected
        column = next(
            c for c in sa.inspect(engine).get_columns("preferences") if c["name"] == "value"
        )
        assert not column["nullable"]
        if engine.dialect.name == "mysql":
            assert column["type"].compile(dialect=engine.dialect) == "BLOB"
        store = SqlAlchemyProjectStore(db_uri)
        assert store.get_order(user_id="legacy") == ["a" * 32]
        assert store.get_order_preference(user_id="oversized") == {
            "sort_mode": "alphabetical",
            "ordered_project_ids": None,
        }

        _migrate(engine, db_uri, _PREVIOUS_REVISION, downgrade=True)
        assert _values(engine, preferences) == expected
        with engine.begin() as connection:
            connection.execute(
                preferences.insert().values(
                    workspace_id=0, user_id="after-downgrade", key="theme", value=b"x" * 65_536
                )
            )
        _migrate(engine, db_uri, _REVISION)
        assert _values(engine, preferences) == expected
    finally:
        _migrate(engine, db_uri, "head")


@pytest.mark.parametrize("dialect_name", ["mysql", "postgresql", "sqlite"])
def test_preferences_blob_migration_sql(dialect_name: str) -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name=dialect_name,
        opts={"as_sql": True, "literal_binds": True, "output_buffer": output},
    )
    migration = import_module(_MIGRATION)
    with Operations.context(context):
        migration.upgrade()
    sql = output.getvalue()
    assert "DELETE FROM preferences WHERE" in sql
    assert "> 65535" in sql
    if dialect_name == "mysql":
        alter = "ALTER TABLE preferences MODIFY value BLOB NOT NULL"
        assert sql.index("DELETE FROM preferences") < sql.index(alter)
        assert sql.index("STRICT_ALL_TABLES") < sql.index(alter)
        assert sql.index(alter) < sql.index(
            "SET SESSION sql_mode = @omnigent_preferences_sql_mode"
        )
    elif dialect_name == "sqlite":
        assert "length(CAST(preferences.value AS BLOB))" in sql
        assert "ALTER TABLE" not in sql
    else:
        assert "octet_length(preferences.value)" in sql
        assert "ALTER TABLE" not in sql

    output.truncate(0)
    output.seek(0)
    with Operations.context(context):
        migration.downgrade()
    sql = output.getvalue()
    if dialect_name == "mysql":
        assert "ALTER TABLE preferences MODIFY value MEDIUMBLOB NOT NULL" in sql
    else:
        assert not sql


def test_mysql_narrowing_does_not_truncate_a_concurrent_oversized_write(db_uri: str) -> None:
    engine = get_or_create_engine(db_uri)
    if engine.dialect.name != "mysql":
        pytest.skip("MySQL narrowing and SQL modes")
    _migrate(engine, db_uri, _PREVIOUS_REVISION, downgrade=True)
    oversized = b"x" * 65_536

    def race_write(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("ALTER TABLE preferences MODIFY value BLOB"):
            conn.execute(
                sa.text(
                    "INSERT INTO preferences (workspace_id, user_id, `key`, value) "
                    "VALUES (0, 'racing-writer', 'theme', :value)"
                ),
                {"value": oversized},
            )

    migration = import_module(_MIGRATION)
    try:
        with engine.connect() as connection:
            old_mode = connection.scalar(sa.text("SELECT @@SESSION.sql_mode"))
            connection.execute(sa.text("SET SESSION sql_mode = ''"))
            sa.event.listen(connection, "before_cursor_execute", race_write)
            try:
                with Operations.context(MigrationContext.configure(connection)):
                    with pytest.raises(sa.exc.DBAPIError):
                        migration.upgrade()
                assert connection.scalar(sa.text("SELECT @@SESSION.sql_mode")) == ""
                assert connection.scalar(sa.text("SELECT value FROM preferences")) == oversized
            finally:
                sa.event.remove(connection, "before_cursor_execute", race_write)
                connection.execute(sa.text("SET SESSION sql_mode = :mode"), {"mode": old_mode})
    finally:
        _migrate(engine, db_uri, "head")
