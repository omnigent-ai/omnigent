"""Tests for making the conversation_items position index plain.

``ix_conversation_items_conversation_id_position`` was UNIQUE on
``(workspace_id, conversation_id, position, created_at)``. Since strict position
uniqueness is owned by the app-level ``next_position`` allocator (the DB key only
enforced it per epoch-second), migration ``c7d2e9f4a1b8`` drops the UNIQUE flag
and the ``created_at`` tail, leaving a plain ``(workspace_id, conversation_id,
position)`` index. These tests assert the post-migration shape and the downgrade.
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

_NEW_REVISION = "c7d2e9f4a1b8"
# Revision just before this one (its down_revision).
_PRE_REVISION = "a2b7c3d8e4f9"
_INDEX = "ix_conversation_items_conversation_id_position"


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """Fresh SQLite DB with the full alembic chain applied (at head)."""
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    try:
        yield engine
    finally:
        clear_engine_cache()


def _position_index(engine: Engine) -> dict:
    idx = {i["name"]: i for i in sa.inspect(engine).get_indexes("conversation_items")}
    assert _INDEX in idx, f"{_INDEX} missing on conversation_items."
    return idx[_INDEX]


def test_position_index_is_plain_at_head(db_engine: Engine) -> None:
    """
    At head the position index is non-unique and keyed on (ws, conv, position).

    A failure means the migration didn't apply (still UNIQUE / still carries
    created_at). The PK — which is what keeps the table partition-ready — must
    still carry created_at.
    """
    idx = _position_index(db_engine)
    assert not idx["unique"], f"{_INDEX} should be non-unique after the migration."
    assert idx["column_names"] == [
        "workspace_id",
        "conversation_id",
        "position",
    ], f"unexpected columns: {idx['column_names']}"

    pk_cols = sa.inspect(db_engine).get_pk_constraint("conversation_items")["constrained_columns"]
    assert "created_at" in pk_cols, (
        f"conversation_items PK must still carry created_at (partition-readiness); got {pk_cols}"
    )


def test_downgrade_restores_unique_created_at_index(tmp_path: Path) -> None:
    """
    Downgrade restores the UNIQUE ``(…, position, created_at)`` shape.

    The downgrade leg is otherwise uncovered (the engine fixtures only run
    ``upgrade head``), so this proves the chain stays reversible.
    """
    uri = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    cfg = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    try:
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, _NEW_REVISION)
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, _PRE_REVISION)

        idx = _position_index(engine)
        assert idx["unique"], f"downgrade must restore UNIQUE on {_INDEX}."
        assert idx["column_names"] == [
            "workspace_id",
            "conversation_id",
            "position",
            "created_at",
        ], f"downgrade must restore the created_at tail; got {idx['column_names']}"
    finally:
        engine.dispose()
        clear_engine_cache()
