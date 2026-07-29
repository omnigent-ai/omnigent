"""Tests for the ``conversations.archived`` column and its migration.

Per the archive-session feature: archived sessions are hidden from the
default ``GET /v1/sessions`` listing. The column is NOT NULL with a
``server_default`` of false so existing rows backfill to not-archived
when the migration applies to a populated database.

The schema split (aa1b2c3d4e5f) moved ``archived`` onto
``omnigent_conversation_metadata``. Migration ``9d820f91deef`` moves it back
onto ``conversations`` so ``list_conversations`` can filter it inline next to
the created_at/updated_at sort keys; these tests assert the post-move state
and the backfill.
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

# Revision just before ``archived`` was moved back to conversations.
_PRE_MOVE_REVISION = "cc3d4e5f6a7b"
_MOVE_REVISION = "9d820f91deef"


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
    The migration creates ``conversations.archived`` as a NOT NULL boolean and
    removes it from ``omnigent_conversation_metadata``.

    A failure on presence means the migration didn't apply — the ORM mapping
    would then crash on every conversation read. NOT NULL matters because the
    listing filter (``archived IS false``) and the entity field (``bool``)
    both assume a concrete value, never NULL.
    """
    insp = sa.inspect(db_engine)
    conv_cols = [c for c in insp.get_columns("conversations") if c["name"] == "archived"]
    assert len(conv_cols) == 1, (
        f"Expected exactly one 'archived' column on conversations, got "
        f"{len(conv_cols)}. If 0, the migration didn't apply."
    )
    assert not conv_cols[0]["nullable"], (
        "conversations.archived must be NOT NULL — the listing filter and "
        "entity field both assume a concrete true/false value."
    )
    meta_cols = [
        c for c in insp.get_columns("omnigent_conversation_metadata") if c["name"] == "archived"
    ]
    assert not meta_cols, (
        "archived must no longer exist on omnigent_conversation_metadata after the move."
    )


def test_archived_index_present(db_engine: Engine) -> None:
    """The default-sidebar support index lives on ``conversations``."""
    idx = {i["name"] for i in sa.inspect(db_engine).get_indexes("conversations")}
    assert "ix_conversations_archived_updated" in idx


def test_archived_defaults_false_on_insert(db_engine: Engine) -> None:
    """
    An insert that omits ``archived`` lands as false (0).

    This exercises the ``server_default`` clause — the same default that
    backfills pre-existing rows when the column is added to a populated
    table. If it regressed, a NOT NULL insert without the column would fail,
    or existing sessions would come back archived (vanished from the sidebar)
    after the upgrade.
    """
    with db_engine.connect() as conn:
        # root_conversation_id is a NOT NULL self-FK; a top-level row's
        # root is its own id, so bind :id for both.
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, root_conversation_id) "
                "VALUES (:id, :ts, :ts, :id)"
            ),
            {"id": "a286a63f38f8f5fdf8ccf8486ade862a", "ts": 1700000000},
        )
        conn.commit()
        value = conn.execute(
            sa.text("SELECT archived FROM conversations WHERE id = :id"),
            {"id": "a286a63f38f8f5fdf8ccf8486ade862a"},
        ).scalar_one()
        assert value == 0, f"Expected archived to default to 0 (false); got {value!r}."


def test_archived_backfilled_from_metadata(tmp_path: Path) -> None:
    """
    The move migration copies each row's ``archived`` value from
    ``omnigent_conversation_metadata`` onto ``conversations``.

    Seed rows at the pre-move revision (archived still on metadata), apply the
    move, and assert the AP column matches the original metadata value.
    """
    uri = f"sqlite:///{tmp_path / 'backfill.db'}"
    cfg = _build_alembic_config(uri)
    raw_engine = sa.create_engine(uri)
    try:
        id_a = "94c349190e241f85a984b3df8f129696"
        id_b = "bfcc6c068875253adf2f20bf30a19015"
        with raw_engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, _PRE_MOVE_REVISION)
            conn.execute(
                sa.text(
                    "INSERT INTO conversations "
                    "(id, created_at, updated_at, root_conversation_id) "
                    "VALUES (:a, 1, 1, :a), (:b, 2, 2, :b)"
                ),
                {"a": id_a, "b": id_b},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO omnigent_conversation_metadata (id, kind, archived) "
                    "VALUES (:a, 1, 1), (:b, 1, 0)"
                ),
                {"a": id_a, "b": id_b},
            )
        with raw_engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, _MOVE_REVISION)
        with raw_engine.connect() as conn:
            rows = dict(
                conn.execute(sa.text("SELECT id, archived FROM conversations ORDER BY id")).all()
            )
        assert rows == {id_a: 1, id_b: 0}
    finally:
        raw_engine.dispose()
        clear_engine_cache()
