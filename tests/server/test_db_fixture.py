"""Server database copies isolate both data and schema changes."""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa

from omnigent.db.utils import get_or_create_engine
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.mark.parametrize("copy_number", [0, 1])
def test_server_database_copy_is_isolated(
    db_uri: str, _sqlite_db_template: Path | None, copy_number: int
) -> None:
    engine = get_or_create_engine(db_uri)
    if engine.dialect.name != "sqlite":
        assert _sqlite_db_template is None
        return

    assert _sqlite_db_template is not None
    assert not sa.inspect(engine).has_table("fixture_schema_probe")
    with engine.begin() as connection:
        assert connection.execute(sa.text("SELECT COUNT(*) FROM conversations")).scalar_one() == 0
        connection.execute(sa.text("CREATE TABLE fixture_schema_probe (value INTEGER)"))
    SqlAlchemyConversationStore(db_uri).create_conversation(title=f"copy-{copy_number}")
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT COUNT(*) FROM conversations")).scalar_one() == 1
        revision = connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"

    with contextlib.closing(
        sqlite3.connect(f"{_sqlite_db_template.as_uri()}?mode=ro", uri=True)
    ) as seed:
        assert seed.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
        assert seed.execute("SELECT version_num FROM alembic_version").fetchone()[0] == revision
        assert (
            seed.execute(
                "SELECT name FROM sqlite_master WHERE name = 'fixture_schema_probe'"
            ).fetchone()
            is None
        )
