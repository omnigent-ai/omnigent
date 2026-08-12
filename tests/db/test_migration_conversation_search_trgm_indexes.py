"""Tests for the conversation-search trigram indexes migration.

Session search (``GET /v1/sessions?search_query=``) matches on
``LOWER(title) LIKE '%q%'`` OR ``LOWER(search_text) LIKE '%q%'``. The leading
wildcard makes both unindexable by a b-tree, so on Postgres the content half
sequential-scans every ``conversation_items`` row. Migration ``d5e9f1a2b3c4``
adds ``pg_trgm`` GIN indexes on those two lowercased expressions so the existing
``LIKE`` becomes index-backed (Postgres only; a no-op on SQLite, which keeps its
FTS5 table and small dev tables).

These tests assert the post-migration shape per dialect and that the chain stays
reversible. The Postgres leg (marked ``databricks``) additionally proves the
planner uses the content index for the real search predicate.
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

_NEW_REVISION = "d5e9f1a2b3c4"
# Revision just before this one (its down_revision).
_PRE_REVISION = "f7a8b9c0d1e2"
_ITEMS_INDEX = "ix_conversation_items_search_text_trgm"
_TITLE_INDEX = "ix_conversations_title_trgm"


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> Iterator[Engine]:
    """Fresh SQLite DB with the full alembic chain applied (at head)."""
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    try:
        yield engine
    finally:
        clear_engine_cache()


def _index_names(engine: Engine, table: str) -> set[str]:
    return {i["name"] for i in sa.inspect(engine).get_indexes(table)}


def test_trgm_indexes_absent_on_sqlite(sqlite_engine: Engine) -> None:
    """
    On SQLite the migration is a no-op: neither trigram index is created.

    ``pg_trgm``/GIN are Postgres-only, and SQLite search rides its FTS5 table,
    so the migration must skip cleanly rather than emit unsupported DDL. A
    failure here means the dialect guard regressed and the chain would break on
    a fresh SQLite database.
    """
    assert _ITEMS_INDEX not in _index_names(sqlite_engine, "conversation_items")
    assert _TITLE_INDEX not in _index_names(sqlite_engine, "conversations")


def test_roundtrip_reversible_on_sqlite(tmp_path: Path) -> None:
    """
    Upgrade → downgrade → upgrade roundtrips on a throwaway SQLite DB.

    The downgrade leg is otherwise uncovered (the engine fixtures only run
    ``upgrade head``), so this proves the migration stays reversible even though
    both directions are no-ops on SQLite.
    """
    uri = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    cfg = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    try:
        for target in (_NEW_REVISION, _PRE_REVISION, _NEW_REVISION):
            with engine.begin() as conn:
                cfg.attributes["connection"] = conn
                command.upgrade(cfg, target) if target == _NEW_REVISION else command.downgrade(
                    cfg, target
                )
        # No trigram indexes on SQLite regardless of direction.
        assert _ITEMS_INDEX not in _index_names(engine, "conversation_items")
    finally:
        engine.dispose()
        clear_engine_cache()


@pytest.mark.databricks
def test_trgm_indexes_present_on_postgres(db_uri: str) -> None:
    """
    On Postgres both trigram GIN indexes exist at head, on the lowercased
    expressions the search query filters on.

    Asserts the migration's contract structurally — index type ``gin`` with
    ``gin_trgm_ops`` over a ``lower(...)`` expression — rather than a query plan,
    which depends on table statistics and would be non-deterministic against the
    empty per-worker test DB. (Whether the planner *chooses* the index at scale
    is a property of Postgres + ``pg_trgm``, not of this DDL.)

    Runs only in the Postgres-backed ``databricks`` lane (the ``db_uri`` fixture
    yields the migrated per-worker Postgres DB there; on SQLite this test is
    deselected). Read-only — it never downgrades the shared worker DB.
    """
    engine = get_or_create_engine(db_uri)
    if engine.dialect.name != "postgresql":
        pytest.skip("trigram indexes are Postgres-only")

    with engine.connect() as conn:
        defs = dict(
            conn.execute(
                sa.text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE indexname IN (:items, :title)"
                ),
                {"items": _ITEMS_INDEX, "title": _TITLE_INDEX},
            ).fetchall()
        )
        invalid = conn.execute(
            sa.text(
                "SELECT c.relname FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname IN (:items, :title) AND NOT i.indisvalid"
            ),
            {"items": _ITEMS_INDEX, "title": _TITLE_INDEX},
        ).fetchall()
    assert set(defs) == {_ITEMS_INDEX, _TITLE_INDEX}, defs
    for definition in defs.values():
        lowered = definition.lower()
        assert "gin" in lowered and "gin_trgm_ops" in lowered and "lower(" in lowered, definition
    # The indexes are built with CREATE INDEX CONCURRENTLY; a failed concurrent
    # build leaves an INVALID (planner-ignored) index that would still pass the
    # structural checks above, so assert both builds actually completed.
    assert not invalid, invalid
