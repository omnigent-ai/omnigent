"""Tests for the ``comment_replies`` table and its migration (21fd69a3f935).

The migration and the ORM model agree on every schema path: replies nest under
their comment in the primary key, ``ix_comment_replies_thread`` pages one thread
in creation order, no column has a database default, and SQLite-family
databases also enforce the byte and type bounds that SQLite ignores. Downgrade
drops the table.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from importlib import import_module
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from omnigent.db.compression import CompressedText
from omnigent.db.db_models import (
    COMMENT_REPLY_BODY_MAX_STORED_BYTES,
    SqlCommentReply,
    workspace_scope,
)
from omnigent.db.enum_codecs import (
    decode_comment_reply_author_kind,
    encode_comment_reply_author_kind,
)
from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine

_PREVIOUS = "f1c7a9e2b406"
_REVISION = "21fd69a3f935"
_TABLE = "comment_replies"
_INDEX = "ix_comment_replies_thread"
_PK = ["workspace_id", "conversation_id", "comment_id", "id"]
_INDEX_COLUMNS = ["workspace_id", "conversation_id", "comment_id", "created_at", "id"]
_NULLABLE = {
    "workspace_id": False,
    "conversation_id": False,
    "comment_id": False,
    "id": False,
    "author_kind": False,
    "body": False,
    "created_by": True,
    "created_at": False,
}
_SQLITE_CHECKS = {
    "ck_comment_replies_author_kind",
    "ck_comment_replies_created_by_bytes",
    "ck_comment_replies_body_bytes",
    "ck_comment_replies_created_at_integer",
}
_CONVERSATION = "a9930027fd3e2e979e65844f7af7bf88"
_COMMENT = "cccccccccccccccccccccccccccccc01"
_OTHER_COMMENT = "cccccccccccccccccccccccccccccc02"
_SCHEMA_PATHS = [
    "migration",
    "metadata",
    pytest.param(
        "configured",
        marks=pytest.mark.skipif(
            not os.environ.get("OMNIGENT_TEST_DB_URI"), reason="needs a configured database"
        ),
    ),
]


def _migration() -> ModuleType:
    return import_module("omnigent.db.migrations.versions.21fd69a3f935_add_comment_replies_table")


def _alembic(uri: str, engine: Engine, step: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        getattr(command, step)(config, revision)


def _reply(reply_id: str, created_at: int, **overrides: object) -> SqlCommentReply:
    values: dict[str, object] = {
        "conversation_id": _CONVERSATION,
        "comment_id": _COMMENT,
        "id": reply_id,
        "author_kind": encode_comment_reply_author_kind("user"),
        "body": f"reply {reply_id[-2:]}",
        "created_by": "alice@example.com",
        "created_at": created_at,
    }
    values.update(overrides)
    return SqlCommentReply(**values)


@pytest.fixture
def db_engine(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Engine]:
    """A migrated SQLite database, the model-built table, or the configured database."""
    path = getattr(request, "param", "migration")
    if path == "configured":
        yield get_or_create_engine(request.getfixturevalue("db_uri"))
        return
    uri = f"sqlite:///{tmp_path / 'replies.db'}"
    if path == "metadata":
        engine = sa.create_engine(uri)
        SqlCommentReply.__table__.create(engine)
    else:
        engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        engine.dispose()
        clear_engine_cache()


def test_model_nests_replies_under_their_comment() -> None:
    table = SqlCommentReply.__table__
    assert [column.name for column in table.primary_key.columns] == _PK
    assert {column.name: column.nullable for column in table.columns} == _NULLABLE
    assert all(column.server_default is None for column in table.columns)
    assert not table.foreign_keys
    body_type = table.c.body.type
    assert isinstance(body_type, CompressedText)
    assert body_type.max_stored_bytes == COMMENT_REPLY_BODY_MAX_STORED_BYTES == 16_383
    index = next(index for index in table.indexes if index.name == _INDEX)
    assert [column.name for column in index.columns] == _INDEX_COLUMNS
    assert not index.unique


@pytest.mark.parametrize("db_engine", _SCHEMA_PATHS, indirect=True)
def test_schema_matches_the_model(db_engine: Engine) -> None:
    inspector = sa.inspect(db_engine)
    columns = {column["name"]: column for column in inspector.get_columns(_TABLE)}
    assert {name: column["nullable"] for name, column in columns.items()} == _NULLABLE
    assert all(column["default"] is None for column in columns.values())
    assert inspector.get_pk_constraint(_TABLE)["constrained_columns"] == _PK
    assert inspector.get_foreign_keys(_TABLE) == []
    indexes = {index["name"]: index for index in inspector.get_indexes(_TABLE)}
    assert indexes[_INDEX]["column_names"] == _INDEX_COLUMNS
    assert not indexes[_INDEX]["unique"]
    if db_engine.dialect.name == "sqlite":
        names = {check["name"] for check in inspector.get_check_constraints(_TABLE)}
        assert names == _SQLITE_CHECKS


@pytest.mark.parametrize("db_engine", _SCHEMA_PATHS, indirect=True)
def test_thread_pages_in_creation_order_within_its_workspace(db_engine: Engine) -> None:
    with Session(db_engine) as session, session.begin():
        session.add_all(
            [
                _reply("dddddddddddddddddddddddddddddd03", 1_000_002),
                _reply(
                    "dddddddddddddddddddddddddddddd01",
                    1_000_001,
                    author_kind=encode_comment_reply_author_kind("agent"),
                    created_by=None,
                ),
                # Same microsecond as ...03; the id breaks the tie.
                _reply("dddddddddddddddddddddddddddddd02", 1_000_002),
                _reply("dddddddddddddddddddddddddddddd04", 1_000_000, comment_id=_OTHER_COMMENT),
            ]
        )
    with workspace_scope(7), Session(db_engine) as session, session.begin():
        session.add(_reply("dddddddddddddddddddddddddddddd05", 999))

    def page(after: tuple[int, str] | None, limit: int = 2) -> list[SqlCommentReply]:
        stmt = sa.select(SqlCommentReply).where(
            SqlCommentReply.workspace_id == 0,
            SqlCommentReply.conversation_id == _CONVERSATION,
            SqlCommentReply.comment_id == _COMMENT,
        )
        if after is not None:
            stmt = stmt.where(sa.tuple_(SqlCommentReply.created_at, SqlCommentReply.id) > after)
        stmt = stmt.order_by(SqlCommentReply.created_at, SqlCommentReply.id).limit(limit)
        with Session(db_engine) as session:
            return list(session.execute(stmt).scalars())

    first = page(None)
    assert [reply.id[-2:] for reply in first] == ["01", "02"]
    assert decode_comment_reply_author_kind(first[0].author_kind) == "agent"
    assert first[0].created_by is None
    assert first[1].body == "reply 02"
    second = page((first[-1].created_at, first[-1].id))
    assert [reply.id[-2:] for reply in second] == ["03"]


@pytest.mark.parametrize("db_engine", _SCHEMA_PATHS, indirect=True)
def test_rejects_unknown_author_kind_and_oversized_body(db_engine: Engine) -> None:
    # MySQL reports CHECK violations as OperationalError (3819).
    with (
        pytest.raises((IntegrityError, sa.exc.OperationalError)),
        Session(db_engine) as session,
        session.begin(),
    ):
        session.add(_reply("eeeeeeeeeeeeeeeeeeeeeeeeeeeeee01", 1, author_kind=3))
    # Random bytes barely compress, so the framed body exceeds the cap.
    oversized = os.urandom(COMMENT_REPLY_BODY_MAX_STORED_BYTES).hex()
    with pytest.raises(StatementError), Session(db_engine) as session, session.begin():
        session.add(_reply("eeeeeeeeeeeeeeeeeeeeeeeeeeeeee02", 1, body=oversized))
    with Session(db_engine) as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(SqlCommentReply)) == 0


@pytest.mark.parametrize("db_engine", ["migration", "metadata"], indirect=True)
@pytest.mark.parametrize(
    ("column", "value"),
    [
        # 129 characters is past the declared VARCHAR(128) SQLite ignores.
        ("created_by", "\U0001f600" * 129),
        # 16,384 stored bytes: the framing header plus one byte past the cap.
        ("body", b"\x00\x00" + b"x" * (COMMENT_REPLY_BODY_MAX_STORED_BYTES - 1)),
        ("created_at", "soon"),
    ],
)
def test_sqlite_rejects_raw_writes_past_the_declared_bounds(
    db_engine: Engine, column: str, value: object
) -> None:
    row: dict[str, object] = {
        "workspace_id": 0,
        "conversation_id": bytes.fromhex(_CONVERSATION),
        "comment_id": bytes.fromhex(_COMMENT),
        "id": bytes.fromhex("ffffffffffffffffffffffffffffff01"),
        "author_kind": 1,
        "body": b"\x00\x00hello",
        "created_by": None,
        "created_at": 1,
    }
    row[column] = value
    insert = sa.text(
        f"INSERT INTO {_TABLE} ({', '.join(row)}) VALUES ({', '.join(':' + key for key in row)})"
    )
    with pytest.raises(IntegrityError), db_engine.begin() as conn:
        conn.execute(insert, row)


@pytest.mark.parametrize("db_engine", ["migration", "metadata"], indirect=True)
def test_thread_page_scans_only_the_thread_index_range(db_engine: Engine) -> None:
    query = (
        f"SELECT id FROM {_TABLE} WHERE workspace_id = ? AND conversation_id = ? "
        "AND comment_id = ? AND (created_at, id) > (?, ?) ORDER BY created_at, id LIMIT 50"
    )
    params = (0, bytes.fromhex(_CONVERSATION), bytes.fromhex(_COMMENT), 1, b"\x00" * 16)
    with db_engine.connect() as conn:
        plan = str(conn.exec_driver_sql("EXPLAIN QUERY PLAN " + query, params).all())
    assert f"COVERING INDEX {_INDEX}" in plan
    assert "(created_at,id)>(?,?)" in plan
    assert "TEMP B-TREE" not in plan


def test_upgrade_is_rerunnable_and_downgrade_drops_the_table(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'replies.db'}"
    engine = sa.create_engine(uri)
    try:
        _alembic(uri, engine, "upgrade", _PREVIOUS)
        assert not sa.inspect(engine).has_table(_TABLE)
        _alembic(uri, engine, "upgrade", _REVISION)
        # DDL may commit before the revision stamp on an interrupted upgrade.
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            _migration().upgrade()
        assert sa.inspect(engine).has_index(_TABLE, _INDEX)

        _alembic(uri, engine, "downgrade", _PREVIOUS)
        assert not sa.inspect(engine).has_table(_TABLE)
        _alembic(uri, engine, "upgrade", _REVISION)
        assert sa.inspect(engine).has_index(_TABLE, _INDEX)
    finally:
        engine.dispose()
        clear_engine_cache()


def test_downgrade_and_upgrade_on_the_test_database(db_uri: str) -> None:
    from omnigent.db.cockroachdb import _crdb_server_version, _prepare_crdb_schema_transaction

    engine = get_or_create_engine(db_uri)
    migration = _migration()

    def migrate(operation: Callable[[], None]) -> None:
        with engine.connect() as connection:
            if engine.dialect.name == "cockroachdb":
                _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
            context = MigrationContext.configure(connection)
            with Operations.context(context), context.begin_transaction():
                operation()
            connection.commit()

    try:
        migrate(migration.downgrade)
        assert not sa.inspect(engine).has_table(_TABLE)
        migrate(migration.upgrade)
        migrate(migration.upgrade)
        inspector = sa.inspect(engine)
        assert inspector.get_pk_constraint(_TABLE)["constrained_columns"] == _PK
        assert _INDEX in {index["name"] for index in inspector.get_indexes(_TABLE)}
    finally:
        # Later tests share this database at head.
        if not sa.inspect(engine).has_index(_TABLE, _INDEX):
            migrate(migration.upgrade)
