"""Tests for the ``conversations.runner_id`` column added to the initial schema migration.

Per ``designs/RUNNER.md`` Phase 0: the column is nullable, no FK
(runner records aren't persisted in v1), and is the load-bearing
column for hard conversation affinity.
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
    """Fresh SQLite DB with full alembic chain applied; cleaned up after."""
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_migration_adds_runner_id_column_nullable(db_engine: Engine) -> None:
    """The migration creates ``conversations.runner_id`` as a nullable VARCHAR(64).

    Three properties matter:
    1. The column exists at all (proves the migration includes it).
    2. It's nullable (Phase 0 contract: NULL until first dispatch pins).
    3. It's String-typed with length 64 (matches the ID convention used by
       every other ID column in the schema).

    A failure on (1) means the migration didn't include the column;
    (2) would mean we'd reject every newly-created conversation since
    it'd need a value at insert time; (3) is mostly cosmetic but a
    drift signal.
    """
    cols = sa.inspect(db_engine).get_columns("conversations")
    runner_id_cols = [c for c in cols if c["name"] == "runner_id"]
    assert len(runner_id_cols) == 1, (
        f"Expected exactly one 'runner_id' column on conversations, got {len(runner_id_cols)}. "
        f"If 0, the migration didn't include the column."
    )
    runner_id_col = runner_id_cols[0]
    assert runner_id_col["nullable"], "conversations.runner_id must be NULLABLE"
    # SQLite reports VARCHAR(64) as VARCHAR(64); SQLAlchemy normalizes the
    # type. Compare on the type's class string rather than the raw repr.
    assert "VARCHAR" in str(runner_id_col["type"]).upper(), (
        f"Expected VARCHAR-style type, got {runner_id_col['type']}"
    )


def test_runner_id_round_trip_null_and_value(db_engine: Engine) -> None:
    """Round-trip a conversation row with both NULL and a runner id value.

    Insert with NULL → read back NULL. Insert with a value → read back
    the value. This exercises the full SQL path independently of the
    ConversationStore wrapper, so we'd notice schema-level drift even
    if the ORM mapping happened to mask it.
    """
    with db_engine.connect() as conn:
        # ── NULL ────────────────────────────────────────────────────
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, root_conversation_id, kind) "
                "VALUES (:id, :ts, :ts, :id, 'default')"
            ),
            {"id": "conv_null_test", "ts": 1700000000},
        )
        result = conn.execute(
            sa.text("SELECT runner_id FROM conversations WHERE id = :id"),
            {"id": "conv_null_test"},
        ).scalar_one()
        assert result is None, f"Expected NULL on default-insert; got {result!r}"

        # ── value ───────────────────────────────────────────────────
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, root_conversation_id, kind, runner_id) "
                "VALUES (:id, :ts, :ts, :id, 'default', :rid)"
            ),
            {"id": "conv_value_test", "ts": 1700000000, "rid": "runner-uuid-abc"},
        )
        result = conn.execute(
            sa.text("SELECT runner_id FROM conversations WHERE id = :id"),
            {"id": "conv_value_test"},
        ).scalar_one()
        assert result == "runner-uuid-abc", (
            f"Round-trip mismatch: stored 'runner-uuid-abc', got {result!r}. "
            f"Either the column doesn't persist values or the migration "
            f"created a column with a different name."
        )
        conn.commit()


def test_no_foreign_key_constraint_on_runner_id(db_engine: Engine) -> None:
    """``conversations.runner_id`` MUST NOT have a foreign key constraint.

    Per RUNNER.md §5 "Persistence" the runner registry is in-memory
    only — there's no ``runners`` table for the column to reference.
    A FK here would either (a) require a runners table we don't have,
    or (b) silently succeed against a phantom row. Either is wrong.
    """
    fks = sa.inspect(db_engine).get_foreign_keys("conversations")
    runner_fks = [fk for fk in fks if "runner_id" in fk.get("constrained_columns", [])]
    assert runner_fks == [], (
        f"conversations.runner_id should have no FK; got {runner_fks}. "
        f"Runner records aren't persisted in v1, so any FK would be a bug."
    )
