"""Schema checks for nullable host connect-generation tokens."""

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
    """Fresh SQLite database with the full migration chain applied."""
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_connect_generation_column_present_and_nullable(db_engine: Engine) -> None:
    """The migration adds a nullable BIGINT, including for legacy rows."""
    cols = sa.inspect(db_engine).get_columns("hosts")
    matches = [c for c in cols if c["name"] == "connect_generation"]
    assert len(matches) == 1, (
        f"Expected exactly one 'connect_generation' column on hosts, "
        f"got {len(matches)}. If 0, the migration didn't apply."
    )
    col = matches[0]
    assert col["nullable"], (
        "hosts.connect_generation must be NULLABLE — pre-migration rows have "
        "no connect token and would otherwise be rejected on read."
    )
    assert isinstance(col["type"], sa.BigInteger), (
        f"Expected a BIGINT type (epoch-µs exceeds int32), got {col['type']!r}."
    )


def test_downgrade_drops_connect_generation(tmp_path: Path) -> None:
    """Downgrade removes the column, restoring the prior hosts schema."""
    db_path = tmp_path / "downgrade.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)

    cols = {c["name"] for c in sa.inspect(engine).get_columns("hosts")}
    assert "connect_generation" in cols

    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "ll1a2b3c4d5e")

    remaining = {c["name"] for c in sa.inspect(engine).get_columns("hosts")}
    assert "connect_generation" not in remaining

    engine.dispose()
    clear_engine_cache()
