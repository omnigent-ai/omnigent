"""Tests for the cross-device user preferences migration."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlUser
from omnigent.db.utils import _build_alembic_config

_PREVIOUS_HEAD = "gb1b2c3d4e5f"
_PREFERENCES_REVISION = "c7e4d9a1b2f6"


def test_users_preferences_column_is_nullable_binary(db_uri: str) -> None:
    """The head schema carries an optional compressed preferences column."""
    engine = sa.create_engine(db_uri)
    try:
        columns = {column["name"]: column for column in sa.inspect(engine).get_columns("users")}
    finally:
        engine.dispose()

    assert columns["preferences"]["nullable"] is True
    assert isinstance(columns["preferences"]["type"], sa.LargeBinary)


def test_preferences_migration_round_trips_and_preserves_null_semantics(tmp_path: Path) -> None:
    """Upgrade and downgrade preserve users while NULL remains uninitialized."""
    uri = f"sqlite:///{tmp_path / 'preferences-migration.db'}"
    config = _build_alembic_config(uri)
    command.upgrade(config, _PREVIOUS_HEAD)

    engine = sa.create_engine(uri)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO users (workspace_id, id, is_admin) "
                "VALUES (0, 'alice@example.com', false)"
            )
        )
    engine.dispose()

    command.upgrade(config, _PREFERENCES_REVISION)
    engine = sa.create_engine(uri)
    with Session(engine) as session:
        alice = session.get(SqlUser, (0, "alice@example.com"))
        assert alice is not None
        assert alice.preferences is None
        alice.preferences = '{"settings":{},"version":1}'
        session.commit()

    with engine.connect() as connection:
        stored = connection.scalar(
            sa.text("SELECT preferences FROM users WHERE id = 'alice@example.com'")
        )
    assert isinstance(stored, bytes)
    assert stored != b'{"settings":{},"version":1}'
    engine.dispose()

    command.downgrade(config, _PREVIOUS_HEAD)
    engine = sa.create_engine(uri)
    assert "preferences" not in {
        column["name"] for column in sa.inspect(engine).get_columns("users")
    }
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT id FROM users")) == "alice@example.com"
    engine.dispose()

    command.upgrade(config, _PREFERENCES_REVISION)
    engine = sa.create_engine(uri)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text("SELECT preferences FROM users WHERE id = 'alice@example.com'")
            )
            is None
        )
    engine.dispose()
