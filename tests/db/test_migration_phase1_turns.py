"""Tests for the Phase 1 server-authoritative ``turns`` schema.

Covers the migration ``n1a2b3c4d5e6_phase1_server_authoritative_turns``
(issue #466, ``designs/PHASE1_SERVER_AUTHORITATIVE_TURNS.md``):

1. The ``turns`` and ``idempotency_keys`` tables exist with the
   expected columns after the full chain applies.
2. ``conversations.default_send_intent`` is added as a nullable column.
3. The ``ux_turns_one_active_per_conversation`` partial-unique index
   enforces queue-depth-1: at most one *non-terminal* turn per
   conversation, while terminal turns are excluded from the constraint.
   This invariant is what makes "agent is already processing" a database
   fact (the racing dispatch hits IntegrityError -> 409 Attach-Required),
   so it is the single most important behavior to lock down.
4. The status / error_code CHECK constraints reject out-of-taxonomy
   values.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from omnigent.db.utils import clear_engine_cache, get_or_create_engine, now_epoch


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """Fresh SQLite DB with the full alembic chain applied; cleaned up after.

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


def _seed_conversation(conn: sa.Connection, conversation_id: str = "conv_test") -> None:
    """Insert a minimal ``conversations`` row to satisfy the turns FK.

    ``get_or_create_engine`` enables SQLite foreign-key enforcement, so a
    ``turns`` row needs a real parent conversation. Only the NOT NULL
    columns without a default need values.
    """
    now = now_epoch()
    conn.execute(
        sa.text(
            "INSERT INTO conversations (id, created_at, updated_at, "
            "root_conversation_id) VALUES (:id, :now, :now, :id)"
        ),
        {"id": conversation_id, "now": now},
    )


def _insert_turn(
    conn: sa.Connection,
    *,
    turn_id: str,
    conversation_id: str = "conv_test",
    status: str = "RUNNING",
    error_code: str | None = None,
) -> None:
    """Insert a minimal ``turns`` row.

    Assumes the parent conversation already exists (see
    :func:`_seed_conversation`).
    """
    conn.execute(
        sa.text(
            "INSERT INTO turns "
            "(id, conversation_id, status, error_code, vendor, intent, "
            " input_json, lease_epoch, attached, created_at) "
            "VALUES (:id, :cid, :status, :ec, 'claude_code', 'enqueue', "
            " '{}', 0, 0, :now)"
        ),
        {
            "id": turn_id,
            "cid": conversation_id,
            "status": status,
            "ec": error_code,
            "now": now_epoch(),
        },
    )


def test_turns_and_idempotency_tables_present(db_engine: Engine) -> None:
    """Both new tables exist with their expected columns after migration.

    A failure here means the migration didn't apply — every ORM path
    that touches ``SqlTurn`` / ``SqlIdempotencyKey`` would crash.
    """
    insp = sa.inspect(db_engine)
    tables = set(insp.get_table_names())
    assert "turns" in tables, "migration must create the 'turns' table"
    assert "idempotency_keys" in tables, "migration must create the 'idempotency_keys' table"

    turn_cols = {c["name"] for c in insp.get_columns("turns")}
    expected = {
        "id",
        "conversation_id",
        "status",
        "error_code",
        "error_message",
        "vendor",
        "intent",
        "input_json",
        "lease_owner",
        "lease_epoch",
        "last_heartbeat_at",
        "lease_expires_at",
        "attached",
        "last_client_seen",
        "created_at",
        "start_ts",
        "end_ts",
        "checkpoint_id",
    }
    missing = expected - turn_cols
    assert not missing, f"turns table missing columns: {sorted(missing)}"

    idem_cols = {c["name"] for c in insp.get_columns("idempotency_keys")}
    assert idem_cols >= {
        "key",
        "conversation_id",
        "turn_id",
        "request_fingerprint",
        "created_at",
    }, f"idempotency_keys missing columns: {idem_cols}"


def test_default_send_intent_column_added_nullable(db_engine: Engine) -> None:
    """``conversations.default_send_intent`` exists and is nullable.

    Nullable so pre-feature rows stay valid and resolve from the system
    policy default rather than being rejected on read.
    """
    cols = sa.inspect(db_engine).get_columns("conversations")
    match = [c for c in cols if c["name"] == "default_send_intent"]
    assert len(match) == 1, (
        "expected exactly one conversations.default_send_intent column; "
        "if 0, the migration didn't add it"
    )
    assert match[0]["nullable"], (
        "default_send_intent must be NULLABLE (NULL => resolve from policy)"
    )


def test_one_active_turn_per_conversation_enforced(db_engine: Engine) -> None:
    """The partial-unique index allows only one non-terminal turn per session.

    This is the load-bearing invariant: a second active dispatch must be
    rejected at the DB layer so the API can convert the IntegrityError to
    a structured 409 Attach-Required instead of racing two live turns.
    """
    with db_engine.begin() as conn:
        _seed_conversation(conn)
        _insert_turn(conn, turn_id="resp_a", status="RUNNING")

    with db_engine.begin() as conn:
        with pytest.raises(IntegrityError):
            _insert_turn(conn, turn_id="resp_b", status="QUEUED")


def test_terminal_turns_excluded_from_active_uniqueness(db_engine: Engine) -> None:
    """A completed turn frees the slot, and terminal turns may coexist.

    The partial predicate excludes terminal statuses, so finishing one
    turn lets the next one start, and multiple terminal turns on the same
    conversation never collide.
    """
    with db_engine.begin() as conn:
        _seed_conversation(conn)
        _insert_turn(conn, turn_id="resp_a", status="RUNNING")
        conn.execute(sa.text("UPDATE turns SET status='COMPLETED' WHERE id='resp_a'"))
        # New active turn is now allowed.
        _insert_turn(conn, turn_id="resp_b", status="RUNNING")
        # A second terminal turn coexists with the active one.
        _insert_turn(conn, turn_id="resp_c", status="FAILED", error_code="RUNNER_LOST")

    with db_engine.connect() as conn:
        count = conn.execute(sa.text("SELECT COUNT(*) FROM turns")).scalar_one()
    assert count == 3, "expected all three turns to persist"


def test_status_check_constraint_rejects_unknown_value(db_engine: Engine) -> None:
    """An out-of-enum ``status`` is rejected by the CHECK constraint."""
    with db_engine.begin() as conn:
        _seed_conversation(conn)
        with pytest.raises(IntegrityError):
            _insert_turn(conn, turn_id="resp_x", status="BOGUS")


def test_error_code_check_constraint_rejects_unknown_value(db_engine: Engine) -> None:
    """An out-of-taxonomy ``error_code`` is rejected by the CHECK constraint.

    Keeping the failure taxonomy closed at the DB layer prevents a typo'd
    code from silently bypassing the WORKER_*-only routing filter.
    """
    with db_engine.begin() as conn:
        _seed_conversation(conn)
        with pytest.raises(IntegrityError):
            _insert_turn(conn, turn_id="resp_y", status="FAILED", error_code="NOT_A_CODE")
