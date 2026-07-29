"""Tests for the ``omnigent_conversation_metadata.terminal_launch_args`` column.

Per ``designs/NATIVE_RUNNER_SERVER_LAUNCH.md``: the column holds a nullable
JSON-encoded list of pass-through CLI args for a native terminal wrapper
(claude / codex). NULL means no native launch args — the common case for
non-native sessions and for rows that pre-date the feature. The column is now
a binary (``BLOB``/``BYTEA``) type storing the value zstd-compressed
(``omnigent.db.compression``). These tests exercise the schema directly (raw
SQL, no ORM) so column drift is caught independently of the store wrapper.

After the schema split (aa1b2c3d4e5f), ``terminal_launch_args`` lives on
``omnigent_conversation_metadata`` rather than ``conversations``.
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
    Verify the migration creates
    ``omnigent_conversation_metadata.terminal_launch_args``
    as a nullable binary column.

    (1) The column must exist — proves the migration applied; without
    it every code path mentioning ``terminal_launch_args`` crashes on
    an ``AttributeError`` from the ORM mapping. (2) It must be nullable
    — non-native and pre-feature rows have no launch args and would
    otherwise be rejected on read. (3) The type must be binary
    (``BLOB``/``BYTEA``): the column is stored zstd-compressed by
    ``omnigent.db.compression``, whose framed bytes can contain NUL and
    would be rejected by a ``TEXT`` column on PostgreSQL.
    """
    cols = sa.inspect(db_engine).get_columns("omnigent_conversation_metadata")
    matches = [c for c in cols if c["name"] == "terminal_launch_args"]
    assert len(matches) == 1, (
        f"Expected exactly one 'terminal_launch_args' column on "
        f"omnigent_conversation_metadata, got {len(matches)}. "
        f"If 0, the migration didn't apply."
    )
    col = matches[0]
    assert col["nullable"], (
        "omnigent_conversation_metadata.terminal_launch_args must be NULLABLE — "
        "non-native and pre-feature rows have no launch args and would otherwise be "
        "rejected on read."
    )
    assert isinstance(col["type"], sa.LargeBinary), (
        f"Expected a binary (BLOB/BYTEA) type for the compressed column, got {col['type']!r}."
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
                "(id, created_at, updated_at, root_conversation_id) "
                "VALUES (:id, :ts, :ts, :id)"
            ),
            {"id": "8c2ab1547c91fcc61ae8226f37d07400", "ts": 1700000000},
        )
        conn.execute(
            sa.text("INSERT INTO omnigent_conversation_metadata (id, kind) VALUES (:id, 1)"),
            {"id": "8c2ab1547c91fcc61ae8226f37d07400"},
        )
        result = conn.execute(
            sa.text(
                "SELECT terminal_launch_args FROM omnigent_conversation_metadata WHERE id = :id"
            ),
            {"id": "8c2ab1547c91fcc61ae8226f37d07400"},
        ).scalar_one()
        assert result is None, (
            f"Expected NULL terminal_launch_args on default insert; got {result!r}."
        )

        # Native-launch insert: a JSON-encoded arg list.
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, root_conversation_id) "
                "VALUES (:id, :ts, :ts, :id)"
            ),
            {"id": "d7d570908f410a452a2d6b912bc6f97a", "ts": 1700000000},
        )
        conn.execute(
            sa.text(
                "INSERT INTO omnigent_conversation_metadata "
                "(id, kind, terminal_launch_args) "
                "VALUES (:id, 1, :tla)"
            ),
            {
                "id": "d7d570908f410a452a2d6b912bc6f97a",
                "tla": '["--dangerously-skip-permissions", "--model", "opus"]',
            },
        )
        result = conn.execute(
            sa.text(
                "SELECT terminal_launch_args FROM omnigent_conversation_metadata WHERE id = :id"
            ),
            {"id": "d7d570908f410a452a2d6b912bc6f97a"},
        ).scalar_one()
        assert result == '["--dangerously-skip-permissions", "--model", "opus"]', (
            f"Round-trip mismatch on terminal_launch_args; got {result!r}."
        )
        conn.commit()
