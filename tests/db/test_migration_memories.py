"""The ``memories`` table migration (ffeec66eadb5) and the model it must match."""

from __future__ import annotations

import random
import string
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.schema import CreateTable

from omnigent.db.db_models import (
    MEMORY_CONTENT_MAX_STORED_BYTES,
    MEMORY_DESCRIPTION_MAX_STORED_BYTES,
    SqlMemory,
)
from omnigent.db.envelope import seal
from omnigent.db.utils import _build_alembic_config, get_or_create_engine

_REVISION = "ffeec66eadb5"
_PREVIOUS = "a8be64286ffc"
_MEMORIES = SqlMemory.__table__


def _row(path: str, content: str = "x", **overrides: object) -> dict[str, object]:
    return {
        "workspace_id": 0,
        "actor_id": "user:alice",
        "path": path,
        "content": content,
        "description": None,
        "created_at": 1,
        "updated_at": 1,
        **overrides,
    }


def _migrate(engine: sa.Engine, revision: str, *, down: bool = False) -> None:
    config = _build_alembic_config(str(engine.url))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        (command.downgrade if down else command.upgrade)(config, revision)


def _shape(engine: sa.Engine) -> tuple[object, ...]:
    inspector = sa.inspect(engine)
    columns = [
        (column["name"], str(column["type"]), column["nullable"], column["default"])
        for column in inspector.get_columns("memories")
    ]
    checks = sorted(
        (check["name"], " ".join(check["sqltext"].split()))
        for check in inspector.get_check_constraints("memories")
    )
    return columns, inspector.get_pk_constraint("memories")["constrained_columns"], checks


def _assert_sqlite_rejects_out_of_bound_values(engine: sa.Engine) -> None:
    """Raw SQL bypasses the column types; the table's own checks must still hold."""
    raw = {**_row("/bounds"), "content": seal("x"), "description": None}
    oversized = {
        "actor_id": "u" * 641,
        "path": "/" + "p" * 2_048,
        "content": b"\x01\x00" + b"x" * MEMORY_CONTENT_MAX_STORED_BYTES,
        "description": b"\x01\x00" + b"x" * MEMORY_DESCRIPTION_MAX_STORED_BYTES,
        "workspace_id": "not a number",
        "created_at": "not a number",
        "updated_at": "not a number",
    }
    insert = sa.text(
        "INSERT INTO memories (workspace_id, actor_id, path, content, description, "
        "created_at, updated_at) VALUES (:workspace_id, :actor_id, :path, :content, "
        ":description, :created_at, :updated_at)"
    )
    for column, value in oversized.items():
        with pytest.raises(IntegrityError, match="CHECK"), engine.begin() as connection:
            connection.execute(insert, {**raw, column: value})
    with engine.begin() as connection:
        connection.execute(insert, raw)


def test_memories_upgrade_and_downgrade(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'memories.db'}")
    try:
        _migrate(engine, _PREVIOUS)
        assert not sa.inspect(engine).has_table("memories")

        _migrate(engine, _REVISION)

        columns, primary_key, _ = _shape(engine)
        assert [name for name, *_ in columns] == [
            "workspace_id",
            "actor_id",
            "path",
            "content",
            "description",
            "created_at",
            "updated_at",
        ]
        # A new table declares no database defaults: the application supplies every value.
        assert all(default is None for *_, default in columns)
        assert [name for name, _, nullable, _ in columns if nullable] == ["description"]
        assert primary_key == ["workspace_id", "actor_id", "path"]
        with engine.begin() as connection:
            connection.execute(
                _MEMORIES.insert().values(**_row("/a", "Prefers tabs", description="Tabs"))
            )
            assert connection.execute(
                sa.select(_MEMORIES.c.content, _MEMORIES.c.description)
            ).one() == ("Prefers tabs", "Tabs")
            # Stored as plaintext envelopes, opaque to the database.
            content, description = connection.execute(
                sa.text("SELECT content, description FROM memories")
            ).one()
            assert content == seal("Prefers tabs")
            assert description == seal("Tabs")
        _assert_sqlite_rejects_out_of_bound_values(engine)

        _migrate(engine, _PREVIOUS, down=True)

        assert not sa.inspect(engine).has_table("memories")
    finally:
        engine.dispose()


def test_a_metadata_bootstrapped_table_matches_the_migration(tmp_path: Path) -> None:
    """Fresh CockroachDB databases are created from the models, then stamped at head."""
    migrated = sa.create_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    bootstrapped = sa.create_engine(f"sqlite:///{tmp_path / 'bootstrapped.db'}")
    try:
        _migrate(migrated, _REVISION)
        _MEMORIES.create(bootstrapped)

        assert _shape(bootstrapped) == _shape(migrated)
        _assert_sqlite_rejects_out_of_bound_values(bootstrapped)
    finally:
        migrated.dispose()
        bootstrapped.dispose()


def test_columns_declare_their_bounds_and_byte_order_per_engine() -> None:
    mysql_ddl = str(CreateTable(_MEMORIES).compile(dialect=mysql.dialect()))
    postgresql_ddl = str(CreateTable(_MEMORIES).compile(dialect=postgresql.dialect()))

    exact = "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL"
    assert f"actor_id VARCHAR(160) {exact}" in mysql_ddl
    assert f"path VARCHAR(512) {exact}" in mysql_ddl
    assert "content VARBINARY(16383) NOT NULL" in mysql_ddl
    assert "description VARBINARY(2100)," in mysql_ddl
    assert 'path VARCHAR(512) COLLATE "C" NOT NULL' in postgresql_ddl
    assert "content BYTEA NOT NULL" in postgresql_ddl
    assert "DEFAULT" not in mysql_ddl
    assert "DEFAULT" not in postgresql_ddl
    # The SQLite-only checks stay off other engines.
    assert "CHECK" not in mysql_ddl
    assert "CHECK" not in postgresql_ddl


def test_envelope_writes_enforce_the_stored_byte_limits(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'limit.db'}")
    rng = random.Random(0)
    incompressible = "".join(rng.choices(string.ascii_letters + string.digits, k=40_000))
    # The largest description: 512 characters of four UTF-8 bytes each, hard to compress.
    widest_description = "".join(chr(rng.randrange(0x10000, 0x10FFFF)) for _ in range(512))
    try:
        _MEMORIES.create(engine)

        with engine.begin() as connection:
            connection.execute(
                _MEMORIES.insert().values(**_row("/wide", description=widest_description))
            )
            assert (
                connection.execute(sa.select(_MEMORIES.c.description)).scalar_one()
                == widest_description
            )
        for column, value in (("content", incompressible), ("description", incompressible)):
            with pytest.raises(StatementError, match="size limit"), engine.begin() as connection:
                connection.execute(_MEMORIES.insert().values(**_row("/big", **{column: value})))
    finally:
        engine.dispose()


def test_paths_and_owners_compare_byte_for_byte_on_this_engine(db_uri: str) -> None:
    """Listing a prefix reads the key range ``[prefix + "/", prefix + "0")``.

    That is exact only when keys compare by byte: case-sensitively, without
    padding trailing spaces, and with control characters in byte order. CI runs
    this on every supported engine.
    """
    paths = [
        "/p",
        "/p/a",
        "/p/a ",
        "/p/b/c",
        "/p/é",
        "/p/\x01",
        "/P/a",
        "/p-x",
        "/p0",
        "/pa",
        "/q",
    ]
    rows = [_row(path) for path in paths] + [
        _row("/p/a", actor_id="user:Alice"),
        _row("/p/a", actor_id="user:alicé"),
    ]
    engine = get_or_create_engine(db_uri)
    with engine.begin() as connection:
        connection.execute(_MEMORIES.insert(), rows)
        alice = _MEMORIES.c.actor_id == "user:alice"
        under = (
            connection.execute(
                sa.select(_MEMORIES.c.path)
                .where(alice, _MEMORIES.c.path >= "/p/", _MEMORIES.c.path < "/p0")
                .order_by(_MEMORIES.c.path)
            )
            .scalars()
            .all()
        )
        ordered = (
            connection.execute(sa.select(_MEMORIES.c.path).where(alice).order_by(_MEMORIES.c.path))
            .scalars()
            .all()
        )
        owners = (
            connection.execute(sa.select(_MEMORIES.c.actor_id).where(_MEMORIES.c.path == "/p/a"))
            .scalars()
            .all()
        )

    assert under == sorted(["/p/a", "/p/a ", "/p/b/c", "/p/é", "/p/\x01"], key=str.encode)
    assert ordered == sorted(paths, key=str.encode)
    assert sorted(owners) == ["user:Alice", "user:alice", "user:alicé"]
