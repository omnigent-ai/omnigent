"""Tests for the session-lifecycle-outbox migration (a8b9c0d1e2f3, OMN-104).

Verifies the migration creates all three tables (``session_lifecycle_cursors``,
``session_lifecycle_outbox``, ``session_elicitations``) with the expected
shape, that none carries a database-level foreign key (schema Rule R032 —
``session_id``/``elicitation_id`` relationships are application-owned), that
every ``CHECK`` constraint is enforced, and that a downgrade drops all three
tables cleanly.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine

_PREVIOUS_HEAD = "d5e9f1a2b3c4"
_HEAD = "a8b9c0d1e2f3"

_HEX16 = "00" * 16  # 16 raw bytes = 32 hex chars, a valid Uuid16 literal


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """Fresh SQLite DB with the full migration chain applied; cleaned up after."""
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


# ── session_lifecycle_cursors ─────────────────────────────────────


def test_migration_creates_all_three_tables(db_engine: Engine) -> None:
    tables = set(sa.inspect(db_engine).get_table_names())
    assert "session_lifecycle_cursors" in tables
    assert "session_lifecycle_outbox" in tables
    assert "session_elicitations" in tables


def test_cursors_columns(db_engine: Engine) -> None:
    cols = {c["name"] for c in sa.inspect(db_engine).get_columns("session_lifecycle_cursors")}
    assert cols == {"workspace_id", "session_id", "next_sequence"}


def test_cursors_pk_leads_with_workspace_id(db_engine: Engine) -> None:
    pk = sa.inspect(db_engine).get_pk_constraint("session_lifecycle_cursors")
    assert pk["constrained_columns"] == ["workspace_id", "session_id"]


def test_cursors_no_foreign_keys(db_engine: Engine) -> None:
    assert sa.inspect(db_engine).get_foreign_keys("session_lifecycle_cursors") == []


# ── session_lifecycle_outbox ──────────────────────────────────────


def test_outbox_columns(db_engine: Engine) -> None:
    cols = {c["name"] for c in sa.inspect(db_engine).get_columns("session_lifecycle_outbox")}
    assert cols == {
        "workspace_id",
        "id",
        "session_id",
        "event_type",
        "transition_key",
        "sequence",
        "event_version",
        "payload",
        "status",
        "attempt_count",
        "next_attempt_at",
        "lease_owner",
        "lease_expires_at",
        "last_attempt_at",
        "delivered_at",
        "last_http_status",
        "last_error_code",
        "last_error_message",
        "created_at",
    }


def test_outbox_pk_leads_with_workspace_id(db_engine: Engine) -> None:
    pk = sa.inspect(db_engine).get_pk_constraint("session_lifecycle_outbox")
    assert pk["constrained_columns"] == ["workspace_id", "id"]


def test_outbox_no_foreign_keys(db_engine: Engine) -> None:
    assert sa.inspect(db_engine).get_foreign_keys("session_lifecycle_outbox") == []


def test_outbox_indexes(db_engine: Engine) -> None:
    insp = sa.inspect(db_engine)
    idx_cols = {
        i["name"]: list(i["column_names"]) for i in insp.get_indexes("session_lifecycle_outbox")
    }
    idx_unique = {i["name"]: i.get("unique") for i in insp.get_indexes("session_lifecycle_outbox")}

    assert idx_cols["uq_session_lifecycle_outbox_session_sequence"] == [
        "workspace_id",
        "session_id",
        "sequence",
    ]
    assert idx_unique["uq_session_lifecycle_outbox_session_sequence"]

    assert idx_cols["uq_session_lifecycle_outbox_transition"] == [
        "workspace_id",
        "session_id",
        "event_type",
        "transition_key",
    ]
    assert idx_unique["uq_session_lifecycle_outbox_transition"]

    assert idx_cols["ix_session_lifecycle_outbox_claim"] == [
        "status",
        "next_attempt_at",
        "workspace_id",
    ]
    assert not idx_unique["ix_session_lifecycle_outbox_claim"]

    assert idx_cols["ix_session_lifecycle_outbox_session_order"] == [
        "workspace_id",
        "session_id",
        "sequence",
        "status",
    ]
    assert not idx_unique["ix_session_lifecycle_outbox_session_order"]


def _insert_outbox_row(conn: sa.Connection, *, event_type: int, status: int) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO session_lifecycle_outbox "
            "(id, session_id, event_type, transition_key, sequence, payload, "
            " status, next_attempt_at, created_at) "
            f"VALUES (X'{_HEX16}', X'{_HEX16}', :event_type, 'tk', 1, X'00', "
            " :status, 1, 1)"
        ),
        {"event_type": event_type, "status": status},
    )


def test_outbox_event_type_check_rejects_bad_code(db_engine: Engine) -> None:
    with pytest.raises(IntegrityError):
        with db_engine.begin() as conn:
            _insert_outbox_row(conn, event_type=99, status=1)


def test_outbox_event_type_check_accepts_valid_codes(db_engine: Engine) -> None:
    for code in (1, 2, 3, 4):
        with db_engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM session_lifecycle_outbox"))
            _insert_outbox_row(conn, event_type=code, status=1)


def test_outbox_status_check_rejects_bad_code(db_engine: Engine) -> None:
    with pytest.raises(IntegrityError):
        with db_engine.begin() as conn:
            _insert_outbox_row(conn, event_type=1, status=99)


def test_outbox_status_check_accepts_valid_codes(db_engine: Engine) -> None:
    for code in (1, 2, 3, 4, 5):
        with db_engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM session_lifecycle_outbox"))
            _insert_outbox_row(conn, event_type=1, status=code)


def test_outbox_status_stored_as_smallint(db_engine: Engine) -> None:
    cols = {c["name"]: c for c in sa.inspect(db_engine).get_columns("session_lifecycle_outbox")}
    assert "INT" in str(cols["status"]["type"]).upper()


# ── session_elicitations ──────────────────────────────────────────


def test_elicitations_columns(db_engine: Engine) -> None:
    cols = {c["name"] for c in sa.inspect(db_engine).get_columns("session_elicitations")}
    assert cols == {
        "workspace_id",
        "id",
        "session_id",
        "status",
        "request_payload",
        "decision_payload",
        "decided_by",
        "created_at",
        "decided_at",
        "resolved_at",
    }


def test_elicitations_pk_leads_with_workspace_id(db_engine: Engine) -> None:
    pk = sa.inspect(db_engine).get_pk_constraint("session_elicitations")
    assert pk["constrained_columns"] == ["workspace_id", "id"]


def test_elicitations_no_foreign_keys(db_engine: Engine) -> None:
    assert sa.inspect(db_engine).get_foreign_keys("session_elicitations") == []


def test_elicitations_id_is_string_not_uuid16(db_engine: Engine) -> None:
    """``elicitation_id`` is a prefixed opaque token
    (``f"elicit_{secrets.token_hex(16)}"``), not a bare-hex UUID — the ``id``
    column must be a plain VARCHAR, unlike every other id column in this
    migration (which are 16-raw-byte Uuid16 BLOBs)."""
    cols = {c["name"]: c for c in sa.inspect(db_engine).get_columns("session_elicitations")}
    assert (
        "VARCHAR" in str(cols["id"]["type"]).upper() or "CHAR" in str(cols["id"]["type"]).upper()
    )
    # A real, non-hex elicitation_id must insert cleanly.
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO session_elicitations "
                "(id, session_id, status, request_payload, created_at) "
                "VALUES ('elicit_not_valid_hex_at_all', X'" + _HEX16 + "', 1, X'00', 1)"
            )
        )
    with db_engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT id FROM session_elicitations WHERE id = 'elicit_not_valid_hex_at_all'")
        ).one()
    assert row.id == "elicit_not_valid_hex_at_all"


def test_elicitations_index(db_engine: Engine) -> None:
    insp = sa.inspect(db_engine)
    idx = {i["name"]: list(i["column_names"]) for i in insp.get_indexes("session_elicitations")}
    assert idx["ix_session_elicitations_session_id"] == ["workspace_id", "session_id"]


def _insert_elicitation_row(conn: sa.Connection, *, status: int) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO session_elicitations "
            "(id, session_id, status, request_payload, created_at) "
            "VALUES ('elicit_check_test', X'" + _HEX16 + "', :status, X'00', 1)"
        ),
        {"status": status},
    )


def test_elicitations_status_check_rejects_bad_code(db_engine: Engine) -> None:
    with pytest.raises(IntegrityError):
        with db_engine.begin() as conn:
            _insert_elicitation_row(conn, status=99)


def test_elicitations_status_check_accepts_valid_codes(db_engine: Engine) -> None:
    for code in (1, 2, 3, 4):
        with db_engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM session_elicitations"))
            _insert_elicitation_row(conn, status=code)


# ── downgrade / re-upgrade ─────────────────────────────────────────


def test_downgrade_drops_all_three_tables(tmp_path: Path) -> None:
    """Downgrading one step removes all three tables; re-upgrade restores them."""
    db_path = tmp_path / "downgrade.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)

    tables = set(sa.inspect(engine).get_table_names())
    assert {
        "session_lifecycle_cursors",
        "session_lifecycle_outbox",
        "session_elicitations",
    } <= tables

    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, _PREVIOUS_HEAD)

    tables = set(sa.inspect(engine).get_table_names())
    assert "session_lifecycle_cursors" not in tables
    assert "session_lifecycle_outbox" not in tables
    assert "session_elicitations" not in tables

    # Re-upgrade restores all three — proves the upgrade is replayable.
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, _HEAD)
    tables = set(sa.inspect(engine).get_table_names())
    assert {
        "session_lifecycle_cursors",
        "session_lifecycle_outbox",
        "session_elicitations",
    } <= tables

    engine.dispose()
    clear_engine_cache()
