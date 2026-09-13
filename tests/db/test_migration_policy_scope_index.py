"""Tests for the policies index consolidation / drop-unique migration (d4c1b9e6f3a2)."""

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


def test_combined_scope_session_index_replaces_split_indexes(db_engine: Engine) -> None:
    """At head the created_at + session_id indexes collapse into one combined key."""
    indexes = {i["name"]: i for i in sa.inspect(db_engine).get_indexes("policies")}

    assert "ix_policies_created_at" not in indexes
    assert "ix_policies_session_id" not in indexes
    assert "ix_policies_scope_session" in indexes
    assert not indexes["ix_policies_scope_session"]["unique"]
    # scope leads session_id (defaults query only constrains scope); PK
    # columns bracket it — workspace_id first, id last.
    assert indexes["ix_policies_scope_session"]["column_names"] == [
        "workspace_id",
        "scope",
        "session_id",
        "id",
    ]


def test_session_name_unique_constraint_dropped(db_engine: Engine) -> None:
    """The (session_id, name_cksum) unique key is gone; the store guards names."""
    uniques = {u["name"] for u in sa.inspect(db_engine).get_unique_constraints("policies")}
    assert "uq_policies_session_id_name_cksum" not in uniques


def test_downgrade_restores_split_indexes_and_unique(tmp_path: Path) -> None:
    """Downgrade drops the combined index and restores the split indexes + key."""
    db_path = tmp_path / "downgrade.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)

    # Sanity: head state before downgrade.
    indexes = {i["name"] for i in sa.inspect(engine).get_indexes("policies")}
    assert "ix_policies_scope_session" in indexes
    assert "ix_policies_session_id" not in indexes
    assert "ix_policies_created_at" not in indexes

    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "a7f3c1b9e2d4")

    inspector = sa.inspect(engine)
    index_names = {i["name"] for i in inspector.get_indexes("policies")}
    assert "ix_policies_scope_session" not in index_names
    assert "ix_policies_session_id" in index_names
    assert "ix_policies_created_at" in index_names

    uniques = {u["name"] for u in inspector.get_unique_constraints("policies")}
    assert "uq_policies_session_id_name_cksum" in uniques

    engine.dispose()
    clear_engine_cache()
