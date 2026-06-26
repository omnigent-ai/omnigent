"""Tests for the ``jobs`` and ``runs`` tables and their migration.

The Jobs/Workflows feature stores a job's graph as opaque JSON plus a rendered
narrative, and records each execution as a run linked to the agent session it
created. This guards the schema (columns/indexes) and the two ``ondelete``
behaviours the feature depends on:

* deleting a job CASCADE-deletes its runs (a run is meaningless without its job);
* deleting the session a run points at SET NULLs ``runs.session_id`` so run
  history outlives session cleanup.
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
    """Fresh SQLite DB with the full alembic chain applied; cleaned up after."""
    db_path = tmp_path / "test.db"
    engine = get_or_create_engine(f"sqlite:///{db_path}")
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_jobs_and_runs_tables_present(db_engine: Engine) -> None:
    """The migration creates both tables with their expected columns."""
    insp = sa.inspect(db_engine)
    job_cols = {c["name"] for c in insp.get_columns("jobs")}
    assert {"id", "name", "graph", "narrative", "agent_id", "schedule_config"} <= job_cols
    run_cols = {c["name"] for c in insp.get_columns("runs")}
    assert {"id", "job_id", "session_id", "status", "started_at", "completed_at"} <= run_cols


def test_run_indexes_present(db_engine: Engine) -> None:
    """The run lookup indexes (job_id, session_id, status) are created."""
    insp = sa.inspect(db_engine)
    names = {i["name"] for i in insp.get_indexes("runs")}
    assert "ix_runs_job_id_started_at" in names
    assert "ix_runs_session_id" in names
    assert "ix_runs_status" in names


def test_delete_job_cascades_to_runs(db_engine: Engine) -> None:
    """Deleting a job removes its runs (FK ondelete=CASCADE)."""
    with db_engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO jobs (id, created_at, updated_at, name, graph, narrative) "
                "VALUES ('job_c', 1, 1, 'C', '{}', 'n')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO runs (id, job_id, session_id, status, started_at) "
                "VALUES ('run_c', 'job_c', NULL, 'running', 1)"
            )
        )
        conn.commit()
        conn.execute(sa.text("DELETE FROM jobs WHERE id = 'job_c'"))
        conn.commit()
        remaining = conn.execute(
            sa.text("SELECT COUNT(*) FROM runs WHERE id = 'run_c'")
        ).scalar_one()
        assert remaining == 0, "Deleting a job must cascade-delete its runs."


def test_delete_session_nulls_run_session_id(db_engine: Engine) -> None:
    """Deleting a run's session SET NULLs runs.session_id, preserving the run."""
    with db_engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, kind, root_conversation_id) "
                "VALUES ('conv_r', 1, 1, 'default', 'conv_r')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO jobs (id, created_at, updated_at, name, graph, narrative) "
                "VALUES ('job_s', 1, 1, 'S', '{}', 'n')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO runs (id, job_id, session_id, status, started_at) "
                "VALUES ('run_s', 'job_s', 'conv_r', 'running', 1)"
            )
        )
        conn.commit()
        conn.execute(sa.text("DELETE FROM conversations WHERE id = 'conv_r'"))
        conn.commit()
        row = conn.execute(sa.text("SELECT session_id FROM runs WHERE id = 'run_s'")).first()
        assert row is not None, "The run must survive its session's deletion."
        assert row[0] is None, "runs.session_id must be NULLed when the session is deleted."
