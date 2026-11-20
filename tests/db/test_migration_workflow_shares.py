"""The ``workflow_shares`` table migration (d3bda6dbade3) and the model it must match.

Checks both schema paths (the migration and the ORM metadata) for the table's
columns, primary key, user index and SQLite-family checks, that it has no
database-level foreign key (schema Rule R032), and that its downgrade drops it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError

from omnigent.db.db_models import SqlWorkflowShare
from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine

_PREVIOUS = "ffeec66eadb5"
_REVISION = "d3bda6dbade3"
_TABLE = "workflow_shares"
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


@pytest.fixture
def db_engine(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Engine]:
    """A migrated database, or just the shares table from ORM metadata."""
    if request.param == "configured":
        yield get_or_create_engine(request.getfixturevalue("db_uri"))
        return
    uri = f"sqlite:///{tmp_path / 'test.db'}"
    if request.param == "metadata":
        engine = sa.create_engine(uri)
        SqlWorkflowShare.__table__.create(engine)
    else:
        engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        engine.dispose()
        clear_engine_cache()


def _share(shares: sa.Table, **values: object) -> sa.Insert:
    row = {"workspace_id": 1, "workflow_id": b"a" * 16, "user_id": "alice", "level": 1}
    return shares.insert().values(**{**row, **values})


@pytest.mark.parametrize("db_engine", _SCHEMA_PATHS, indirect=True)
def test_columns_primary_key_and_index(db_engine: Engine) -> None:
    """Four NOT NULL columns without DB defaults, one row per grantee per workflow, and
    a non-unique user index for "shared with me" reads."""
    inspector = sa.inspect(db_engine)
    columns = {column["name"]: column for column in inspector.get_columns(_TABLE)}
    assert set(columns) == {"workspace_id", "workflow_id", "user_id", "level"}
    assert all(column["nullable"] is False for column in columns.values())
    assert all(column["default"] is None for column in columns.values())
    assert columns["user_id"]["type"].length == 128
    if db_engine.dialect.name == "cockroachdb":
        # CockroachDB reflects INT2 as INTEGER and drops the width, so read the stored type.
        with db_engine.connect() as conn:
            stored_type = conn.execute(
                sa.text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'workflow_shares' AND column_name = 'level'"
                )
            ).scalar_one()
        assert stored_type == "smallint"
    else:
        assert str(columns["level"]["type"]).upper().startswith(("SMALLINT", "INT2"))
    assert inspector.get_pk_constraint(_TABLE)["constrained_columns"] == [
        "workspace_id",
        "workflow_id",
        "user_id",
    ]
    indexes = {index["name"]: index for index in inspector.get_indexes(_TABLE)}
    assert indexes["ix_workflow_shares_user_id"]["column_names"] == [
        "workspace_id",
        "user_id",
        "workflow_id",
    ]
    assert not indexes["ix_workflow_shares_user_id"]["unique"]
    assert inspector.get_foreign_keys(_TABLE) == []


@pytest.mark.parametrize("db_engine", _SCHEMA_PATHS, indirect=True)
def test_rows_are_unique_per_grantee_and_bounded(db_engine: Engine) -> None:
    """A second share for the same grantee is refused. SQLite and D1 ignore VARCHAR(128)
    and cap stored bytes at 512 instead, so 128 four-byte characters still fit."""
    shares = sa.Table(_TABLE, sa.MetaData(), autoload_with=db_engine)
    with db_engine.begin() as conn:
        conn.execute(_share(shares))
    with pytest.raises(IntegrityError), db_engine.begin() as conn:
        conn.execute(_share(shares, level=2))
    with db_engine.begin() as conn:
        conn.execute(_share(shares, user_id="bob", level=2))
    with pytest.raises(DBAPIError), db_engine.begin() as conn:
        conn.execute(_share(shares, user_id="a" * 513))
    # 129 characters and 513 bytes: over the width everywhere, and over the byte cap on SQLite.
    with pytest.raises(DBAPIError), db_engine.begin() as conn:
        conn.execute(_share(shares, user_id="😀" * 128 + "a"))
    with db_engine.begin() as conn:
        conn.execute(_share(shares, user_id="😀" * 128))


@pytest.mark.parametrize("column", ["workspace_id", "level"])
@pytest.mark.parametrize("db_engine", _SCHEMA_PATHS, indirect=True)
def test_non_integer_codes_are_refused(db_engine: Engine, column: str) -> None:
    """A non-integer value in an integer column is refused, even where SQLite would store it."""
    shares = sa.Table(_TABLE, sa.MetaData(), autoload_with=db_engine)
    with pytest.raises(DBAPIError), db_engine.begin() as conn:
        conn.execute(_share(shares, **{column: "not-a-number"}))


def test_downgrade_drops_the_table_and_upgrade_restores_it(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'shares.db'}"
    engine = get_or_create_engine(uri)
    assert _TABLE in sa.inspect(engine).get_table_names()

    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, _PREVIOUS)
    assert _TABLE not in sa.inspect(engine).get_table_names()

    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, _REVISION)
    assert _TABLE in sa.inspect(engine).get_table_names()

    engine.dispose()
    clear_engine_cache()
