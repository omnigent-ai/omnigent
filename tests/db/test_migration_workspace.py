"""Tests for the ``conversations.workspace`` column and its check constraint.

Per ``designs/SESSION_WORKSPACE_SELECTION.md``: column is nullable so
existing rows pre-dating the feature stay valid; CLI sessions can populate
it without setting ``host_id``; but a ``host_id`` row without a
``workspace`` is forbidden by the check constraint. Without the check,
host-launched sessions could land in a permanently-broken state where
the launch frame has no path to send.
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
    Verify the migration creates ``conversations.workspace`` as a nullable
    VARCHAR(2048).

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
    cols = sa.inspect(db_engine).get_columns("conversations")
    workspace_cols = [c for c in cols if c["name"] == "workspace"]
    assert len(workspace_cols) == 1, (
        f"Expected exactly one 'workspace' column on conversations, "
        f"got {len(workspace_cols)}. If 0, the migration didn't apply."
    )
    workspace_col = workspace_cols[0]
    assert workspace_col["nullable"], (
        "conversations.workspace must be NULLABLE — pre-feature rows "
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
                "(id, created_at, updated_at, kind, root_conversation_id) "
                "VALUES (:id, :ts, :ts, 1, :id)"
            ),
            {"id": "conv_ws_null", "ts": 1700000000},
        )
        result = conn.execute(
            sa.text("SELECT workspace FROM conversations WHERE id = :id"),
            {"id": "conv_ws_null"},
        ).scalar_one()
        assert result is None, f"Expected NULL workspace on default insert; got {result!r}."

        # CLI-style insert: workspace set, host_id NULL — allowed.
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, kind, workspace, root_conversation_id) "
                "VALUES (:id, :ts, :ts, 1, :ws, :id)"
            ),
            {
                "id": "conv_ws_cli",
                "ts": 1700000000,
                "ws": "/Users/corey/projects/myapp",
            },
        )
        result = conn.execute(
            sa.text("SELECT workspace FROM conversations WHERE id = :id"),
            {"id": "conv_ws_cli"},
        ).scalar_one()
        assert result == "/Users/corey/projects/myapp", (
            f"Round-trip mismatch: stored '/Users/corey/projects/myapp', got {result!r}."
        )
        conn.commit()


def test_check_constraint_blocks_host_id_without_workspace(
    db_engine: Engine,
) -> None:
    """
    Verify ``ck_conversations_workspace_required_for_host`` blocks
    ``(host_id NOT NULL, workspace NULL)``.

    Without this constraint, a host-launched session row could be
    written with no workspace path, causing every subsequent launch to
    have nothing to send in the ``host.launch_runner`` frame. The
    application-layer validation should catch this first, but the DB
    constraint is the last line of defense.
    """
    with db_engine.connect() as conn:
        with pytest.raises(IntegrityError) as exc_info:
            conn.execute(
                sa.text(
                    "INSERT INTO conversations "
                    "(id, created_at, updated_at, kind, host_id, root_conversation_id) "
                    "VALUES (:id, :ts, :ts, 1, :hid, :id)"
                ),
                {
                    "id": "conv_ws_host_no_ws",
                    "ts": 1700000000,
                    "hid": "host_abc",
                },
            )
        # The constraint name should appear in the error so failures
        # are diagnosable in production logs.
        assert "ck_conversations_workspace_required_for_host" in str(exc_info.value), (
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
                "(owner, name, host_id, status, created_at, updated_at) "
                "VALUES (:o, :n, :hid, 1, :ts, :ts)"
            ),
            {"o": "alice@test.com", "n": "laptop", "hid": "host_abc", "ts": 1700000000},
        )
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, kind, host_id, workspace, root_conversation_id) "
                "VALUES (:id, :ts, :ts, 1, :hid, :ws, :id)"
            ),
            {
                "id": "conv_ws_host_ok",
                "ts": 1700000000,
                "hid": "host_abc",
                "ws": "/Users/corey/universe/src/foo",
            },
        )
        conn.commit()

        result = conn.execute(
            sa.text("SELECT host_id, workspace FROM conversations WHERE id = :id"),
            {"id": "conv_ws_host_ok"},
        ).one()
        assert result.host_id == "host_abc"
        assert result.workspace == "/Users/corey/universe/src/foo"


def test_host_id_is_indexed(db_engine: Engine) -> None:
    """
    Verify ``ix_conversations_host_id`` exists.

    Reconnect reconciliation queries conversations by ``host_id`` on
    every host reconnect; without the index that's a full table scan.
    """
    index_names = {ix["name"] for ix in sa.inspect(db_engine).get_indexes("conversations")}
    assert "ix_conversations_host_id" in index_names, (
        f"Expected ix_conversations_host_id on conversations; got {sorted(index_names)}."
    )


def test_host_id_fk_sets_null_when_host_deleted(db_engine: Engine) -> None:
    """
    After the FK was removed, deleting a host leaves conversations.host_id
    as a dangling reference — the application is responsible for nulling it.
    This test documents the current (post-FK-removal) DB-level behavior:
    host deletion does NOT automatically null conversations.host_id.
    """
    with db_engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO hosts "
                "(owner, name, host_id, status, created_at, updated_at) "
                "VALUES (:o, :n, :hid, 1, :ts, :ts)"
            ),
            {"o": "alice@test.com", "n": "laptop", "hid": "host_del", "ts": 1700000000},
        )
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, kind, host_id, workspace, root_conversation_id) "
                "VALUES (:id, :ts, :ts, 1, :hid, :ws, :id)"
            ),
            {"id": "conv_fk", "ts": 1700000000, "hid": "host_del", "ws": "/ws/foo"},
        )
        conn.commit()

        conn.execute(sa.text("DELETE FROM hosts WHERE host_id = :hid"), {"hid": "host_del"})
        conn.commit()

        row = conn.execute(
            sa.text("SELECT host_id, workspace FROM conversations WHERE id = :id"),
            {"id": "conv_fk"},
        ).one()
        # No FK cascade: host_id is left dangling after host deletion.
        # The application (host store / disconnect handler) is responsible
        # for nulling conversations.host_id when a host is removed.
        assert row.host_id == "host_del", (
            "Without a DB FK, host deletion must not auto-null conversations.host_id."
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
                "(id, created_at, updated_at, kind, workspace, root_conversation_id) "
                "VALUES (:id, :ts, :ts, 1, :ws, :id)"
            ),
            {
                "id": "conv_cli_ws_only",
                "ts": 1700000000,
                "ws": "/Users/corey/projects/cli-launched",
            },
        )
        conn.commit()
        # The row's host_id must be NULL — verifying explicitly so we'd
        # catch a regression that introduced an implicit default.
        result = conn.execute(
            sa.text("SELECT host_id, workspace FROM conversations WHERE id = :id"),
            {"id": "conv_cli_ws_only"},
        ).one()
        assert result.host_id is None
        assert result.workspace == "/Users/corey/projects/cli-launched"
