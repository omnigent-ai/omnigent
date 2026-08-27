"""Upgrade must not crash when task_summary column pre-exists its Alembic migration.

Revision ``za2b3c4d5e6f`` runs an unconditional ``ALTER TABLE
omnigent_conversation_metadata ADD COLUMN task_summary``.  On a real database
that sits at ``d5e9f1a2b3c4`` (one step before that revision) but already carries
a ``task_summary`` column — e.g. a database that was patched outside of Alembic
during a hotfix — the automatic startup upgrade to head (``e5d9bc8ac650``) aborts
with::

    sqlite3.OperationalError: duplicate column name: task_summary

and the server refuses to boot.  These tests exercise the *real* SQLite migration
and startup path (``_initialize_or_verify_schema`` → ``alembic upgrade head``),
not a mocked schema inspection:

* ``test_upgrade_with_preexisting_task_summary_column`` — the bug: a DB at
  ``d5e9f1a2b3c4`` with ``task_summary`` already present must reach head without
  a duplicate-column failure and without losing existing data.
* ``test_upgrade_adds_missing_task_summary_column`` — negative control: a DB at
  ``d5e9f1a2b3c4`` *without* the column still has it added and reaches head.
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
    _create_engine,
    _get_current_db_revision,
    _initialize_or_verify_schema,
    clear_engine_cache,
)

# Revision one step before ``za2b3c4d5e6f`` (which adds ``task_summary``).
_REVISION_BEFORE_TASK_SUMMARY = "d5e9f1a2b3c4"
# Current head at the time this bug was filed; asserted as the reachable target.
_EXPECTED_HEAD = "e5d9bc8ac650"

_METADATA_TABLE = "omnigent_conversation_metadata"


def _stamp_at_revision(uri: str, revision: str) -> Engine:
    """Build a real SQLite DB migrated exactly up to ``revision``."""
    engine = _create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)
    assert _get_current_db_revision(engine) == revision
    return engine


def _task_summary_present(engine: Engine) -> bool:
    cols = {c["name"] for c in sa.inspect(engine).get_columns(_METADATA_TABLE)}
    return "task_summary" in cols


@pytest.fixture(autouse=True)
def _clean_engine_cache() -> Iterator[None]:
    """Ensure no cached engine leaks between tests."""
    try:
        yield
    finally:
        clear_engine_cache()


def test_upgrade_with_preexisting_task_summary_column(tmp_path: Path) -> None:
    """A DB behind head that already has ``task_summary`` must upgrade cleanly.

    When a database sits at ``d5e9f1a2b3c4`` but already carries the
    ``task_summary`` column (added outside Alembic), the automatic startup
    upgrade to head must not abort with ``duplicate column name: task_summary``.
    After the fix the migration is idempotent and the database reaches head
    with its existing data intact.
    """
    uri = f"sqlite:///{tmp_path / 'chat.db'}"
    engine = _stamp_at_revision(uri, _REVISION_BEFORE_TASK_SUMMARY)

    # The column already exists out of band, while the ledger is still behind.
    with engine.connect() as conn:
        conn.execute(
            sa.text(f"ALTER TABLE {_METADATA_TABLE} ADD COLUMN task_summary VARCHAR(128)")
        )
        conn.commit()
    assert _task_summary_present(engine), "precondition: column present pre-upgrade"

    # Seed a row so we can prove data survives the reconciled migration.
    # ``id`` is the row PK (BLOB); ``kind`` and ``workspace_id`` are NOT NULL.
    row_id = b"\x55\x75" + b"\x00" * 14
    with engine.connect() as conn:
        conn.execute(
            sa.text(
                f"INSERT INTO {_METADATA_TABLE} (id, workspace_id, kind, task_summary) "
                "VALUES (:id, :ws, :kind, :summary)"
            ),
            {"id": row_id, "ws": 1, "kind": 1, "summary": "Investigate auth token refresh"},
        )
        conn.commit()

    # Drive the real server-startup migration path.
    _initialize_or_verify_schema(engine, uri)

    assert _get_current_db_revision(engine) == _EXPECTED_HEAD, (
        "database must reach head after reconciling the preexisting column"
    )
    assert _task_summary_present(engine), "task_summary must remain present after upgrade"

    with engine.connect() as conn:
        surviving = conn.execute(
            sa.text(f"SELECT task_summary FROM {_METADATA_TABLE} WHERE id = :id"),
            {"id": row_id},
        ).scalar_one()
    assert surviving == "Investigate auth token refresh", "existing data must survive"


def test_upgrade_adds_missing_task_summary_column(tmp_path: Path) -> None:
    """Negative control: a genuinely missing ``task_summary`` is still added.

    Guards against a fix that skips the column unconditionally: a DB at
    ``d5e9f1a2b3c4`` without the column must still gain it and reach head.
    """
    uri = f"sqlite:///{tmp_path / 'chat.db'}"
    engine = _stamp_at_revision(uri, _REVISION_BEFORE_TASK_SUMMARY)
    assert not _task_summary_present(engine), "precondition: column absent pre-upgrade"

    _initialize_or_verify_schema(engine, uri)

    assert _get_current_db_revision(engine) == _EXPECTED_HEAD
    assert _task_summary_present(engine), "task_summary must be added when absent"
