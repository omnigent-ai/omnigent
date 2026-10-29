"""Tests for the conversation metadata ``account_transition`` migration (7a5b2074da28).

The migration and the ORM model both add a nullable bounded string column with
no database default; existing rows keep their values and read ``NULL``, and
downgrading drops the column unless a move is still pending.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.db_models import ACCOUNT_TRANSITION_MAX_BYTES, SqlConversationMetadata
from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_PREVIOUS = "d0a1f879a01d"
_REVISION = "7a5b2074da28"
_TABLE = "omnigent_conversation_metadata"
_SESSION_ID = bytes.fromhex("0123456789abcdef0123456789abcdef")
_OTHER_SESSION_ID = bytes.fromhex("fedcba9876543210fedcba9876543210")


def _migrate(uri: str, engine: sa.Engine, step: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        getattr(command, step)(config, revision)


def _insert(engine: sa.Engine, session_id: bytes, **values: object) -> None:
    columns = ", ".join(["workspace_id", "id", "kind", *values])
    binds = ", ".join(["0", ":id", "1", *(f":{name}" for name in values)])
    with engine.begin() as conn:
        conn.execute(
            sa.text(f"INSERT INTO {_TABLE} ({columns}) VALUES ({binds})"),
            {"id": session_id, **values},
        )


def _columns(engine: sa.Engine) -> dict[str, dict]:
    return {column["name"]: column for column in sa.inspect(engine).get_columns(_TABLE)}


@pytest.fixture
def database(tmp_path: Path) -> Iterator[tuple[str, sa.Engine]]:
    uri = f"sqlite:///{tmp_path / 'metadata.db'}"
    engine = sa.create_engine(uri)
    try:
        yield uri, engine
    finally:
        engine.dispose()
        clear_engine_cache()


def test_model_declares_a_bounded_nullable_string() -> None:
    column = SqlConversationMetadata.__table__.c.account_transition
    assert column.nullable
    assert isinstance(column.type, sa.String)
    assert column.type.length == ACCOUNT_TRANSITION_MAX_BYTES < 16_384
    assert column.server_default is None


def test_upgrade_adds_the_column_and_keeps_rows(database: tuple[str, sa.Engine]) -> None:
    uri, engine = database
    _migrate(uri, engine, "upgrade", _PREVIOUS)
    assert "account_transition" not in _columns(engine)
    _insert(engine, _SESSION_ID, git_branch="main")

    _migrate(uri, engine, "upgrade", _REVISION)

    column = _columns(engine)["account_transition"]
    assert column["nullable"] is True
    assert column["default"] is None
    assert isinstance(column["type"], sa.String)
    assert column["type"].length == ACCOUNT_TRANSITION_MAX_BYTES
    with engine.connect() as conn:
        row = conn.execute(sa.text(f"SELECT git_branch, account_transition FROM {_TABLE}")).one()
    assert tuple(row) == ("main", None)


def test_downgrade_drops_the_column_and_keeps_rows(database: tuple[str, sa.Engine]) -> None:
    uri, engine = database
    _migrate(uri, engine, "upgrade", _REVISION)
    _insert(engine, _SESSION_ID, git_branch="main", account_transition=None)

    _migrate(uri, engine, "downgrade", _PREVIOUS)

    assert "account_transition" not in _columns(engine)
    with engine.connect() as conn:
        assert conn.execute(sa.text(f"SELECT git_branch FROM {_TABLE}")).scalar_one() == "main"


def test_downgrade_refuses_while_a_move_is_pending(database: tuple[str, sa.Engine]) -> None:
    uri, engine = database
    _migrate(uri, engine, "upgrade", _REVISION)
    _insert(engine, _SESSION_ID, git_branch="main")
    _insert(engine, _OTHER_SESSION_ID, account_transition='{"id":"move"}')

    with pytest.raises(RuntimeError, match="pending"):
        _migrate(uri, engine, "downgrade", _PREVIOUS)

    assert "account_transition" in _columns(engine)
    with engine.connect() as conn:
        pending = conn.execute(
            sa.text(
                f"SELECT account_transition FROM {_TABLE} WHERE account_transition IS NOT NULL"
            )
        ).scalar_one()
    assert pending == '{"id":"move"}'

    with engine.begin() as conn:
        conn.execute(sa.text(f"UPDATE {_TABLE} SET account_transition = NULL"))
    _migrate(uri, engine, "downgrade", _PREVIOUS)
    assert "account_transition" not in _columns(engine)
