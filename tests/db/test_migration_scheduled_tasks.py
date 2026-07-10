"""Tests for the scheduled_tasks/scheduled_task_runs migration (z6a2b3c4d5e6).

Verifies the migration creates both tables with the expected shape, that
neither carries a database-level foreign key (schema Rule R032 — the
``agent_id`` / ``conversation_id`` / ``scheduled_task_id`` relationships are
application-owned), that the ``scheduled_tasks`` trigger XOR CHECK and the
``scheduled_task_runs.status`` CHECK are enforced, and that a downgrade drops both
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

from omnigent.db.utils import (
    _build_alembic_config,
    clear_engine_cache,
    get_or_create_engine,
)

_PREVIOUS_HEAD = "z5a2b3c4d5e6"


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


def test_migration_creates_both_tables(db_engine: Engine) -> None:
    """Both ``scheduled_tasks`` and ``scheduled_task_runs`` exist after migrating to head."""
    tables = set(sa.inspect(db_engine).get_table_names())
    assert "scheduled_tasks" in tables
    assert "scheduled_task_runs" in tables


def test_scheduled_tasks_columns(db_engine: Engine) -> None:
    """``scheduled_tasks`` has the full expected column set."""
    cols = {c["name"] for c in sa.inspect(db_engine).get_columns("scheduled_tasks")}
    assert cols == {
        "workspace_id",
        "id",
        "name",
        "prompt",
        "cron_expression",
        "run_at_ms",
        "owner_user_id",
        "agent_id",
        "harness_override",
        "model_override",
        "reasoning_effort",
        "workspace",
        "base_branch",
        "sandbox_target",
        "timezone",
        "state",
        "last_run_at",
        "last_run_conversation_id",
        "metadata",
        "created_at",
        "updated_at",
    }


def test_scheduled_task_runs_columns(db_engine: Engine) -> None:
    """``scheduled_task_runs`` has the full expected column set."""
    cols = {c["name"] for c in sa.inspect(db_engine).get_columns("scheduled_task_runs")}
    assert cols == {
        "workspace_id",
        "id",
        "scheduled_task_id",
        "conversation_id",
        "status",
        "scheduled_at",
        "fired_at",
        "finished_at",
        "error",
    }


def test_workspace_id_leads_primary_keys(db_engine: Engine) -> None:
    """``workspace_id`` is the leading PK member on both tables."""
    insp = sa.inspect(db_engine)
    assert insp.get_pk_constraint("scheduled_tasks")["constrained_columns"] == [
        "workspace_id",
        "id",
    ]
    assert insp.get_pk_constraint("scheduled_task_runs")["constrained_columns"] == [
        "workspace_id",
        "id",
    ]


def test_no_foreign_keys(db_engine: Engine) -> None:
    """Neither table carries a DB-level FK (schema Rule R032).

    ``agent_id`` / ``last_run_conversation_id`` / ``scheduled_task_id`` /
    ``conversation_id`` are plain columns; referential cleanup is
    application-owned. A FK here would violate the repo-wide standard set by
    ``p1a2b3c4d5e6_remove_all_fks``.
    """
    insp = sa.inspect(db_engine)
    assert insp.get_foreign_keys("scheduled_tasks") == []
    assert insp.get_foreign_keys("scheduled_task_runs") == []


def test_expected_indexes(db_engine: Engine) -> None:
    """Both tables expose the indexes that back the read paths."""
    insp = sa.inspect(db_engine)
    scheduled_tasks_idx = {i["name"] for i in insp.get_indexes("scheduled_tasks")}
    assert {
        "ix_scheduled_tasks_created_at",
        "ix_scheduled_tasks_owner_user_id",
        "ix_scheduled_tasks_agent_id",
        "ix_scheduled_tasks_state",
    } <= scheduled_tasks_idx
    runs_idx = {i["name"] for i in insp.get_indexes("scheduled_task_runs")}
    assert "ix_scheduled_task_runs_scheduled_task_id" in runs_idx


def test_state_default_on_omitted_insert(db_engine: Engine) -> None:
    """A raw insert omitting ``state`` / ``workspace_id`` picks up their defaults.

    ``metadata`` is NOT NULL with no server_default (MySQL forbids a TEXT
    default), so it must be supplied explicitly — only the integer server_defaults
    (``state`` / ``workspace_id``) are exercised here.
    """
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_tasks "
                "(id, name, prompt, cron_expression, owner_user_id, agent_id, "
                " timezone, metadata, created_at) "
                "VALUES ('st_def', 'n', 'p', '0 9 * * *', 'u', 'ag_1', 'UTC', '{}', 1)"
            )
        )
        state, workspace_id = conn.execute(
            sa.text("SELECT state, workspace_id FROM scheduled_tasks WHERE id = 'st_def'")
        ).one()
    assert state == 1  # 1 = 'active'
    assert workspace_id == 0


def test_trigger_check_accepts_cron_only(db_engine: Engine) -> None:
    """A row with only ``cron_expression`` set satisfies the XOR CHECK."""
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_tasks "
                "(id, name, prompt, cron_expression, owner_user_id, agent_id, "
                " timezone, metadata, created_at) "
                "VALUES ('st_cron', 'n', 'p', '0 9 * * *', 'u', 'ag', 'UTC', '{}', 1)"
            )
        )


def test_trigger_check_accepts_run_at_only(db_engine: Engine) -> None:
    """A row with only ``run_at_ms`` set satisfies the XOR CHECK."""
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_tasks "
                "(id, name, prompt, run_at_ms, owner_user_id, agent_id, "
                " timezone, metadata, created_at) "
                "VALUES ('st_once', 'n', 'p', 1700000000000, 'u', 'ag', 'UTC', '{}', 1)"
            )
        )


def test_trigger_check_rejects_both_set(db_engine: Engine) -> None:
    """The XOR CHECK rejects a row with both trigger columns set (SQLite)."""
    with pytest.raises(IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO scheduled_tasks "
                    "(id, name, prompt, cron_expression, run_at_ms, owner_user_id, "
                    " agent_id, timezone, created_at) "
                    "VALUES ('st_both', 'n', 'p', '0 9 * * *', 1700000000000, 'u', "
                    " 'ag', 'UTC', 1)"
                )
            )


def test_trigger_check_rejects_neither_set(db_engine: Engine) -> None:
    """The XOR CHECK rejects a row with neither trigger column set (SQLite)."""
    with pytest.raises(IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO scheduled_tasks "
                    "(id, name, prompt, owner_user_id, agent_id, timezone, created_at) "
                    "VALUES ('st_none', 'n', 'p', 'u', 'ag', 'UTC', 1)"
                )
            )


def test_state_stored_as_smallint(db_engine: Engine) -> None:
    """The ``scheduled_tasks.state`` column is an integer type, not a VARCHAR."""
    cols = {c["name"]: c for c in sa.inspect(db_engine).get_columns("scheduled_tasks")}
    assert "INT" in str(cols["state"]["type"]).upper()


def test_state_check_rejects_bad_code(db_engine: Engine) -> None:
    """The ``scheduled_tasks.state`` CHECK rejects codes outside the closed set.

    State is stored as a stable int code (see enum_codecs SCHEDULED_TASK_STATE,
    codes 1-4); a code outside that range must fail the CHECK.
    """
    with pytest.raises(IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO scheduled_tasks "
                    "(id, name, prompt, cron_expression, owner_user_id, agent_id, "
                    " timezone, state, created_at) "
                    "VALUES ('st_badstate', 'n', 'p', '0 9 * * *', 'u', 'ag', 'UTC', 99, 1)"
                )
            )


def test_scheduled_task_runs_status_check_rejects_bad_code(db_engine: Engine) -> None:
    """The ``scheduled_task_runs.status`` CHECK rejects codes outside the closed set.

    Status is stored as a stable int code (see enum_codecs
    SCHEDULED_TASK_RUN_STATUS, codes 1-5); a code outside that range must fail
    the CHECK.
    """
    with pytest.raises(IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO scheduled_task_runs "
                    "(id, scheduled_task_id, status, scheduled_at) "
                    "VALUES ('sr_bad', 'st_1', 99, 1)"
                )
            )


def test_scheduled_task_runs_status_check_accepts_valid_code(db_engine: Engine) -> None:
    """A valid status code inserts cleanly."""
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_task_runs "
                "(id, scheduled_task_id, status, scheduled_at) "
                "VALUES ('sr_ok', 'st_1', 1, 1)"  # 1 = 'scheduled'
            )
        )
        status = conn.execute(
            sa.text("SELECT status FROM scheduled_task_runs WHERE id = 'sr_ok'")
        ).scalar_one()
    assert status == 1


def test_scheduled_task_runs_status_stored_as_smallint(db_engine: Engine) -> None:
    """The ``status`` column is an integer type, not a VARCHAR."""
    cols = {c["name"]: c for c in sa.inspect(db_engine).get_columns("scheduled_task_runs")}
    assert "INT" in str(cols["status"]["type"]).upper()


def test_downgrade_drops_both_tables(tmp_path: Path) -> None:
    """Downgrading one step removes both tables; re-upgrade restores them."""
    db_path = tmp_path / "downgrade.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)

    tables = set(sa.inspect(engine).get_table_names())
    assert {"scheduled_tasks", "scheduled_task_runs"} <= tables

    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, _PREVIOUS_HEAD)

    tables = set(sa.inspect(engine).get_table_names())
    assert "scheduled_tasks" not in tables
    assert "scheduled_task_runs" not in tables

    # Re-upgrade restores both tables — proves the upgrade is replayable.
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "z6a2b3c4d5e6")
    tables = set(sa.inspect(engine).get_table_names())
    assert {"scheduled_tasks", "scheduled_task_runs"} <= tables

    engine.dispose()
    clear_engine_cache()
