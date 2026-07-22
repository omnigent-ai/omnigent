"""Tests for the user_id-unification migration (b3c1a2d4e5f6).

Verifies that after upgrade the session-owner identity columns match the
schema-wide ``user_id`` convention: ``hosts.owner`` is now ``hosts.user_id``
(``VARCHAR(128)``) behind ``uq_hosts_workspace_user_id_name``, and
``scheduled_tasks.owner_user_id`` is now ``scheduled_tasks.user_id`` behind
``ix_scheduled_tasks_user_id``. Downgrade restores the original ``owner`` /
``owner_user_id`` names (and their constraint/index names) with row data intact.
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

# One step below b3c1a2d4e5f6 — the revision its downgrade lands on.
_PREVIOUS_HEAD = "f82e866d9de0"


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """Fresh SQLite database at head (b3c1a2d4e5f6)."""
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_hosts_user_id_column_at_head(db_engine: Engine) -> None:
    """After upgrade hosts exposes ``user_id`` (VARCHAR(128)), not ``owner``."""
    cols = {c["name"]: c for c in sa.inspect(db_engine).get_columns("hosts")}
    assert "user_id" in cols, f"Expected hosts.user_id; found {set(cols)}"
    assert "owner" not in cols, f"hosts.owner should be gone; found {set(cols)}"
    # Narrowed to the schema-wide 128-char width for user-identity columns.
    assert "128" in str(cols["user_id"]["type"]).upper(), (
        f"Expected VARCHAR(128); got {cols['user_id']['type']}"
    )


def test_hosts_unique_constraint_renamed(db_engine: Engine) -> None:
    """The hosts unique key is ``uq_hosts_workspace_user_id_name`` at head."""
    uqs = sa.inspect(db_engine).get_unique_constraints("hosts")
    names = {u["name"] for u in uqs}
    assert "uq_hosts_workspace_user_id_name" in names, (
        f"Expected uq_hosts_workspace_user_id_name; found {names}"
    )
    assert "uq_hosts_workspace_owner_name" not in names, (
        f"uq_hosts_workspace_owner_name should be gone; found {names}"
    )
    uq = next(u for u in uqs if u["name"] == "uq_hosts_workspace_user_id_name")
    assert set(uq["column_names"]) == {"workspace_id", "user_id", "name"}


def test_scheduled_tasks_user_id_column_at_head(db_engine: Engine) -> None:
    """After upgrade scheduled_tasks exposes ``user_id``, not ``owner_user_id``."""
    cols = {c["name"] for c in sa.inspect(db_engine).get_columns("scheduled_tasks")}
    assert "user_id" in cols, f"Expected scheduled_tasks.user_id; found {cols}"
    assert "owner_user_id" not in cols, (
        f"scheduled_tasks.owner_user_id should be gone; found {cols}"
    )


def test_scheduled_tasks_index_renamed(db_engine: Engine) -> None:
    """The scheduled_tasks owner index is ``ix_scheduled_tasks_user_id`` at head."""
    idx = {i["name"] for i in sa.inspect(db_engine).get_indexes("scheduled_tasks")}
    assert "ix_scheduled_tasks_user_id" in idx, f"Expected ix_scheduled_tasks_user_id; found {idx}"
    assert "ix_scheduled_tasks_owner_user_id" not in idx, (
        f"ix_scheduled_tasks_owner_user_id should be gone; found {idx}"
    )


def test_downgrade_restores_old_names(tmp_path: Path) -> None:
    """Downgrade one step restores ``owner`` / ``owner_user_id`` with data intact.

    Insert a host row at head (``user_id``), downgrade to the previous revision
    (which renames the columns back), and confirm the old names/constraint/index
    are restored and the row's identity value survived the round-trip. A final
    re-upgrade proves the rename is replayable and the value survives both hops.
    """
    db_path = tmp_path / "downgrade.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)

    # Insert at head using the new column name. status is SmallInteger
    # (u1a2b3c4d5e6 converted it): online=1.
    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO hosts "
                "(workspace_id, user_id, name, host_id, status, created_at, updated_at) "
                "VALUES (0, 'alice@example.com', 'laptop',"
                " 'c0ffee00c0ffee00c0ffee00c0ffee00', 1, "
                "1700000000, 1700000001)"
            )
        )
        conn.commit()

    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, _PREVIOUS_HEAD)

    inspector = sa.inspect(engine)

    # hosts: owner restored, user_id gone, old unique constraint restored.
    host_cols = {c["name"] for c in inspector.get_columns("hosts")}
    assert "owner" in host_cols and "user_id" not in host_cols, (
        f"hosts.owner must be restored and user_id gone; found {host_cols}"
    )
    host_uqs = {u["name"] for u in inspector.get_unique_constraints("hosts")}
    assert "uq_hosts_workspace_owner_name" in host_uqs, (
        f"uq_hosts_workspace_owner_name must be restored; found {host_uqs}"
    )
    assert "uq_hosts_workspace_user_id_name" not in host_uqs

    # scheduled_tasks: owner_user_id restored, user_id gone, old index restored.
    task_cols = {c["name"] for c in inspector.get_columns("scheduled_tasks")}
    assert "owner_user_id" in task_cols and "user_id" not in task_cols, (
        f"scheduled_tasks.owner_user_id must be restored and user_id gone; found {task_cols}"
    )
    task_idx = {i["name"] for i in inspector.get_indexes("scheduled_tasks")}
    assert "ix_scheduled_tasks_owner_user_id" in task_idx, (
        f"ix_scheduled_tasks_owner_user_id must be restored; found {task_idx}"
    )
    assert "ix_scheduled_tasks_user_id" not in task_idx

    # The pre-inserted row survived the rename; read it back via the old name.
    with engine.connect() as conn:
        owner = conn.execute(
            sa.text("SELECT owner FROM hosts WHERE host_id = 'c0ffee00c0ffee00c0ffee00c0ffee00'")
        ).scalar_one_or_none()
    assert owner == "alice@example.com", f"owner value must survive downgrade; got {owner!r}"

    # Re-upgrade to head: user_id is back, owner gone, and the value survives.
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "head")

    inspector = sa.inspect(engine)
    host_cols = {c["name"] for c in inspector.get_columns("hosts")}
    assert "user_id" in host_cols and "owner" not in host_cols, (
        f"hosts.user_id must be restored on re-upgrade; found {host_cols}"
    )
    with engine.connect() as conn:
        user_id = conn.execute(
            sa.text("SELECT user_id FROM hosts WHERE host_id = 'c0ffee00c0ffee00c0ffee00c0ffee00'")
        ).scalar_one_or_none()
    assert user_id == "alice@example.com", (
        f"user_id value must survive the full round-trip; got {user_id!r}"
    )

    engine.dispose()
    clear_engine_cache()
