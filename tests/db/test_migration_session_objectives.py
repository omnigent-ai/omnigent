"""Tests for the ``session_objectives`` table and its migration (74b9abb2854c).

The migration and the ORM model must agree on the table's columns, owner-first
primary key, ordering indexes and CHECK constraints, with no database foreign
key (schema Rule R032). Downgrading drops the table, and upgrading again
recreates it empty.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from omnigent.db.db_models import SqlSessionObjective
from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine

_PREVIOUS = "e5b2c8d4f1a7"
_REVISION = "74b9abb2854c"
_TABLE = "session_objectives"
_SESSION = bytes.fromhex("0123456789abcdef0123456789abcdef")
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
_OUTCOME = {
    "outcome_success": False,
    "outcome_payload": '{"plaintext":{"emoji":"🧯","text":"Build still red"}}'.encode(),
    "outcome_recorded_at": 2,
}


@pytest.fixture
def db_engine(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Engine]:
    """A database migrated to head, the table built from ORM metadata, or a configured one."""
    path = getattr(request, "param", "migration")
    if path == "configured":
        yield get_or_create_engine(request.getfixturevalue("db_uri"))
        return
    uri = f"sqlite:///{tmp_path / 'objectives.db'}"
    if path == "metadata":
        engine = sa.create_engine(uri)
        SqlSessionObjective.__table__.create(engine)
    else:
        engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        engine.dispose()
        clear_engine_cache()


def _migrate(engine: Engine, step: str, revision: str) -> None:
    config = _build_alembic_config(engine.url.render_as_string(hide_password=False))
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        getattr(command, step)(config, revision)


def _indexes(engine: Engine) -> dict[str, list[str]]:
    return {
        index["name"]: index["column_names"] for index in sa.inspect(engine).get_indexes(_TABLE)
    }


def _row(object_id: bytes, **values: object) -> dict[str, object]:
    # The table has no database defaults, so every insert names the workspace.
    return {
        "workspace_id": 0,
        "conversation_id": _SESSION,
        "id": object_id,
        "created_at": 1,
        **values,
    }


# MySQL reports a CHECK violation as OperationalError 3819 rather than IntegrityError.
_MYSQL_CHECK_CONSTRAINT_VIOLATED = 3819


@contextmanager
def _rejected_by_check() -> Iterator[None]:
    """Expect a CHECK violation, accepting MySQL's 3819 but no other operational error."""
    with pytest.raises((IntegrityError, sa.exc.OperationalError)) as raised:
        yield
    if isinstance(raised.value, sa.exc.OperationalError):
        assert raised.value.orig.args[0] == _MYSQL_CHECK_CONSTRAINT_VIOLATED, raised.value


@pytest.mark.parametrize("db_engine", _SCHEMA_PATHS, indirect=True)
def test_table_shape(db_engine: Engine) -> None:
    """Columns, nullability, the owner-first key and both ordering indexes, with no FK."""
    insp = sa.inspect(db_engine)
    columns = {column["name"]: column for column in insp.get_columns(_TABLE)}
    assert {name: column["nullable"] for name, column in columns.items()} == {
        "workspace_id": False,
        "conversation_id": False,
        "id": False,
        "created_at": False,
        "scheduled_task_run_id": True,
        "objective_payload": True,
        "outcome_success": True,
        "outcome_payload": True,
        "outcome_recorded_at": True,
    }
    # The CockroachDB dialect reflects INT8 as SQLAlchemy Integer.
    timestamp_type = sa.Integer if db_engine.dialect.name == "cockroachdb" else sa.BigInteger
    for name in ("created_at", "outcome_recorded_at"):
        assert isinstance(columns[name]["type"], timestamp_type), name
    assert insp.get_pk_constraint(_TABLE)["constrained_columns"] == [
        "workspace_id",
        "conversation_id",
        "id",
    ]
    assert insp.get_foreign_keys(_TABLE) == []
    indexes = _indexes(db_engine)
    assert indexes["ix_session_objectives_conversation_created"] == [
        "workspace_id",
        "conversation_id",
        "created_at",
        "id",
    ]
    # Both ordered indexes carry the whole primary key, so each entry is unique.
    assert indexes["ix_session_objectives_run_created"] == [
        "workspace_id",
        "scheduled_task_run_id",
        "created_at",
        "id",
        "conversation_id",
    ]
    # A new table declares no database defaults.
    assert {name: column["default"] for name, column in columns.items()} == dict.fromkeys(columns)


@pytest.mark.parametrize("column", ["objective_payload", "outcome_payload"])
@pytest.mark.parametrize("db_engine", _SCHEMA_PATHS, indirect=True)
def test_payload_size_checks(db_engine: Engine, column: str) -> None:
    """Each payload holds at most 2,048 stored bytes, on insert and on update.

    Declared lengths are not enforced on every engine (SQLite ignores them), so
    the CHECKs are what bound the columns on both the migrated and the ORM-built
    schema. The outcome payload goes in with a whole outcome, which another
    CHECK requires.
    """
    table = sa.Table(_TABLE, sa.MetaData(), autoload_with=db_engine)
    extra = _OUTCOME if column == "outcome_payload" else {}

    def values(size: bytes) -> dict[str, object]:
        return {**extra, column: size}

    with db_engine.begin() as conn:
        conn.execute(table.insert().values(**_row(b"1" * 16, **values(b"x" * 2048))))
        assert (
            conn.execute(
                sa.select(sa.func.length(table.c[column])).where(table.c.id == b"1" * 16)
            ).scalar_one()
            == 2048
        )
    with _rejected_by_check(), db_engine.begin() as conn:
        conn.execute(table.insert().values(**_row(b"2" * 16, **values(b"x" * 2049))))
    with _rejected_by_check(), db_engine.begin() as conn:
        conn.execute(
            table.update().where(table.c.id == b"1" * 16).values({column: "é".encode() * 1025})
        )


@pytest.mark.parametrize(
    "missing",
    [
        ("outcome_success",),
        ("outcome_payload",),
        ("outcome_recorded_at",),
        ("outcome_payload", "outcome_recorded_at"),
        ("outcome_success", "outcome_recorded_at"),
        ("outcome_success", "outcome_payload"),
    ],
)
@pytest.mark.parametrize("db_engine", _SCHEMA_PATHS, indirect=True)
def test_an_outcome_is_stored_whole(db_engine: Engine, missing: tuple[str, ...]) -> None:
    """An outcome needs its success, words and time together; no part of it is stored alone."""
    table = sa.Table(_TABLE, sa.MetaData(), autoload_with=db_engine)
    with db_engine.begin() as conn:
        conn.execute(table.insert().values(**_row(b"1" * 16)))
        conn.execute(table.insert().values(**_row(b"2" * 16, **_OUTCOME)))
    partial = {name: value for name, value in _OUTCOME.items() if name not in missing}
    with _rejected_by_check(), db_engine.begin() as conn:
        conn.execute(table.insert().values(**_row(b"3" * 16, **partial)))
    with _rejected_by_check(), db_engine.begin() as conn:
        conn.execute(table.update().where(table.c.id == b"2" * 16).values(dict.fromkeys(missing)))


def test_migration_round_trips(tmp_path: Path) -> None:
    """Upgrade creates the table, downgrade drops it with its rows, and upgrade recreates it."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'round_trip.db'}")
    try:
        _migrate(engine, "upgrade", _PREVIOUS)
        assert _TABLE not in sa.inspect(engine).get_table_names()

        _migrate(engine, "upgrade", _REVISION)
        table = sa.Table(_TABLE, sa.MetaData(), autoload_with=engine)
        objective = b'{"plaintext":{"text":"Get the nightly build green"}}'
        with engine.begin() as conn:
            conn.execute(
                table.insert().values(
                    **_row(b"a" * 16, scheduled_task_run_id=b"r" * 16),
                    objective_payload=objective,
                    **_OUTCOME,
                )
            )
            row = conn.execute(sa.select(table)).one()
        # A recorded failure must not read back as "nothing recorded".
        assert row.outcome_success is not None
        assert (
            bytes(row.scheduled_task_run_id),
            bytes(row.objective_payload),
            bool(row.outcome_success),
            bytes(row.outcome_payload),
            row.outcome_recorded_at,
        ) == (b"r" * 16, objective, False, _OUTCOME["outcome_payload"], 2)

        _migrate(engine, "downgrade", _PREVIOUS)
        assert _TABLE not in sa.inspect(engine).get_table_names()

        _migrate(engine, "upgrade", _REVISION)
        assert set(_indexes(engine)) >= {
            "ix_session_objectives_conversation_created",
            "ix_session_objectives_run_created",
        }
        count = sa.select(sa.func.count()).select_from(sa.table(_TABLE))
        with engine.connect() as conn:
            assert conn.execute(count).scalar_one() == 0
    finally:
        engine.dispose()
        clear_engine_cache()
