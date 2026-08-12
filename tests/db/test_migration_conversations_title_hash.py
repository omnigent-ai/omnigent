"""Tests for the ``conversations.title_hash`` column and its migration.

Per-parent child-title uniqueness used to key a UNIQUE index on the wide
``title`` column (a 512-char prefix on MySQL). Migration ``a2b7c3d8e4f9`` adds a
``title_hash`` column holding ``sha256(title)[:16]`` and repoints the index at
it, so entries are a fixed 16 bytes. Uniqueness semantics are unchanged: two
titles collide iff their digests do. These tests assert the post-migration
shape, the backfill, and that the downgrade restores the title-keyed index.

These pin to ``a2b7c3d8e4f9`` explicitly, not head: a later migration
(``72e6dceae14f``) drops the column and index once uniqueness moved into
application code, so the column is absent at head. Head-state coverage lives in
``test_migration_drop_conversations_title_hash.py``.
"""

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
)

_NEW_REVISION = "a2b7c3d8e4f9"
# Revision just before title_hash was introduced (its down_revision).
_PRE_REVISION = "f4a1c8b2d3e6"
_INDEX = "ix_conversations_parent_title_unique"


def _title_hash(title: str) -> bytes:
    return hashlib.sha256(title.encode("utf-8")).digest()[:16]


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """Fresh SQLite DB migrated to ``a2b7c3d8e4f9`` (where title_hash exists)."""
    uri = f"sqlite:///{tmp_path / 'test.db'}"
    cfg = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, _NEW_REVISION)
    try:
        yield engine
    finally:
        engine.dispose()
        clear_engine_cache()


def test_title_hash_column_present_and_nullable(db_engine: Engine) -> None:
    """
    The migration adds ``conversations.title_hash`` as a nullable binary column.

    A failure on presence means the migration didn't apply. It is nullable by
    design so raw-SQL inserts that bypass the ORM default (tests/tooling) still
    succeed; the app always populates it via the ORM default.
    """
    cols = {c["name"]: c for c in sa.inspect(db_engine).get_columns("conversations")}
    assert "title_hash" in cols, "title_hash column missing; migration didn't apply."
    assert cols["title_hash"]["nullable"], "title_hash should be nullable (raw inserts)."


def test_parent_title_index_keys_on_hash(db_engine: Engine) -> None:
    """
    The per-parent unique index keys on ``title_hash``, not the wide ``title``.

    Keeping the index NAME stable matters: the store maps the resulting
    IntegrityError back to a name-already-exists error by matching this name.
    """
    idx = {i["name"]: i for i in sa.inspect(db_engine).get_indexes("conversations")}
    assert _INDEX in idx, f"{_INDEX} missing after upgrade."
    assert idx[_INDEX]["unique"], f"{_INDEX} must stay UNIQUE."
    assert idx[_INDEX]["column_names"] == [
        "workspace_id",
        "parent_conversation_id",
        "title_hash",
    ], f"unexpected columns: {idx[_INDEX]['column_names']}"


def test_backfill_computes_sha256_16_of_title(tmp_path: Path) -> None:
    """
    The migration back-fills ``title_hash = sha256(title)[:16]`` for existing rows.

    Seed a row at the pre-migration revision (no title_hash column), apply the
    migration, and assert the stored digest matches the Python computation.
    """
    uri = f"sqlite:///{tmp_path / 'backfill.db'}"
    cfg = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    try:
        conv_id = "94c349190e241f85a984b3df8f129696"
        title = "claude:legacy session title"
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, _PRE_REVISION)
            # Top-level row (NULL parent) is exempt from the unique index but is
            # still back-filled; root_conversation_id is a NOT NULL self-FK.
            conn.execute(
                sa.text(
                    "INSERT INTO conversations "
                    "(id, created_at, updated_at, root_conversation_id, title) "
                    "VALUES (:id, 1, 1, :id, :title)"
                ),
                {"id": conv_id, "title": title},
            )
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, _NEW_REVISION)
        with engine.connect() as conn:
            stored = conn.execute(
                sa.text("SELECT title_hash FROM conversations WHERE id = :id"),
                {"id": conv_id},
            ).scalar_one()
        assert bytes(stored) == _title_hash(title), "backfilled hash != sha256(title)[:16]"
    finally:
        engine.dispose()
        clear_engine_cache()


def test_downgrade_restores_title_index_and_drops_hash(tmp_path: Path) -> None:
    """
    Downgrade repoints the unique index back onto ``title`` and drops the column.

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

        insp = sa.inspect(engine)
        cols = {c["name"] for c in insp.get_columns("conversations")}
        assert "title_hash" not in cols, "downgrade must drop title_hash."
        idx = {i["name"]: i for i in insp.get_indexes("conversations")}
        assert idx[_INDEX]["column_names"] == [
            "workspace_id",
            "parent_conversation_id",
            "title",
        ], f"downgrade must re-key the index on title; got {idx[_INDEX]['column_names']}"
    finally:
        engine.dispose()
        clear_engine_cache()
