"""Tests for the migration admitting the ``code_run`` item type (``nn1a2b3c4d5e``).

Runs on SQLite always, and on PostgreSQL or CockroachDB when
``OMNIGENT_TEST_DB_URI`` points at one. Checks the widened ``CHECK`` admits
code 12 and still rejects unknown codes, the table rebuild keeps the key and
indexes, and the downgrade removes ``code_run`` rows before restoring the
narrower ``CHECK``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine

from omnigent.db.cockroachdb import _crdb_server_version, _prepare_crdb_schema_transaction
from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine

_MIGRATION = "omnigent.db.migrations.versions.nn1a2b3c4d5e_conversation_items_type_code_run"
_PRIOR = "mm1a2b3c4d5e"
_THIS = "nn1a2b3c4d5e"
_CONVERSATION = uuid.uuid4().bytes


# DROP DATABASE spelling per server backend.
_DROP_DATABASE = {
    "postgresql": 'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)',
    "cockroachdb": 'DROP DATABASE IF EXISTS "{name}" CASCADE',
}


@pytest.fixture(params=["sqlite", "postgresql", "cockroachdb"])
def engine(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Engine]:
    """Engine on a fresh database at the revision before this one."""
    admin = None
    if request.param == "sqlite":
        uri = f"sqlite:///{tmp_path / 'test.db'}"
    else:
        root_uri = os.environ.get("OMNIGENT_TEST_DB_URI", "")
        if not root_uri.startswith(request.param):
            pytest.skip(f"needs {request.param} (OMNIGENT_TEST_DB_URI)")
        name = f"nn1_code_run_{os.getpid()}"
        drop = _DROP_DATABASE[request.param].format(name=name)
        root = sa.make_url(root_uri)
        uri = root.set(database=name).render_as_string(hide_password=False)
        admin = sa.create_engine(root, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(sa.text(drop))
            conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    if request.param == "cockroachdb":
        # CockroachDB is bootstrapped from the models at head and never runs
        # the earlier chain, so reach the prior revision by downgrading.
        get_or_create_engine(uri)
        clear_engine_cache()
        engine = sa.create_engine(uri)
        _migrate(engine, command.downgrade, _PRIOR)
    else:
        engine = sa.create_engine(uri)
        _migrate(engine, command.upgrade, _PRIOR)
    try:
        yield engine
    finally:
        engine.dispose()
        clear_engine_cache()
        if admin is not None:
            with admin.connect() as conn:
                conn.execute(sa.text(drop))
            admin.dispose()


def _migrate(engine: Engine, step: Any, target: str) -> None:
    cfg = _build_alembic_config(engine.url.render_as_string(hide_password=False))
    # A plain connection lets Alembic own transactions, as _run_migrations does;
    # earlier revisions open autocommit blocks for concurrent index builds.
    with engine.connect() as conn:
        if engine.dialect.name == "cockroachdb":
            _prepare_crdb_schema_transaction(conn, _crdb_server_version(engine))
        cfg.attributes["connection"] = conn
        step(cfg, target)
        conn.commit()
    # Drop pooled connections so later reflection sees the migrated schema.
    engine.dispose()


def _insert_item(engine: Engine, type_code: int, position: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversation_items (workspace_id, conversation_id, id, "
                "response_id, created_at, status, position, type, data, search_text) "
                "VALUES (0, :conversation, :id, 'resp_1', 1, 1, :position, :type, '{}', '')"
            ),
            {
                "conversation": _CONVERSATION,
                "id": uuid.uuid4().bytes,
                "position": position,
                "type": type_code,
            },
        )


def _item_types(engine: Engine) -> list[int]:
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT type FROM conversation_items ORDER BY position"))
        return [row[0] for row in rows]


def _key_and_indexes(engine: Engine) -> tuple[Any, ...]:
    inspector = sa.inspect(engine)
    indexes = sorted(
        (index["name"], tuple(index["column_names"]), index["unique"])
        for index in inspector.get_indexes("conversation_items")
    )
    primary_key = inspector.get_pk_constraint("conversation_items")["constrained_columns"]
    return tuple(primary_key), tuple(indexes)


def test_upgrade_admits_code_run_type(engine: Engine) -> None:
    """Code 12 is rejected before the migration and accepted after it."""
    with pytest.raises(sa.exc.IntegrityError):
        _insert_item(engine, type_code=12, position=0)
    _insert_item(engine, type_code=1, position=1)
    schema_before = _key_and_indexes(engine)

    _migrate(engine, command.upgrade, _THIS)

    _insert_item(engine, type_code=12, position=2)
    with pytest.raises(sa.exc.IntegrityError):
        _insert_item(engine, type_code=13, position=3)
    assert _item_types(engine) == [1, 12]
    assert _key_and_indexes(engine) == schema_before


def test_downgrade_removes_code_run_rows_and_restores_check(engine: Engine) -> None:
    """Downgrade deletes code 12 rows, keeps the rest, and rejects 12 again."""
    _migrate(engine, command.upgrade, _THIS)
    _insert_item(engine, type_code=11, position=0)
    _insert_item(engine, type_code=12, position=1)
    schema_at_head = _key_and_indexes(engine)

    _migrate(engine, command.downgrade, _PRIOR)

    assert _item_types(engine) == [11]
    with pytest.raises(sa.exc.IntegrityError):
        _insert_item(engine, type_code=12, position=2)
    assert _key_and_indexes(engine) == schema_at_head


def _drop_type_check(engine: Engine) -> None:
    """Leave the state of a CockroachDB run that stopped after committing the drop."""
    with engine.connect() as conn:
        _prepare_crdb_schema_transaction(conn, _crdb_server_version(engine))
        conn.execute(
            sa.text("ALTER TABLE conversation_items DROP CONSTRAINT ck_conversation_items_type")
        )
        conn.commit()


@pytest.mark.parametrize("engine", ["cockroachdb"], indirect=True)
def test_upgrade_resumes_after_committed_drop(engine: Engine) -> None:
    """An upgrade retried after its committed drop adds the widened CHECK."""
    _drop_type_check(engine)

    _migrate(engine, command.upgrade, _THIS)

    _insert_item(engine, type_code=12, position=0)
    with pytest.raises(sa.exc.IntegrityError):
        _insert_item(engine, type_code=13, position=1)


@pytest.mark.parametrize("engine", ["cockroachdb"], indirect=True)
def test_downgrade_resumes_after_committed_drop(engine: Engine) -> None:
    """A downgrade retried after its committed drop removes code 12 and narrows the CHECK."""
    _migrate(engine, command.upgrade, _THIS)
    _drop_type_check(engine)
    _insert_item(engine, type_code=12, position=0)

    _migrate(engine, command.downgrade, _PRIOR)

    assert _item_types(engine) == []
    with pytest.raises(sa.exc.IntegrityError):
        _insert_item(engine, type_code=12, position=1)


@pytest.mark.parametrize("engine", ["cockroachdb"], indirect=True)
@pytest.mark.parametrize(
    "wrong_check",
    [
        "type NOT IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)",
        "type IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12) AND type <> 12",
    ],
)
def test_upgrade_replaces_same_named_wrong_check(engine: Engine, wrong_check: str) -> None:
    """A same-named CHECK whose numbers match but whose shape differs is replaced."""
    _drop_type_check(engine)
    with engine.connect() as conn:
        _prepare_crdb_schema_transaction(conn, _crdb_server_version(engine))
        conn.execute(
            sa.text(
                "ALTER TABLE conversation_items "
                f"ADD CONSTRAINT ck_conversation_items_type CHECK ({wrong_check})"
            )
        )
        conn.commit()

    _migrate(engine, command.upgrade, _THIS)

    _insert_item(engine, type_code=12, position=0)
    with pytest.raises(sa.exc.IntegrityError):
        _insert_item(engine, type_code=13, position=1)


_ALL_CODES = frozenset(range(1, 13))
_ALL_CODES_SQL = ", ".join(str(code) for code in sorted(_ALL_CODES))


@pytest.mark.parametrize(
    ("sqltext", "expected"),
    [
        # Captured verbatim from CockroachDB 23.2.28: inspector, then catalog form.
        ("type IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)", _ALL_CODES),
        ("CHECK ((type IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)))", _ALL_CODES),
        ("type IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)", frozenset(range(1, 12))),
        # Captured verbatim from PostgreSQL 16, which rewrites IN as = ANY.
        ("type = ANY (ARRAY[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])", None),
        ("CHECK ((type = ANY (ARRAY[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])))", None),
        # Normalized spellings of the same constraint.
        (f'  ( "type"  in  ( {_ALL_CODES_SQL.replace(", ", " ,")} ) )  ', _ALL_CODES),
        (f"type IN ({', '.join(f'{c}:::INT8' for c in sorted(_ALL_CODES))})", _ALL_CODES),
        (f"type IN ({', '.join(f'{c}::INT8' for c in sorted(_ALL_CODES))})", _ALL_CODES),
        # Other shapes are unrecognized, even when their numbers match.
        (f"type NOT IN ({_ALL_CODES_SQL})", None),
        (f"type IN ({_ALL_CODES_SQL}) AND type <> 12", None),
        (f"(type IN ({_ALL_CODES_SQL})) AND (type <> 12)", None),
        (f"type IN ({_ALL_CODES_SQL}) OR type = 13", None),
        (f"kind IN ({_ALL_CODES_SQL})", None),
        ("type IN ()", None),
        ("not a constraint (1, 2)", None),
        ("", None),
    ],
)
def test_parse_type_check_codes(sqltext: str, expected: frozenset[int] | None) -> None:
    assert import_module(_MIGRATION)._parse_type_check_codes(sqltext) == expected
