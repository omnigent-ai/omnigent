"""Tests for the projects user_id rename (d5e6f7a8b9c0).

Verifies that after upgrade ``projects.owner_user_id`` is ``projects.user_id``
behind ``ix_projects_user_id`` and the ``ix_projects_name`` unique index, and
that downgrade restores the original column and index names with row data
intact.
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

# One step below d5e6f7a8b9c0 — the revision its downgrade lands on.
_PREVIOUS_HEAD = "c4d5e6f7a8b9"

_PROJECT_ID = "beefbeefbeefbeefbeefbeefbeefbeef"


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """Fresh SQLite database at head."""
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_projects_user_id_column_at_head(db_engine: Engine) -> None:
    """After upgrade projects exposes ``user_id``, not ``owner_user_id``."""
    cols = {c["name"] for c in sa.inspect(db_engine).get_columns("projects")}
    assert "user_id" in cols, f"Expected projects.user_id; found {cols}"
    assert "owner_user_id" not in cols, f"projects.owner_user_id should be gone; found {cols}"


def test_projects_indexes_at_head(db_engine: Engine) -> None:
    """The owner-scope index is renamed and both indexes cover ``user_id``."""
    indexes = {i["name"]: i for i in sa.inspect(db_engine).get_indexes("projects")}
    assert "ix_projects_user_id" in indexes, f"Expected ix_projects_user_id; found {set(indexes)}"
    assert "ix_projects_owner_user_id" not in indexes, (
        f"ix_projects_owner_user_id should be gone; found {set(indexes)}"
    )
    # created_at trails the equality columns so "list my projects" sorts from
    # the index; id completes the key.
    assert indexes["ix_projects_user_id"]["column_names"] == [
        "workspace_id",
        "user_id",
        "created_at",
        "id",
    ]
    # ix_projects_name keeps its name (the store's _is_name_conflict matches on
    # it) but now covers user_id, and must still be UNIQUE.
    name_idx = indexes["ix_projects_name"]
    assert name_idx["unique"], "ix_projects_name must stay UNIQUE"
    assert name_idx["column_names"] == ["workspace_id", "user_id", "name"]


def test_downgrade_restores_owner_user_id(tmp_path: Path) -> None:
    """Downgrade one step restores ``owner_user_id`` with data intact.

    Insert a project at head (``user_id``), downgrade (which renames the column
    back), and confirm the old column/index names are restored and the identity
    value survived. A final re-upgrade proves the rename is replayable.
    """
    db_path = tmp_path / "downgrade.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)

    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO projects (workspace_id, id, name, user_id, created_at, updated_at) "
                f"VALUES (0, X'{_PROJECT_ID}', 'launch', 'alice@example.com', 1700000000, NULL)"
            )
        )
        conn.commit()

    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, _PREVIOUS_HEAD)

    inspector = sa.inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("projects")}
    assert "owner_user_id" in cols and "user_id" not in cols, (
        f"projects.owner_user_id must be restored and user_id gone; found {cols}"
    )
    idx = {i["name"] for i in inspector.get_indexes("projects")}
    assert "ix_projects_owner_user_id" in idx, (
        f"ix_projects_owner_user_id must be restored; found {idx}"
    )
    assert "ix_projects_user_id" not in idx

    with engine.connect() as conn:
        owner = conn.execute(
            sa.text(f"SELECT owner_user_id FROM projects WHERE id = X'{_PROJECT_ID}'")
        ).scalar_one_or_none()
    assert owner == "alice@example.com", f"owner value must survive downgrade; got {owner!r}"

    # Re-upgrade to head: user_id is back and the value survives both hops.
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "head")

    inspector = sa.inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("projects")}
    assert "user_id" in cols and "owner_user_id" not in cols, (
        f"projects.user_id must be restored on re-upgrade; found {cols}"
    )
    with engine.connect() as conn:
        user_id = conn.execute(
            sa.text(f"SELECT user_id FROM projects WHERE id = X'{_PROJECT_ID}'")
        ).scalar_one_or_none()
    assert user_id == "alice@example.com", (
        f"user_id value must survive the full round-trip; got {user_id!r}"
    )

    engine.dispose()
    clear_engine_cache()
