"""Migration coverage for scheduled-task Project assignment."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.db_models import SqlScheduledTask, Uuid16
from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine

_PREVIOUS_HEAD = "e5d9bc8ac650"
_TASK_ID = "11111111111111111111111111111111"
_RUN_ID = "22222222222222222222222222222222"
_PROJECT_ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_AGENT_ID = "33333333333333333333333333333333"
_HOST_ID = "44444444444444444444444444444444"
_CONVERSATION_ID = "55555555555555555555555555555555"

_EXPECTED_TASK_VALUES = {
    "workspace_id": 7,
    "id": _TASK_ID.upper(),
    "name": "distinct-nightly",
    "prompt": "distinct migration prompt",
    "rrule": "FREQ=HOURLY;BYMINUTE=17",
    "user_id": "migration@example.com",
    "agent_id": _AGENT_ID.upper(),
    "model_override": "migration-model",
    "reasoning_effort": "high",
    "permission_mode": "acceptEdits",
    "max_cost_usd": 17.25,
    "workspace": "/distinct/workspace",
    "base_branch": "release/migration",
    "execution_target": 2,
    "host_id": _HOST_ID.upper(),
    "timezone": "America/Los_Angeles",
    "state": 2,
    "last_run_at": 1_700_000_123,
    "last_run_conversation_id": _CONVERSATION_ID.upper(),
    "created_at": 1_600_000_111,
    "updated_at": 1_600_000_222,
}

_EXPECTED_RUN_VALUES = {
    "workspace_id": 7,
    "id": _RUN_ID.upper(),
    "scheduled_task_id": _TASK_ID.upper(),
    "conversation_id": _CONVERSATION_ID.upper(),
    "status": 4,
    "scheduled_at": 1_700_000_001,
    "fired_at": 1_700_000_002,
    "finished_at": 1_700_000_003,
    "error": "distinct migration error",
    "error_code": "migration_error",
}


def _insert_task_and_run(engine: sa.Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_tasks "
                "(workspace_id, id, name, prompt, rrule, user_id, agent_id, model_override, "
                "reasoning_effort, permission_mode, max_cost_usd, workspace, base_branch, "
                "execution_target, host_id, timezone, state, last_run_at, "
                "last_run_conversation_id, created_at, updated_at) VALUES "
                f"(7, X'{_TASK_ID}', 'distinct-nightly', 'distinct migration prompt', "
                f"'FREQ=HOURLY;BYMINUTE=17', 'migration@example.com', X'{_AGENT_ID}', "
                "'migration-model', 'high', 'acceptEdits', 17.25, '/distinct/workspace', "
                f"'release/migration', 2, X'{_HOST_ID}', 'America/Los_Angeles', 2, "
                f"1700000123, X'{_CONVERSATION_ID}', 1600000111, 1600000222)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_task_runs "
                "(workspace_id, id, scheduled_task_id, conversation_id, status, scheduled_at, "
                "fired_at, finished_at, error, error_code) VALUES "
                f"(7, X'{_RUN_ID}', X'{_TASK_ID}', X'{_CONVERSATION_ID}', 4, "
                "1700000001, 1700000002, 1700000003, 'distinct migration error', "
                "'migration_error')"
            )
        )


def _task_values(engine: sa.Engine) -> dict[str, object]:
    with engine.connect() as conn:
        row = (
            conn.execute(
                sa.text(
                    "SELECT workspace_id, hex(id) AS id, name, prompt, rrule, user_id, "
                    "hex(agent_id) AS agent_id, model_override, reasoning_effort, "
                    "permission_mode, max_cost_usd, workspace, base_branch, execution_target, "
                    "hex(host_id) AS host_id, "
                    "timezone, state, last_run_at, hex(last_run_conversation_id) AS "
                    "last_run_conversation_id, created_at, updated_at FROM scheduled_tasks"
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


def _run_values(engine: sa.Engine) -> dict[str, object]:
    with engine.connect() as conn:
        row = (
            conn.execute(
                sa.text(
                    "SELECT workspace_id, hex(id) AS id, hex(scheduled_task_id) AS "
                    "scheduled_task_id, hex(conversation_id) AS conversation_id, status, "
                    "scheduled_at, fired_at, finished_at, error, error_code "
                    "FROM scheduled_task_runs"
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


def _column_defaults(engine: sa.Engine, table: str) -> dict[str, object]:
    return {column["name"]: column["default"] for column in sa.inspect(engine).get_columns(table)}


def _indexes(engine: sa.Engine, table: str) -> dict[str, tuple[bool, tuple[str, ...]]]:
    return {
        index["name"]: (
            bool(index["unique"]),
            tuple(index["column_names"]),
        )
        for index in sa.inspect(engine).get_indexes(table)
    }


def _assert_distinct_payload_survives(engine: sa.Engine) -> None:
    assert _task_values(engine) == _EXPECTED_TASK_VALUES
    assert _run_values(engine) == _EXPECTED_RUN_VALUES


def test_project_id_column_and_index_at_head(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'head.db'}"
    engine = get_or_create_engine(uri)
    inspector = sa.inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns("scheduled_tasks")}
    project_column = columns["project_id"]
    assert project_column["nullable"] is True
    assert isinstance(project_column["type"], sa.LargeBinary)
    assert isinstance(SqlScheduledTask.__table__.c.project_id.type, Uuid16)
    assert inspector.get_foreign_keys("scheduled_tasks") == []

    indexes = {index["name"]: index for index in inspector.get_indexes("scheduled_tasks")}
    assert indexes["ix_scheduled_tasks_project_id"]["unique"] == 0
    assert indexes["ix_scheduled_tasks_project_id"]["column_names"] == [
        "workspace_id",
        "user_id",
        "project_id",
        "created_at",
        "id",
    ]
    assert indexes["ix_scheduled_tasks_user_scope"]["column_names"] == [
        "workspace_id",
        "user_id",
        "created_at",
        "id",
    ]
    clear_engine_cache()


def test_upgrade_leaves_existing_tasks_project_id_null(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'upgrade.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, _PREVIOUS_HEAD)
    _insert_task_and_run(engine)
    _assert_distinct_payload_survives(engine)
    task_defaults_before = _column_defaults(engine, "scheduled_tasks")
    run_defaults_before = _column_defaults(engine, "scheduled_task_runs")
    task_indexes_before = _indexes(engine, "scheduled_tasks")
    run_indexes_before = _indexes(engine, "scheduled_task_runs")
    statements: list[str] = []

    def _capture_statement(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        statements.append(statement.lower().replace('"', ""))

    sa.event.listen(engine, "before_cursor_execute", _capture_statement)

    try:
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "a5363b7c9d2e")
    finally:
        sa.event.remove(engine, "before_cursor_execute", _capture_statement)

    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT project_id FROM scheduled_tasks")).scalar_one() is None
    assert any(
        "alter table scheduled_tasks add column project_id" in statement
        for statement in statements
    )
    assert not any("_alembic_tmp_scheduled_tasks" in statement for statement in statements)
    _assert_distinct_payload_survives(engine)
    task_defaults_after = _column_defaults(engine, "scheduled_tasks")
    assert {
        name: task_defaults_after[name] for name in task_defaults_before
    } == task_defaults_before
    assert task_defaults_after["project_id"] is None
    assert _column_defaults(engine, "scheduled_task_runs") == run_defaults_before
    assert _indexes(engine, "scheduled_tasks") == {
        **task_indexes_before,
        "ix_scheduled_tasks_project_id": (
            False,
            ("workspace_id", "user_id", "project_id", "created_at", "id"),
        ),
    }
    assert _indexes(engine, "scheduled_task_runs") == run_indexes_before
    engine.dispose()


def test_project_assignment_migration_downgrade_round_trip(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'round_trip.db'}"
    engine = get_or_create_engine(uri)
    _insert_task_and_run(engine)
    _assert_distinct_payload_survives(engine)
    with engine.begin() as conn:
        conn.execute(sa.text(f"UPDATE scheduled_tasks SET project_id = X'{_PROJECT_ID}'"))
    head_task_defaults = _column_defaults(engine, "scheduled_tasks")
    head_run_defaults = _column_defaults(engine, "scheduled_task_runs")
    head_task_indexes = _indexes(engine, "scheduled_tasks")
    head_run_indexes = _indexes(engine, "scheduled_task_runs")

    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, _PREVIOUS_HEAD)

    inspector = sa.inspect(engine)
    assert "project_id" not in {
        column["name"] for column in inspector.get_columns("scheduled_tasks")
    }
    assert "ix_scheduled_tasks_project_id" not in {
        index["name"] for index in inspector.get_indexes("scheduled_tasks")
    }
    _assert_distinct_payload_survives(engine)
    assert _column_defaults(engine, "scheduled_tasks") == {
        name: default for name, default in head_task_defaults.items() if name != "project_id"
    }
    assert _column_defaults(engine, "scheduled_task_runs") == head_run_defaults
    assert _indexes(engine, "scheduled_tasks") == {
        name: signature
        for name, signature in head_task_indexes.items()
        if name != "ix_scheduled_tasks_project_id"
    }
    assert _indexes(engine, "scheduled_task_runs") == head_run_indexes

    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "head")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT project_id FROM scheduled_tasks")).scalar_one() is None
    _assert_distinct_payload_survives(engine)
    assert _column_defaults(engine, "scheduled_tasks") == head_task_defaults
    assert _column_defaults(engine, "scheduled_task_runs") == head_run_defaults
    assert _indexes(engine, "scheduled_tasks") == head_task_indexes
    assert _indexes(engine, "scheduled_task_runs") == head_run_indexes
    engine.dispose()
    clear_engine_cache()


def test_deployed_project_revision_runs_new_parallel_migration(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'deployed-project-revision.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)

    # This is the production state that exposed the regression: the project
    # migration was already stamped before the sandbox migration existed.
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "a5363b7c9d2e")
    assert "project_id" in {
        column["name"] for column in sa.inspect(engine).get_columns("scheduled_tasks")
    }
    assert "terminating_sandbox_id" not in {
        column["name"] for column in sa.inspect(engine).get_columns("hosts")
    }

    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "head")
    assert "terminating_sandbox_id" in {
        column["name"] for column in sa.inspect(engine).get_columns("hosts")
    }
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "c18e2f7a4b90"
        )

    engine.dispose()
    clear_engine_cache()


def test_single_alembic_head_after_project_merge(tmp_path: Path) -> None:
    config = _build_alembic_config(f"sqlite:///{tmp_path / 'heads.db'}")
    assert ScriptDirectory.from_config(config).get_heads() == ["c18e2f7a4b90"]


@pytest.mark.parametrize("dialect", ["postgresql", "sqlite", "mysql"])
def test_project_index_ddl_matches_backend(monkeypatch: pytest.MonkeyPatch, dialect: str) -> None:
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))
    revision = script.get_revision("a5363b7c9d2e")
    assert revision is not None
    migration = revision.module

    events: list[tuple[str, bool]] = []
    in_autocommit = False
    create_index = Mock(
        side_effect=lambda *args, **kwargs: events.append(("create", in_autocommit))
    )
    drop_index = Mock(side_effect=lambda *args, **kwargs: events.append(("drop", in_autocommit)))
    batch_op = SimpleNamespace(drop_column=Mock())

    @contextmanager
    def autocommit_block():
        nonlocal in_autocommit
        in_autocommit = True
        try:
            yield
        finally:
            in_autocommit = False

    @contextmanager
    def batch_alter_table(*args: object, **kwargs: object):
        yield batch_op

    monkeypatch.setattr(
        migration.op, "get_bind", lambda: SimpleNamespace(dialect=SimpleNamespace(name=dialect))
    )
    monkeypatch.setattr(
        migration.op,
        "get_context",
        lambda: SimpleNamespace(autocommit_block=autocommit_block),
    )
    monkeypatch.setattr(migration.op, "add_column", Mock())
    monkeypatch.setattr(migration.op, "create_index", create_index)
    monkeypatch.setattr(migration.op, "drop_index", drop_index)
    monkeypatch.setattr(migration.op, "batch_alter_table", batch_alter_table)

    migration.upgrade()
    migration.downgrade()

    expected_concurrent = dialect == "postgresql"
    assert events == [("create", expected_concurrent), ("drop", expected_concurrent)]
    assert (
        create_index.call_args.kwargs.get("postgresql_concurrently", False) is expected_concurrent
    )
    assert drop_index.call_args.kwargs.get("postgresql_concurrently", False) is expected_concurrent
