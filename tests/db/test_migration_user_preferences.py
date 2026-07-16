"""Tests for the user_preferences migration (b1c2d3e4f5a6).

Verifies the migration creates the table with the expected shape, that the
primary key is the ``(workspace_id, user_id, key)`` triple that scopes every
read to one tenant and one user, that the table carries no database-level
foreign key (schema Rule R032 — the ``user_id`` relationship is
application-owned), and that a downgrade drops the table cleanly.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine

from omnigent.db.utils import (
    _build_alembic_config,
    clear_engine_cache,
    get_or_create_engine,
)

_PREVIOUS_HEAD = "a7b3c4d5e6f7"


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """Fresh SQLite DB with the full migration chain applied; cleaned up after."""
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_migration_creates_table(db_engine: Engine) -> None:
    """``user_preferences`` exists after migrating to head."""
    assert "user_preferences" in set(sa.inspect(db_engine).get_table_names())


def test_user_preferences_columns(db_engine: Engine) -> None:
    """``user_preferences`` has the full expected column set."""
    cols = {c["name"] for c in sa.inspect(db_engine).get_columns("user_preferences")}
    assert cols == {"workspace_id", "user_id", "key", "value", "updated_at"}


def test_primary_key_is_workspace_user_key(db_engine: Engine) -> None:
    """The PK scopes every row to one tenant, one user, one key."""
    pk = sa.inspect(db_engine).get_pk_constraint("user_preferences")
    assert pk["constrained_columns"] == ["workspace_id", "user_id", "key"]


def test_no_foreign_keys(db_engine: Engine) -> None:
    """No DB-level FK on ``user_id`` (schema Rule R032)."""
    assert sa.inspect(db_engine).get_foreign_keys("user_preferences") == []


def test_downgrade_drops_table(tmp_path: Path) -> None:
    """Downgrading one step removes the table; re-upgrade restores it."""
    db_path = tmp_path / "downgrade.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        assert "user_preferences" in set(sa.inspect(engine).get_table_names())

        config = _build_alembic_config(uri)
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.downgrade(config, _PREVIOUS_HEAD)
        assert "user_preferences" not in set(sa.inspect(engine).get_table_names())

        # Re-upgrade restores it — proves the upgrade is replayable.
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "b1c2d3e4f5a6")
        assert "user_preferences" in set(sa.inspect(engine).get_table_names())
    finally:
        clear_engine_cache()
