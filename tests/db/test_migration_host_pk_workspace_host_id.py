"""Tests for the hosts PK migration to (workspace_id, host_id) (v1a2b3c4d5e6).

Verifies that after upgrade the PK is (workspace_id, host_id) with
uq_hosts_workspace_owner_name in place, and that downgrade restores the
original (workspace_id, owner, name) PK with uq_hosts_host_id.
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
    """Fresh SQLite database at head (v1a2b3c4d5e6)."""
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_pk_is_workspace_id_and_host_id(db_engine: Engine) -> None:
    """After upgrade the hosts PK must be (workspace_id, host_id)."""
    pk = sa.inspect(db_engine).get_pk_constraint("hosts")
    assert pk["constrained_columns"] == ["workspace_id", "host_id"], (
        f"Expected PK (workspace_id, host_id); got {pk['constrained_columns']}"
    )


def test_unique_constraint_on_workspace_owner_name(db_engine: Engine) -> None:
    """After upgrade uq_hosts_workspace_owner_name must exist."""
    uqs = sa.inspect(db_engine).get_unique_constraints("hosts")
    names = {u["name"] for u in uqs}
    assert "uq_hosts_workspace_owner_name" in names, (
        f"Expected uq_hosts_workspace_owner_name; found {names}"
    )
    uq = next(u for u in uqs if u["name"] == "uq_hosts_workspace_owner_name")
    assert set(uq["column_names"]) == {"workspace_id", "owner", "name"}


def test_old_unique_constraint_dropped(db_engine: Engine) -> None:
    """After upgrade uq_hosts_host_id must no longer exist (host_id is in PK)."""
    uqs = sa.inspect(db_engine).get_unique_constraints("hosts")
    names = {u["name"] for u in uqs}
    assert "uq_hosts_host_id" not in names, (
        f"uq_hosts_host_id should have been dropped; found {names}"
    )


def test_data_survives_upgrade(tmp_path: Path) -> None:
    """Existing host rows must survive the table recreate intact."""
    db_path = tmp_path / "data.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)

    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO hosts "
                "(workspace_id, owner, name, host_id, status, created_at, updated_at) "
                "VALUES (0, 'alice@example.com', 'laptop', 'host_abc', 1, "
                "1700000000, 1700000001)"
            )
        )
        conn.commit()

    row = (
        engine.connect()
        .execute(sa.text("SELECT owner, name, host_id FROM hosts WHERE host_id = 'host_abc'"))
        .fetchone()
    )
    assert row is not None
    assert row[0] == "alice@example.com"
    assert row[1] == "laptop"
    assert row[2] == "host_abc"

    engine.dispose()
    clear_engine_cache()


def test_downgrade_restores_old_pk(tmp_path: Path) -> None:
    """Downgrade to u1a2b3c4d5e6 must restore PK (workspace_id, owner, name)."""
    db_path = tmp_path / "downgrade.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)

    # Insert a row at head before downgrading.
    # status is SmallInteger at head (u1a2b3c4d5e6 converted it): offline=2.
    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO hosts "
                "(workspace_id, owner, name, host_id, status, created_at, updated_at) "
                "VALUES (0, 'bob@example.com', 'workstation', 'host_xyz', 2, "
                "1700000002, 1700000003)"
            )
        )
        conn.commit()

    # Downgrade only v1a2b3c4d5e6 (our migration); stop at u1a2b3c4d5e6
    # to avoid the enums migration's intermediate status_str column.
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "u1a2b3c4d5e6")

    inspector = sa.inspect(engine)
    pk = inspector.get_pk_constraint("hosts")
    assert pk["constrained_columns"] == ["workspace_id", "owner", "name"], (
        f"Expected old PK (workspace_id, owner, name) after downgrade; "
        f"got {pk['constrained_columns']}"
    )

    uqs = inspector.get_unique_constraints("hosts")
    uq_names = {u["name"] for u in uqs}
    assert "uq_hosts_host_id" in uq_names, (
        f"uq_hosts_host_id must be restored after downgrade; found {uq_names}"
    )
    assert "uq_hosts_workspace_owner_name" not in uq_names, (
        f"uq_hosts_workspace_owner_name must be gone after downgrade; found {uq_names}"
    )

    # Data survives the round-trip.
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT host_id FROM hosts WHERE owner = 'bob@example.com'")
        ).scalar_one_or_none()
    assert row == "host_xyz", f"host_id must survive downgrade; got {row!r}"

    engine.dispose()
    clear_engine_cache()
