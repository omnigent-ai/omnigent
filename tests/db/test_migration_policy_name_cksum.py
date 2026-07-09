"""Tests for the policies.name_cksum migration (x1a2b3c4d5e6)."""

from __future__ import annotations

import hashlib
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


def test_name_cksum_column_exists_and_is_not_nullable(db_engine: Engine) -> None:
    """policies.name_cksum is a NOT NULL binary column after the migration."""
    columns = {c["name"]: c for c in sa.inspect(db_engine).get_columns("policies")}
    assert "name_cksum" in columns, "policies.name_cksum column must exist after migration"
    assert not columns["name_cksum"]["nullable"], "policies.name_cksum must be NOT NULL"


def test_name_indexes_key_on_checksum(db_engine: Engine) -> None:
    """Both name-uniqueness structures key on name_cksum, not name."""
    inspector = sa.inspect(db_engine)

    indexes = {i["name"]: i for i in inspector.get_indexes("policies")}
    # At head the partial unique index is replaced by a plain name_cksum
    # lookup index (see z5a2b3c4d5e6); default-name uniqueness lives in the
    # store. It still keys on name_cksum, not the raw name.
    assert "ix_policies_default_name_cksum" not in indexes
    assert "ix_policies_name_cksum" in indexes
    assert not indexes["ix_policies_name_cksum"]["unique"]
    # Leads with workspace_id (PK inclusion), keys on name_cksum, ends with id.
    assert indexes["ix_policies_name_cksum"]["column_names"] == [
        "workspace_id",
        "name_cksum",
        "id",
    ]
    # The old raw-name index is gone.
    assert "ix_policies_default_name" not in indexes

    uniques = {u["name"]: u for u in inspector.get_unique_constraints("policies")}
    assert "uq_policies_session_id_name_cksum" in uniques
    assert uniques["uq_policies_session_id_name_cksum"]["column_names"] == [
        "workspace_id",
        "session_id",
        "name_cksum",
    ]
    assert "uq_policies_session_id_name" not in uniques


def test_backfill_computes_sha256_of_name(tmp_path: Path) -> None:
    """Rows present before x1a2b3c4d5e6 get name_cksum = sha256(name)."""
    db_path = tmp_path / "backfill.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)

    # Stop just below this migration (name_cksum not added yet), insert a row
    # without it, then upgrade so the migration's Python back-fill runs.
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "w1a2b3c4d5e6")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                # scope=1 default, type=1 python (int codes at this revision).
                "INSERT INTO policies"
                " (workspace_id, id, name, session_id, scope, created_at, type, handler, enabled)"
                " VALUES (0, 'pol_bf1', 'legacy_name', NULL, 1, 1, 1, 'mod.f', 1)"
            )
        )
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "x1a2b3c4d5e6")

    with engine.begin() as conn:
        cksum = conn.execute(
            sa.text("SELECT name_cksum FROM policies WHERE id = 'pol_bf1'")
        ).scalar_one()
    assert bytes(cksum) == hashlib.sha256(b"legacy_name").digest()

    engine.dispose()
    clear_engine_cache()


def test_downgrade_restores_name_index_and_drops_checksum(tmp_path: Path) -> None:
    """Downgrade drops name_cksum and restores the raw-name index/constraint."""
    db_path = tmp_path / "downgrade.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)

    # Start at head (includes the name_cksum migration x1a2b3c4d5e6).
    assert "name_cksum" in {c["name"] for c in sa.inspect(engine).get_columns("policies")}

    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "w1a2b3c4d5e6")

    inspector = sa.inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("policies")}
    assert "name_cksum" not in columns, "name_cksum must be dropped by downgrade"

    index_names = {i["name"] for i in inspector.get_indexes("policies")}
    assert "ix_policies_default_name_cksum" not in index_names
    assert "ix_policies_default_name" in index_names, "raw-name index must be restored"

    uniques = {u["name"] for u in inspector.get_unique_constraints("policies")}
    assert "uq_policies_session_id_name_cksum" not in uniques
    assert "uq_policies_session_id_name" in uniques, "raw-name constraint must be restored"

    engine.dispose()
    clear_engine_cache()
