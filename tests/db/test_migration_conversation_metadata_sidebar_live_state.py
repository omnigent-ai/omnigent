"""Tests for the conversation metadata sidebar live-state migration (c7cd88f24c1c).

The migration and the ORM model both add ``background_work_running``,
``background_work_reported_at`` and ``blocked_on`` as nullable columns with no
database default. Existing rows keep their values and read ``NULL``; downgrading
drops the columns and keeps the rows.
SQLite does not enforce declared types, so there CHECKs bound ``blocked_on`` to
512 bytes, keep ``background_work_running`` 0 or 1 and keep
``background_work_reported_at`` an integer.
Cloudflare D1 serves SQLite under its own dialect name, so every database test
also runs this migration and the model against a SQLite file registered under
that name. The earlier migrations it builds on run as plain SQLite.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.ddl.sqlite import SQLiteImpl
from sqlalchemy.dialects import registry
from sqlalchemy.dialects.sqlite.pysqlite import SQLiteDialect_pysqlite

from omnigent.db.db_models import OmnigentBase, SqlConversationMetadata
from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_PREVIOUS = "d0a1f879a01d"
_REVISION = "c7cd88f24c1c"
_TABLE = "omnigent_conversation_metadata"
_SESSION_ID = bytes.fromhex("0123456789abcdef0123456789abcdef")


class _D1OnSqlite(SQLiteDialect_pysqlite):
    """A local SQLite file under Cloudflare D1's dialect name, which is what D1 runs."""

    name = "cloudflare_d1"
    supports_statement_cache = True


class _D1MigrationImpl(SQLiteImpl):
    """Alembic's SQLite behavior for the D1 dialect name."""

    __dialect__ = "cloudflare_d1"


registry.register("cloudflare_d1.local_sqlite", __name__, "_D1OnSqlite")


def _migrate(uri: str, engine: sa.Engine, step: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        getattr(command, step)(config, revision)


def _columns(engine: sa.Engine) -> dict[str, dict[str, object]]:
    return {column["name"]: column for column in sa.inspect(engine).get_columns(_TABLE)}


def _insert(engine: sa.Engine, row_id: int, column: str, value: object) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                f"INSERT INTO {_TABLE} (workspace_id, id, kind, git_branch, {column}) "
                "VALUES (0, :id, 1, 'main', :value)"
            ),
            {"id": row_id.to_bytes(16, "big"), "value": value},
        )


def _assert_blocked_on_bytes_are_bounded(engine: sa.Engine) -> None:
    # 256 two-byte characters fill the 512-byte bound exactly.
    for row_id, reason in enumerate((None, "permission prompt", "\u00e9" * 256), start=1):
        _insert(engine, row_id, "blocked_on", reason)
    for size in (513, 16_384):
        with pytest.raises(sa.exc.IntegrityError):
            _insert(engine, size, "blocked_on", "a" * size)


def _assert_holds_only(engine: sa.Engine, column: str, start: int, *accepted: object) -> None:
    """Store each accepted value of *column*; 16 KB of text or of a blob fails its CHECK."""
    for row_id, value in enumerate(accepted, start=start):
        _insert(engine, row_id, column, value)
    for row_id, value in enumerate(("a" * 16_384, b"\x00" * 16_384), start=start + 50):
        with pytest.raises(sa.exc.IntegrityError):
            _insert(engine, row_id, column, value)


def _assert_flag_and_report_time_are_typed(engine: sa.Engine) -> None:
    _assert_holds_only(engine, "background_work_running", 100, None, False, True)
    _assert_holds_only(engine, "background_work_reported_at", 200, None, 1_793_300_000_000_000_000)


@pytest.fixture(params=["sqlite", "cloudflare_d1+local_sqlite"])
def database(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[tuple[str, sa.Engine]]:
    """
    One SQLite file under the dialect being tested, already migrated to the
    revision before this one as plain SQLite.
    """
    path = tmp_path / "metadata.db"
    base_uri = f"sqlite:///{path}"
    base = sa.create_engine(base_uri)
    uri = f"{request.param}:///{path}"
    engine = sa.create_engine(uri)
    try:
        _migrate(base_uri, base, "upgrade", _PREVIOUS)
        yield uri, engine
    finally:
        base.dispose()
        engine.dispose()
        clear_engine_cache()


def test_model_declares_nullable_columns_without_defaults() -> None:
    table = SqlConversationMetadata.__table__
    assert isinstance(table.c.background_work_running.type, sa.Boolean)
    assert isinstance(table.c.background_work_reported_at.type, sa.BigInteger)
    assert isinstance(table.c.blocked_on.type, sa.String)
    assert table.c.blocked_on.type.length == 128
    for column in (
        table.c.background_work_running,
        table.c.background_work_reported_at,
        table.c.blocked_on,
    ):
        assert column.nullable
        assert column.server_default is None


def test_upgrade_adds_the_columns_and_keeps_rows(database: tuple[str, sa.Engine]) -> None:
    uri, engine = database
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
    assert isinstance(columns["background_work_reported_at"]["type"], sa.BigInteger)
    assert isinstance(columns["blocked_on"]["type"], sa.String)
    for name in ("background_work_running", "background_work_reported_at", "blocked_on"):
        assert columns[name]["nullable"] is True
        assert columns[name]["default"] is None
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT git_branch, background_work_running, background_work_reported_at, "
                f"blocked_on FROM {_TABLE}"
            )
        ).one()
    assert tuple(row) == ("main", None, None, None)
    _assert_blocked_on_bytes_are_bounded(engine)
    _assert_flag_and_report_time_are_typed(engine)


def test_model_built_schema_bounds_the_new_columns(database: tuple[str, sa.Engine]) -> None:
    _, engine = database
    with engine.begin() as conn:
        conn.execute(sa.text(f"DROP TABLE {_TABLE}"))
    OmnigentBase.metadata.create_all(engine, tables=[SqlConversationMetadata.__table__])

    _assert_blocked_on_bytes_are_bounded(engine)
    _assert_flag_and_report_time_are_typed(engine)


def test_downgrade_drops_the_columns_and_keeps_rows(database: tuple[str, sa.Engine]) -> None:
    uri, engine = database
    _migrate(uri, engine, "upgrade", _REVISION)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                f"INSERT INTO {_TABLE} "
                "(workspace_id, id, kind, git_branch, background_work_running, "
                "background_work_reported_at, blocked_on) "
                "VALUES (0, :id, 1, 'main', 1, 1793300000000000000, 'permission prompt')"
            ),
            {"id": _SESSION_ID},
        )

    _migrate(uri, engine, "downgrade", _PREVIOUS)

    columns = _columns(engine)
    assert "background_work_running" not in columns
    assert "background_work_reported_at" not in columns
    assert "blocked_on" not in columns
    with engine.connect() as conn:
        assert conn.execute(sa.text(f"SELECT git_branch FROM {_TABLE}")).scalar_one() == "main"
