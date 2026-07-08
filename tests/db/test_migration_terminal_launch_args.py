"""Tests for the ``conversations.terminal_launch_args`` column.

Per ``designs/NATIVE_RUNNER_SERVER_LAUNCH.md``: the column is a nullable
TEXT holding a JSON-encoded list of pass-through CLI args for a native
terminal wrapper (claude / codex). NULL means no native launch args —
the common case for non-native sessions and for rows that pre-date the
feature. These tests exercise the schema directly (raw SQL, no ORM) so
column drift is caught independently of the store wrapper.
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
    Fresh SQLite DB with the full alembic chain applied; cleaned up
    after.

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


def test_terminal_launch_args_column_present_and_nullable(db_engine: Engine) -> None:
    """
    Verify the migration creates ``conversations.terminal_launch_args``
    as a nullable TEXT column.

    (1) The column must exist — proves the migration applied; without
    it every code path mentioning ``terminal_launch_args`` crashes on
    an ``AttributeError`` from the ORM mapping. (2) It must be nullable
    — non-native and pre-feature rows have no launch args and would
    otherwise be rejected on read. (3) The type must be TEXT (not a
    bounded VARCHAR) so an arbitrarily-long JSON arg list isn't
    silently truncated.
    """
    cols = sa.inspect(db_engine).get_columns("conversations")
    matches = [c for c in cols if c["name"] == "terminal_launch_args"]
    assert len(matches) == 1, (
        f"Expected exactly one 'terminal_launch_args' column on "
        f"conversations, got {len(matches)}. If 0, the migration didn't apply."
    )
    col = matches[0]
    assert col["nullable"], (
        "conversations.terminal_launch_args must be NULLABLE — non-native "
        "and pre-feature rows have no launch args and would otherwise be "
        "rejected on read."
    )
    assert "TEXT" in str(col["type"]).upper(), (
        f"Expected a TEXT-style type, got {col['type']}. A bounded VARCHAR "
        f"could truncate a long JSON arg list."
    )


def test_terminal_launch_args_round_trip_null_and_json(db_engine: Engine) -> None:
    """
    Round-trip a default insert (NULL) and a JSON-encoded arg list.

    Exercises the schema with raw SQL (no ORM) so column drift is
    caught independently of the store wrapper. NULL stays NULL; a
    stored JSON string comes back byte-for-byte (the store layer is
    what decodes it to a list — here we pin the raw column behaviour).
    """
    with db_engine.connect() as conn:
        # Default insert: terminal_launch_args omitted → NULL.
        # root_conversation_id is NOT NULL (self-FK); a top-level row's
        # root is its own id, so :id binds both.
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, kind, root_conversation_id) "
                "VALUES (:id, :ts, :ts, 1, :id)"
            ),
            {"id": "conv_tla_null", "ts": 1700000000},
        )
        result = conn.execute(
            sa.text("SELECT terminal_launch_args FROM conversations WHERE id = :id"),
            {"id": "conv_tla_null"},
        ).scalar_one()
        assert result is None, (
            f"Expected NULL terminal_launch_args on default insert; got {result!r}."
        )

        # Native-launch insert: a JSON-encoded arg list.
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, kind, terminal_launch_args, "
                "root_conversation_id) "
                "VALUES (:id, :ts, :ts, 1, :tla, :id)"
            ),
            {
                "id": "conv_tla_value",
                "ts": 1700000000,
                "tla": '["--dangerously-skip-permissions", "--model", "opus"]',
            },
        )
        result = conn.execute(
            sa.text("SELECT terminal_launch_args FROM conversations WHERE id = :id"),
            {"id": "conv_tla_value"},
        ).scalar_one()
        assert result == '["--dangerously-skip-permissions", "--model", "opus"]', (
            f"Round-trip mismatch on terminal_launch_args; got {result!r}."
        )
        conn.commit()
