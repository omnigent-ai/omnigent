"""Tests for the ``conversations.archived`` column and its migration.

Per the archive-session feature: archived sessions are hidden from the
default ``GET /v1/sessions`` listing. The column is NOT NULL with a
``server_default`` of false so existing rows backfill to not-archived
when the migration applies to a populated database.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from omnigent.db.utils import clear_engine_cache, get_or_create_engine


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """
    Fresh SQLite DB with the full alembic chain applied; cleaned up after.

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


def test_archived_column_present_and_not_nullable(db_engine: Engine) -> None:
    """
    The migration creates ``conversations.archived`` as a NOT NULL
    boolean.

    A failure on presence means the migration didn't apply — the ORM
    mapping would then crash on every conversation read. NOT NULL
    matters because the listing filter (``archived IS false``) and the
    entity field (``bool``) both assume a concrete value, never NULL.
    """
    cols = sa.inspect(db_engine).get_columns("conversations")
    archived_cols = [c for c in cols if c["name"] == "archived"]
    assert len(archived_cols) == 1, (
        f"Expected exactly one 'archived' column on conversations, "
        f"got {len(archived_cols)}. If 0, the migration didn't apply."
    )
    assert not archived_cols[0]["nullable"], (
        "conversations.archived must be NOT NULL — the listing filter "
        "and entity field both assume a concrete true/false value."
    )


def test_archived_defaults_false_on_insert(db_engine: Engine) -> None:
    """
    An insert that omits ``archived`` lands as false (0).

    This exercises the ``server_default`` clause — the same default
    that backfills pre-existing rows when the column is added to a
    populated table. If it regressed, a NOT NULL insert without the
    column would fail, or existing sessions would come back archived
    (vanished from the sidebar) after the upgrade.
    """
    with db_engine.connect() as conn:
        # root_conversation_id is a NOT NULL self-FK; a top-level row's
        # root is its own id, so bind :id for both.
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, kind, root_conversation_id) "
                "VALUES (:id, :ts, :ts, 'default', :id)"
            ),
            {"id": "conv_arch_default", "ts": 1700000000},
        )
        conn.commit()
        value = conn.execute(
            sa.text("SELECT archived FROM conversations WHERE id = :id"),
            {"id": "conv_arch_default"},
        ).scalar_one()
        assert value == 0, f"Expected archived to default to 0 (false); got {value!r}."
