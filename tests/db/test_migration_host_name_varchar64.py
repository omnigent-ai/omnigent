"""Tests for the hosts.name VARCHAR(64) migration (t1a2b3c4d5e6).

Verifies that after upgrade the column is VARCHAR(64) and NOT NULL, and
that downgrade restores it to VARCHAR(256).
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


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """Fresh SQLite database with the full migration chain applied (including t1a2b3c4d5e6)."""
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_hosts_name_column_is_varchar64(db_engine: Engine) -> None:
    """After upgrade, hosts.name must be VARCHAR(64) and NOT NULL."""
    cols = sa.inspect(db_engine).get_columns("hosts")
    name_cols = [c for c in cols if c["name"] == "name"]
    assert len(name_cols) == 1, "Expected exactly one 'name' column on hosts"
    col = name_cols[0]
    assert not col["nullable"], "hosts.name must be NOT NULL after the migration"
    col_type = col["type"]
    assert isinstance(col_type, sa.String), (
        f"hosts.name must be a String type; got {type(col_type)}"
    )
    assert col_type.length == 64, f"hosts.name must be VARCHAR(64); got VARCHAR({col_type.length})"


def test_downgrade_restores_varchar256(tmp_path: Path) -> None:
    """Downgrade to s1a2b3c4d5e6 widens hosts.name back to VARCHAR(256)."""
    db_path = tmp_path / "downgrade.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)

    # Insert a host row at head (t1a2b3c4d5e6).
    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO hosts "
                "(workspace_id, user_id, name, host_id, status, created_at, updated_at) "
                "VALUES (0, 'user@example.com', 'my-laptop',"
                " '4f64b6ee625f4e8259185c35c6e63f3d', 1, "
                "1700000000, 1700000001)"
            )
        )
        conn.commit()

    # Downgrade to s1a2b3c4d5e6 (below the enums→SMALLINT migration that now
    # sits above t1, which converts hosts.status back to a string on the way).
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "s1a2b3c4d5e6")

    inspector = sa.inspect(engine)
    name_col = next(c for c in inspector.get_columns("hosts") if c["name"] == "name")
    assert not name_col["nullable"], "hosts.name must still be NOT NULL after downgrade"
    col_type = name_col["type"]
    assert isinstance(col_type, sa.String), (
        f"hosts.name must be a String type after downgrade; got {type(col_type)}"
    )
    assert col_type.length == 256, (
        f"hosts.name must be VARCHAR(256) after downgrade; got VARCHAR({col_type.length})"
    )

    # Existing data should survive the round-trip.
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT name FROM hosts WHERE owner = 'user@example.com'")
        ).scalar_one_or_none()
    assert row == "my-laptop", f"Existing host name should survive downgrade; got {row!r}"

    engine.dispose()
    clear_engine_cache()
