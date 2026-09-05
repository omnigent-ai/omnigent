"""Tests for the ``hosts.connect_generation`` column (gb2c3d4e5f6a).

The column holds the epoch-microseconds token stamped by each connect's
``upsert_on_connect``. Cleanup paths that hold no live registry connection
mark the row offline via a compare-and-update against this token
(``HostStore.set_offline_if_generation``), so a superseded connection
cannot overwrite a newer connect's online row. NULL means the row was
last written before the column existed — a NULL token never matches, so
legacy rows are never conditionally offlined.
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
    """Fresh SQLite database with the full migration chain applied.

    :param tmp_path: Pytest-managed temp directory for the SQLite file.
    :returns: Engine pointed at the migrated database.
    """
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_connect_generation_column_present_and_nullable(db_engine: Engine) -> None:
    """The migration adds ``hosts.connect_generation`` as nullable BIGINT.

    (1) The column must exist — without it every connect's upsert crashes
    on the ORM mapping. (2) It must be nullable — rows last written before
    the column existed carry no token, and NULL-never-matches is the
    documented legacy behaviour. (3) It must be a big integer: epoch-µs
    exceeds int32.
    """
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

    # Sanity: head state before downgrade.
    cols = {c["name"] for c in sa.inspect(engine).get_columns("hosts")}
    assert "connect_generation" in cols

    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "gb1b2c3d4e5f")

    remaining = {c["name"] for c in sa.inspect(engine).get_columns("hosts")}
    assert "connect_generation" not in remaining

    engine.dispose()
    clear_engine_cache()
