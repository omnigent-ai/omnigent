"""Tests for the browser_session_revocations migration (78e50b68412e).

Verifies the table's shape (``workspace_id`` leading the primary key, the
purge index, no foreign keys, no SQL defaults, 64-bit epoch columns) on the
configured test database, and that a downgrade drops it cleanly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from omnigent.db.utils import (
    _build_alembic_config,
    clear_engine_cache,
    get_or_create_engine,
)

_TABLE = "browser_session_revocations"
_PREVIOUS_HEAD = "mm1a2b3c4d5e"
# Past the 32-bit signed limit (2038-01-19), e.g. a login plus a long lifetime.
_BEYOND_INT32 = 2**31 + 3600


@pytest.fixture
def db_engine(db_uri: str) -> Engine:
    """The migrated test database (SQLite, or the ``OMNIGENT_TEST_DB_URI`` backend)."""
    return get_or_create_engine(db_uri)


def test_table_shape(db_engine: Engine) -> None:
    """Columns, primary key, purge index and no foreign keys."""
    inspector = sa.inspect(db_engine)
    columns = {c["name"]: c for c in inspector.get_columns(_TABLE)}
    assert set(columns) == {"workspace_id", "sid", "user_id", "revoked_at", "expires_at"}
    assert not any(c["nullable"] for c in columns.values())
    assert inspector.get_pk_constraint(_TABLE)["constrained_columns"] == ["workspace_id", "sid"]
    indexes = {i["name"]: i["column_names"] for i in inspector.get_indexes(_TABLE)}
    assert indexes["ix_browser_session_revocations_expires_at"] == [
        "workspace_id",
        "expires_at",
        "sid",
    ]
    assert inspector.get_foreign_keys(_TABLE) == []


def test_no_column_has_a_sql_default(db_engine: Engine) -> None:
    """A new table declares no SQL DEFAULT; the application supplies every value."""
    columns = sa.inspect(db_engine).get_columns(_TABLE)

    assert {c["name"]: c.get("default") for c in columns} == dict.fromkeys(
        ("workspace_id", "sid", "user_id", "revoked_at", "expires_at")
    )
    with db_engine.connect() as conn, pytest.raises(DBAPIError):
        conn.execute(
            sa.text(
                f"INSERT INTO {_TABLE} (sid, user_id, revoked_at, expires_at) "
                "VALUES ('sid-a', 'alice', 1, 2)"
            )
        )


def test_epoch_columns_hold_times_past_2038(db_engine: Engine) -> None:
    """``revoked_at`` and ``expires_at`` are 64-bit and round-trip a post-2038 time."""
    columns = {c["name"]: c for c in sa.inspect(db_engine).get_columns(_TABLE)}
    for name in ("revoked_at", "expires_at"):
        assert isinstance(columns[name]["type"], sa.BigInteger), name

    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                f"INSERT INTO {_TABLE} (workspace_id, sid, user_id, revoked_at, expires_at) "
                "VALUES (0, 'sid-a', 'alice', :now, :later)"
            ),
            {"now": 1_790_000_000, "later": _BEYOND_INT32},
        )
        stored = conn.execute(sa.text(f"SELECT expires_at FROM {_TABLE}")).scalar_one()

    assert stored == _BEYOND_INT32


def test_downgrade_drops_table(tmp_path: Path) -> None:
    """Downgrading one step removes the table; re-upgrade restores it."""
    uri = f"sqlite:///{tmp_path / 'downgrade.db'}"
    engine = get_or_create_engine(uri)
    try:
        assert _TABLE in sa.inspect(engine).get_table_names()
        config = _build_alembic_config(uri)
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.downgrade(config, _PREVIOUS_HEAD)
        assert _TABLE not in sa.inspect(engine).get_table_names()
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "78e50b68412e")
        assert _TABLE in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()
        clear_engine_cache()
