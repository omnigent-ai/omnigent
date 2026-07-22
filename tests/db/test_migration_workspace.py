"""Tests for the ``omnigent_conversation_metadata.workspace`` column and its check constraint.

Per ``designs/SESSION_WORKSPACE_SELECTION.md``: column is nullable so
existing rows pre-dating the feature stay valid; CLI sessions can populate
it without setting ``host_id``; but a ``host_id`` row without a
``workspace`` is forbidden by the check constraint. Without the check,
host-launched sessions could land in a permanently-broken state where
the launch frame has no path to send.

These columns now live on ``omnigent_conversation_metadata`` rather than
``conversations`` following the schema split migration (aa1b2c3d4e5f).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from omnigent.db.utils import clear_engine_cache, get_or_create_engine


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """
    Fresh SQLite DB with full alembic chain applied; cleaned up after.

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


def test_workspace_column_present_and_nullable(db_engine: Engine) -> None:
    """
    Verify the migration creates ``omnigent_conversation_metadata.workspace``
    as a nullable VARCHAR(2048).

    Three properties matter:
    1. The column exists (proves the migration applied).
    2. It's nullable (so existing rows and CLI sessions without a host
       binding pre-date or skip the feature).
    3. The type is a long enough VARCHAR to hold realistic absolute
       paths.

    A failure on (1) means the migration didn't include the column —
    every code path that mentions ``workspace`` will then crash on
    ``AttributeError`` from the ORM mapping. (2) failing means we'd
    reject every legacy row at first read. (3) failing would silently
    truncate workspace paths, leaving the runner unable to find them.
    """
    cols = sa.inspect(db_engine).get_columns("omnigent_conversation_metadata")
    workspace_cols = [c for c in cols if c["name"] == "workspace"]
    assert len(workspace_cols) == 1, (
        f"Expected exactly one 'workspace' column on omnigent_conversation_metadata, "
        f"got {len(workspace_cols)}. If 0, the migration didn't apply."
    )
    workspace_col = workspace_cols[0]
    assert workspace_col["nullable"], (
        "omnigent_conversation_metadata.workspace must be NULLABLE — pre-feature rows "
        "have no workspace and would otherwise be rejected on read."
    )
    assert "VARCHAR" in str(workspace_col["type"]).upper(), (
        f"Expected VARCHAR-style type, got {workspace_col['type']}. "
        f"A non-VARCHAR type may not handle long path strings."
    )


def test_workspace_round_trip_null_and_value(db_engine: Engine) -> None:
    """
    Round-trip insert with NULL and with a real path string.

    Exercises the schema directly (no ORM) so we'd catch column drift
    independently of any wrapper. NULL → NULL, "/foo/bar" → "/foo/bar".
    """
    with db_engine.connect() as conn:
        # NULL workspace, no host_id — allowed by the check constraint.
        # root_conversation_id is NOT NULL (self-FK to conversations.id);
        # a top-level row's root is its own id, so we bind :id for both.
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, root_conversation_id) "
                "VALUES (:id, :ts, :ts, :id)"
            ),
            {"id": "5221a3ae03d21ff063a9255347dde591", "ts": 1700000000},
        )
        conn.execute(
            sa.text("INSERT INTO omnigent_conversation_metadata (id, kind) VALUES (:id, 1)"),
            {"id": "5221a3ae03d21ff063a9255347dde591"},
        )
        result = conn.execute(
            sa.text("SELECT workspace FROM omnigent_conversation_metadata WHERE id = :id"),
            {"id": "5221a3ae03d21ff063a9255347dde591"},
        ).scalar_one()
        assert result is None, f"Expected NULL workspace on default insert; got {result!r}."

        # CLI-style insert: workspace set, host_id NULL — allowed.
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, root_conversation_id) "
                "VALUES (:id, :ts, :ts, :id)"
            ),
            {"id": "36d62d2d69c0f58390b4d2c17633053e", "ts": 1700000000},
        )
        conn.execute(
            sa.text(
                "INSERT INTO omnigent_conversation_metadata "
                "(id, kind, workspace) "
                "VALUES (:id, 1, :ws)"
            ),
            {
                "id": "36d62d2d69c0f58390b4d2c17633053e",
                "ws": "/Users/corey/projects/myapp",
            },
        )
        result = conn.execute(
            sa.text("SELECT workspace FROM omnigent_conversation_metadata WHERE id = :id"),
            {"id": "36d62d2d69c0f58390b4d2c17633053e"},
        ).scalar_one()
        assert result == "/Users/corey/projects/myapp", (
            f"Round-trip mismatch: stored '/Users/corey/projects/myapp', got {result!r}."
        )
        conn.commit()


def test_check_constraint_blocks_host_id_without_workspace(
    db_engine: Engine,
) -> None:
    """
    Verify ``ck_conversation_metadata_workspace_required_for_host`` blocks
    ``(host_id NOT NULL, workspace NULL)`` on omnigent_conversation_metadata.

    Without this constraint, a host-launched session row could be
    written with no workspace path, causing every subsequent launch to
    have nothing to send in the ``host.launch_runner`` frame. The
    application-layer validation should catch this first, but the DB
    constraint is the last line of defense.
    """
    with db_engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, root_conversation_id) "
                "VALUES (:id, :ts, :ts, :id)"
            ),
            {"id": "565b00780d56b2fdaeb275f4013906dc", "ts": 1700000000},
        )
        with pytest.raises(IntegrityError) as exc_info:
            conn.execute(
                sa.text(
                    "INSERT INTO omnigent_conversation_metadata "
                    "(id, kind, host_id) "
                    "VALUES (:id, 1, :hid)"
                ),
                {
                    "id": "565b00780d56b2fdaeb275f4013906dc",
                    "hid": "abb32306b80732bdfa6153b2f5f6eb92",
                },
            )
        # The constraint name should appear in the error so failures
        # are diagnosable in production logs.
        assert "ck_conversation_metadata_workspace_required_for_host" in str(exc_info.value), (
            f"Expected check-constraint name in IntegrityError; "
            f"got {exc_info.value!r}. Without this name we'd have to "
            f"guess which constraint fired."
        )


def test_check_constraint_allows_host_id_with_workspace(
    db_engine: Engine,
) -> None:
    """
    Verify the canonical valid host-launched insert is accepted.

    Pairs with the previous test: the constraint must reject the
    invalid case and accept the valid one. If both are rejected, the
    constraint expression is wrong and would block all host launches.
    """
    with db_engine.connect() as conn:
        # Insert a host first (no FK enforced, but needed for the join in
        # online_host_ids queries).
        conn.execute(
            sa.text(
                "INSERT INTO hosts "
                "(user_id, name, host_id, status, created_at, updated_at) "
                "VALUES (:o, :n, :hid, 1, :ts, :ts)"
            ),
            {
                "o": "alice@test.com",
                "n": "laptop",
                "hid": "abb32306b80732bdfa6153b2f5f6eb92",
                "ts": 1700000000,
            },
        )
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, root_conversation_id) "
                "VALUES (:id, :ts, :ts, :id)"
            ),
            {"id": "8ebb129f4017d3388ddf25bd4d2d731c", "ts": 1700000000},
        )
        conn.execute(
            sa.text(
                "INSERT INTO omnigent_conversation_metadata "
                "(id, kind, host_id, workspace) "
                "VALUES (:id, 1, :hid, :ws)"
            ),
            {
                "id": "8ebb129f4017d3388ddf25bd4d2d731c",
                "hid": "abb32306b80732bdfa6153b2f5f6eb92",
                "ws": "/Users/corey/universe/src/foo",
            },
        )
        conn.commit()

        result = conn.execute(
            sa.text(
                "SELECT host_id, workspace FROM omnigent_conversation_metadata WHERE id = :id"
            ),
            {"id": "8ebb129f4017d3388ddf25bd4d2d731c"},
        ).one()
        assert result.host_id == "abb32306b80732bdfa6153b2f5f6eb92"
        assert result.workspace == "/Users/corey/universe/src/foo"


def test_host_id_index_dropped(db_engine: Engine) -> None:
    """
    Verify ``ix_conversations_host_id`` no longer exists at head.

    The index served only ``list_conversations_by_host_id``, which had no
    callers and was removed along with the index (migration
    ``z1a2b3c4d5e6``). This locks in the removal so the write-only index
    isn't accidentally reintroduced.
    """
    index_names = {ix["name"] for ix in sa.inspect(db_engine).get_indexes("conversations")}
    assert "ix_conversations_host_id" not in index_names, (
        f"ix_conversations_host_id should have been dropped; got {sorted(index_names)}."
    )


def test_runner_id_is_indexed(db_engine: Engine) -> None:
    """
    Verify ``ix_conversation_metadata_runner_id`` exists on
    omnigent_conversation_metadata at head.

    Reconnect/relaunch reconciliation queries conversations by
    ``runner_id`` on every runner reconnect; without the index that's a
    full table scan.
    """
    index_names = {
        ix["name"] for ix in sa.inspect(db_engine).get_indexes("omnigent_conversation_metadata")
    }
    assert "ix_conversation_metadata_runner_id" in index_names, (
        f"Expected ix_conversation_metadata_runner_id on omnigent_conversation_metadata; "
        f"got {sorted(n for n in index_names if n is not None)}."
    )


def test_host_id_fk_sets_null_when_host_deleted(db_engine: Engine) -> None:
    """
    After the FK was removed, deleting a host leaves metadata.host_id
    as a dangling reference — the application is responsible for nulling it.
    This test documents the current (post-FK-removal) DB-level behavior:
    host deletion does NOT automatically null host_id.
    """
    with db_engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO hosts "
                "(user_id, name, host_id, status, created_at, updated_at) "
                "VALUES (:o, :n, :hid, 1, :ts, :ts)"
            ),
            {
                "o": "alice@test.com",
                "n": "laptop",
                "hid": "3b3efe73c63e0259558065798f210e5d",
                "ts": 1700000000,
            },
        )
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, root_conversation_id) "
                "VALUES (:id, :ts, :ts, :id)"
            ),
            {"id": "d6d79856e6b3f398cfd001d84952622e", "ts": 1700000000},
        )
        conn.execute(
            sa.text(
                "INSERT INTO omnigent_conversation_metadata "
                "(id, kind, host_id, workspace) "
                "VALUES (:id, 1, :hid, :ws)"
            ),
            {
                "id": "d6d79856e6b3f398cfd001d84952622e",
                "hid": "3b3efe73c63e0259558065798f210e5d",
                "ws": "/ws/foo",
            },
        )
        conn.commit()

        conn.execute(
            sa.text("DELETE FROM hosts WHERE host_id = :hid"),
            {"hid": "3b3efe73c63e0259558065798f210e5d"},
        )
        conn.commit()

        row = conn.execute(
            sa.text(
                "SELECT host_id, workspace FROM omnigent_conversation_metadata WHERE id = :id"
            ),
            {"id": "d6d79856e6b3f398cfd001d84952622e"},
        ).one()
        # No FK cascade: host_id is left dangling after host deletion.
        # The application (host store / disconnect handler) is responsible
        # for nulling host_id when a host is removed.
        assert row.host_id == "3b3efe73c63e0259558065798f210e5d", (
            "Without a DB FK, host deletion must not auto-null host_id."
        )
        assert row.workspace == "/ws/foo", "workspace must be untouched."


def test_check_constraint_allows_cli_session_workspace_no_host(
    db_engine: Engine,
) -> None:
    """
    Verify CLI sessions can set ``workspace`` without ``host_id``.

    The constraint is one-way: ``host_id`` requires ``workspace``, but
    ``workspace`` does NOT require ``host_id``. CLI-launched sessions
    record ``os.getcwd()`` as their workspace for display while leaving
    ``host_id`` NULL. If the constraint were symmetric, CLI session
    creation would crash.
    """
    with db_engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, root_conversation_id) "
                "VALUES (:id, :ts, :ts, :id)"
            ),
            {"id": "77049ba7474822eaa4e24c45c2c24999", "ts": 1700000000},
        )
        conn.execute(
            sa.text(
                "INSERT INTO omnigent_conversation_metadata "
                "(id, kind, workspace) "
                "VALUES (:id, 1, :ws)"
            ),
            {
                "id": "77049ba7474822eaa4e24c45c2c24999",
                "ws": "/Users/corey/projects/cli-launched",
            },
        )
        conn.commit()
        # The row's host_id must be NULL — verifying explicitly so we'd
        # catch a regression that introduced an implicit default.
        result = conn.execute(
            sa.text(
                "SELECT host_id, workspace FROM omnigent_conversation_metadata WHERE id = :id"
            ),
            {"id": "77049ba7474822eaa4e24c45c2c24999"},
        ).one()
        assert result.host_id is None
        assert result.workspace == "/Users/corey/projects/cli-launched"


def test_compressed_columns_are_binary_at_head(db_engine: Engine) -> None:
    """
    Verify the opaque text columns are binary (``BLOB``/``BYTEA``) at head.

    These columns are stored zstd-compressed by ``omnigent.db.compression``;
    the compression codec writes raw bytes, so a regression that left any of
    them as ``TEXT`` would corrupt values on a NUL-rejecting backend
    (PostgreSQL) the moment a compressed payload contained a NUL byte.

    After the schema split (aa1b2c3d4e5f), the session-level binary columns
    live on ``omnigent_conversation_metadata``.
    """
    inspector = sa.inspect(db_engine)
    expected = {
        "omnigent_conversation_metadata": [
            "session_usage",
            "session_state",
            "terminal_launch_args",
        ],
        "comments": ["body", "anchor_content"],
        "agents": ["description"],
    }
    for table, columns in expected.items():
        types = {c["name"]: c["type"] for c in inspector.get_columns(table)}
        for column in columns:
            assert isinstance(types[column], sa.LargeBinary), (
                f"{table}.{column} should be binary at head, got {types[column]!r}."
            )
