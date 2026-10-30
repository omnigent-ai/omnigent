"""Tests for the conversation metadata sidebar live-state migration (c7cd88f24c1c).

The migration and the ORM model both add ``background_work_running`` and
``blocked_on`` as nullable columns with no database default. Existing rows keep
their values and read ``NULL``; downgrading drops both columns and keeps the rows.
SQLite ignores VARCHAR lengths, so there a CHECK bounds ``blocked_on`` to 512 bytes.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.db_models import OmnigentBase, SqlConversationMetadata
from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_PREVIOUS = "d0a1f879a01d"
_REVISION = "c7cd88f24c1c"
_TABLE = "omnigent_conversation_metadata"
_SESSION_ID = bytes.fromhex("0123456789abcdef0123456789abcdef")


def _migrate(uri: str, engine: sa.Engine, step: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        getattr(command, step)(config, revision)


def _columns(engine: sa.Engine) -> dict[str, dict[str, object]]:
    return {column["name"]: column for column in sa.inspect(engine).get_columns(_TABLE)}


def _insert_blocked_on(engine: sa.Engine, row_id: int, reason: str | None) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                f"INSERT INTO {_TABLE} (workspace_id, id, kind, git_branch, blocked_on) "
                "VALUES (0, :id, 1, 'main', :reason)"
            ),
            {"id": row_id.to_bytes(16, "big"), "reason": reason},
        )


def _assert_blocked_on_bytes_are_bounded(engine: sa.Engine) -> None:
    # 256 two-byte characters fill the 512-byte bound exactly.
    for row_id, reason in enumerate((None, "permission prompt", "\u00e9" * 256), start=1):
        _insert_blocked_on(engine, row_id, reason)
    for size in (513, 16_384):
        with pytest.raises(sa.exc.IntegrityError):
            _insert_blocked_on(engine, size, "a" * size)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[tuple[str, sa.Engine]]:
    uri = f"sqlite:///{tmp_path / 'metadata.db'}"
    engine = sa.create_engine(uri)
    try:
        yield uri, engine
    finally:
        engine.dispose()
        clear_engine_cache()


def test_model_declares_nullable_columns_without_defaults() -> None:
    table = SqlConversationMetadata.__table__
    assert isinstance(table.c.background_work_running.type, sa.Boolean)
    assert isinstance(table.c.blocked_on.type, sa.String)
    assert table.c.blocked_on.type.length == 128
    for column in (table.c.background_work_running, table.c.blocked_on):
        assert column.nullable
        assert column.server_default is None


def test_upgrade_adds_the_columns_and_keeps_rows(database: tuple[str, sa.Engine]) -> None:
    uri, engine = database
    _migrate(uri, engine, "upgrade", _PREVIOUS)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                f"INSERT INTO {_TABLE} (workspace_id, id, kind, git_branch) "
                "VALUES (0, :id, 1, 'main')"
            ),
            {"id": _SESSION_ID},
        )

    _migrate(uri, engine, "upgrade", _REVISION)

    columns = _columns(engine)
    assert isinstance(columns["background_work_running"]["type"], sa.Boolean)
    assert isinstance(columns["blocked_on"]["type"], sa.String)
    for name in ("background_work_running", "blocked_on"):
        assert columns[name]["nullable"] is True
        assert columns[name]["default"] is None
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(f"SELECT git_branch, background_work_running, blocked_on FROM {_TABLE}")
        ).one()
    assert tuple(row) == ("main", None, None)
    _assert_blocked_on_bytes_are_bounded(engine)


def test_model_built_schema_bounds_blocked_on_bytes(database: tuple[str, sa.Engine]) -> None:
    _, engine = database
    OmnigentBase.metadata.create_all(engine, tables=[SqlConversationMetadata.__table__])

    _assert_blocked_on_bytes_are_bounded(engine)


def test_downgrade_drops_the_columns_and_keeps_rows(database: tuple[str, sa.Engine]) -> None:
    uri, engine = database
    _migrate(uri, engine, "upgrade", _REVISION)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                f"INSERT INTO {_TABLE} "
                "(workspace_id, id, kind, git_branch, background_work_running, blocked_on) "
                "VALUES (0, :id, 1, 'main', 1, 'permission prompt')"
            ),
            {"id": _SESSION_ID},
        )

    _migrate(uri, engine, "downgrade", _PREVIOUS)

    columns = _columns(engine)
    assert "background_work_running" not in columns
    assert "blocked_on" not in columns
    with engine.connect() as conn:
        assert conn.execute(sa.text(f"SELECT git_branch FROM {_TABLE}")).scalar_one() == "main"
