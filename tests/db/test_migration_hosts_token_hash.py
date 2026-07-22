"""Tests for the hosts token_hash drop-unique migration (f82e866d9de0).

Verifies that at head ``uq_hosts_token_hash`` is gone (the launch-token auth
path resolves by the ``(workspace_id, host_id)`` PK and matches the digest in
Python), that the ``(workspace_id, user_id, name)`` uniqueness is untouched, and
that downgrade restores the constraint.
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


def test_token_hash_unique_dropped_at_head(db_engine: Engine) -> None:
    """At head the (workspace_id, token_hash) unique key is gone."""
    uniques = {u["name"] for u in sa.inspect(db_engine).get_unique_constraints("hosts")}
    assert "uq_hosts_token_hash" not in uniques
    # The user_id/name uniqueness that guards host_id rotation is untouched.
    assert "uq_hosts_workspace_user_id_name" in uniques


def test_downgrade_restores_token_hash_unique(tmp_path: Path) -> None:
    """Downgrade restores the (workspace_id, token_hash) unique constraint."""
    db_path = tmp_path / "downgrade.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)

    # Sanity: head state before downgrade.
    uniques = {u["name"] for u in sa.inspect(engine).get_unique_constraints("hosts")}
    assert "uq_hosts_token_hash" not in uniques

    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "d4c1b9e6f3a2")

    restored = {u["name"] for u in sa.inspect(engine).get_unique_constraints("hosts")}
    assert "uq_hosts_token_hash" in restored
    assert "uq_hosts_workspace_owner_name" in restored

    engine.dispose()
    clear_engine_cache()
