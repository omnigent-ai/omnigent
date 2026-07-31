"""Tests for the hosts gateway_inference migration (d5e6f7a8b9c0).

Verifies the nullable ``hosts.gateway_inference`` column exists at head and that
downgrade removes it, so the Smart-Routing capability map has somewhere to land
without breaking the rest of the chain.
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
    """Fresh SQLite database with the full migration chain applied."""
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_gateway_inference_column_nullable_at_head(db_engine: Engine) -> None:
    columns = {c["name"]: c for c in sa.inspect(db_engine).get_columns("hosts")}
    assert "gateway_inference" in columns
    assert columns["gateway_inference"]["nullable"] is True


def test_downgrade_drops_gateway_inference(tmp_path: Path) -> None:
    db_path = tmp_path / "downgrade.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)

    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "c4d5e6f7a8b9")

    columns = {c["name"] for c in sa.inspect(engine).get_columns("hosts")}
    assert "gateway_inference" not in columns
    # The sibling readiness column is untouched by this migration.
    assert "configured_harnesses" in columns

    engine.dispose()
    clear_engine_cache()
