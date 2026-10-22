"""Tests for scheduled-task schemas and the dispatch-field migration.

Verifies the migration creates both tables with the expected shape, that
neither carries a database-level foreign key (schema Rule R032 — the
``agent_id`` / ``conversation_id`` / ``scheduled_task_id`` relationships are
application-owned), that ``scheduled_tasks.rrule`` is NOT NULL and the
``scheduled_task_runs.status`` CHECK is enforced, and that a downgrade drops both
tables cleanly. Dispatch coverage includes occurrence deduplication,
legacy writers, history preservation, and guarded rollback.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from omnigent.db.db_models import SqlScheduledTask, SqlScheduledTaskRun
from omnigent.db.utils import (
    _build_alembic_config,
    clear_engine_cache,
    get_or_create_engine,
)

_PREVIOUS_HEAD = "9d820f91deef"
_DISPATCH_PREVIOUS = "nf12ab34cd56"
_DISPATCH_REVISION = "c8e4f2a6b901"
_SCHEMA_PATHS = [
    "migration",
    "metadata",
    pytest.param(
        "configured",
        marks=pytest.mark.skipif(
            not os.environ.get("OMNIGENT_TEST_DB_URI"), reason="needs a configured database"
        ),
    ),
]


@pytest.fixture
def db_engine(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Engine]:
    """Create a migrated database or just the task tables from ORM metadata."""
    if getattr(request, "param", "migration") == "configured":
        yield get_or_create_engine(request.getfixturevalue("db_uri"))
        return
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    if getattr(request, "param", "migration") == "metadata":
        engine = sa.create_engine(uri)
        SqlScheduledTask.__table__.create(engine)
        SqlScheduledTaskRun.__table__.create(engine)
    else:
        engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        engine.dispose()
        clear_engine_cache()


def test_migration_creates_both_tables(db_engine: Engine) -> None:
    """Both ``scheduled_tasks`` and ``scheduled_task_runs`` exist after migrating to head."""
    tables = set(sa.inspect(db_engine).get_table_names())
    assert "scheduled_tasks" in tables
    assert "scheduled_task_runs" in tables


@pytest.mark.parametrize("db_engine", _SCHEMA_PATHS, indirect=True)
def test_scheduled_tasks_columns(db_engine: Engine) -> None:
    """``scheduled_tasks`` has the full expected column set."""
    columns = {c["name"]: c for c in sa.inspect(db_engine).get_columns("scheduled_tasks")}
    for name in ("last_run_at", "created_at", "updated_at", "next_run_at"):
        # The CockroachDB dialect reflects INT8 as SQLAlchemy Integer.
        timestamp_type = sa.Integer if db_engine.dialect.name == "cockroachdb" else sa.BigInteger
        assert isinstance(columns[name]["type"], timestamp_type)
        assert columns[name]["nullable"] is (name != "created_at")
    cols = set(columns)
    assert cols == {
        "workspace_id",
        "id",
        "name",
        "prompt",
        "rrule",
        "user_id",
        "agent_id",
        "account_generation",
        "model_override",
        "reasoning_effort",
        "permission_mode",
        "max_cost_usd",
        "workspace",
        "base_branch",
        "execution_target",
        "host_id",
        "timezone",
        "state",
        "last_run_at",
        "last_run_conversation_id",
        "created_at",
        "updated_at",
        "next_run_at",
        "project_id",
        "delegation_ref",
    }


@pytest.mark.parametrize("db_engine", _SCHEMA_PATHS, indirect=True)
def test_scheduled_task_runs_columns(db_engine: Engine) -> None:
    """``scheduled_task_runs`` has the full expected column set."""
    columns = {c["name"]: c for c in sa.inspect(db_engine).get_columns("scheduled_task_runs")}
    for name in ("scheduled_at", "fired_at", "finished_at", "lease_expires_at"):
        # The CockroachDB dialect reflects INT8 as SQLAlchemy Integer.
        timestamp_type = sa.Integer if db_engine.dialect.name == "cockroachdb" else sa.BigInteger
        assert isinstance(columns[name]["type"], timestamp_type)
        assert columns[name]["nullable"] is (name != "scheduled_at")
    cols = set(columns)
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
        "error_code",
        "trigger_key",
        "host_id",
        "lease_expires_at",
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


@pytest.mark.parametrize("db_engine", _SCHEMA_PATHS, indirect=True)
def test_expected_indexes(db_engine: Engine) -> None:
    """Both tables expose the indexes that back the read paths."""
    insp = sa.inspect(db_engine)
    scheduled_tasks_idx = {i["name"] for i in insp.get_indexes("scheduled_tasks")}
    assert "ix_scheduled_tasks_user_scope" in scheduled_tasks_idx
    scheduled_tasks_idx_cols = {
        i["name"]: list(i["column_names"]) for i in insp.get_indexes("scheduled_tasks")
    }
    assert scheduled_tasks_idx_cols["ix_scheduled_tasks_user_scope"] == [
        "workspace_id",
        "user_id",
        "created_at",
        "id",
    ]
    assert scheduled_tasks_idx_cols["ix_scheduled_tasks_due"] == [
        "state",
        "next_run_at",
        "workspace_id",
        "id",
    ]
    assert "ix_scheduled_tasks_agent_id" not in scheduled_tasks_idx
    # The separate created_at + user_id listing indexes were folded into the
    # single user-scope index above: with the user_id filter pushed into SQL the
    # per-user list is a covered seek, and the boot scan reads whole rows
    # regardless (see migration f4664ca64ea8).
    assert "ix_scheduled_tasks_created_at" not in scheduled_tasks_idx
    assert "ix_scheduled_tasks_user_id" not in scheduled_tasks_idx
    # ix_scheduled_tasks_state was dropped earlier (see migration e5c8b1f4a2d7).
    assert "ix_scheduled_tasks_state" not in scheduled_tasks_idx
    runs_idx = {i["name"] for i in insp.get_indexes("scheduled_task_runs")}
    assert "ix_scheduled_task_runs_scheduled_task_id" in runs_idx
    runs_idx_cols = {
        i["name"]: list(i["column_names"]) for i in insp.get_indexes("scheduled_task_runs")
    }
    assert runs_idx_cols["ix_scheduled_task_runs_scheduled_task_id"] == [
        "workspace_id",
        "scheduled_task_id",
        "scheduled_at",
        "id",
    ]
    assert runs_idx_cols["ix_scheduled_task_runs_expired_claims"] == [
        "status",
        "lease_expires_at",
        "workspace_id",
        "id",
    ]
    unique_keys = {
        key["name"]: key["column_names"]
        for key in insp.get_unique_constraints("scheduled_task_runs")
    }
    assert unique_keys["uq_scheduled_task_runs_occurrence"] == [
        "workspace_id",
        "scheduled_task_id",
        "trigger_key",
    ]


def test_state_default_on_omitted_insert(db_engine: Engine) -> None:
    """A raw insert omitting ``state`` / ``workspace_id`` / ``execution_target``
    picks up their defaults.

    Only the integer server_defaults (``state`` / ``workspace_id`` /
    ``execution_target``) are exercised here — all are omitted from the insert
    and must fall back to their defaults.
    """
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_tasks "
                "(id, name, prompt, rrule, user_id, agent_id, "
                " timezone, created_at) "
                "VALUES (X'00000000000000000000000000000de1', 'n', 'p', "
                "'FREQ=DAILY;BYHOUR=9;BYMINUTE=0', 'u', 'ag_1', 'UTC', 1)"
            )
        )
        state, workspace_id, execution_target = conn.execute(
            sa.text(
                "SELECT state, workspace_id, execution_target FROM scheduled_tasks "
                "WHERE id = X'00000000000000000000000000000de1'"
            )
        ).one()
    assert state == 1  # 1 = 'active'
    assert workspace_id == 0
    assert execution_target == 1  # 1 = 'connected_host'


def test_execution_target_check_rejects_bad_code(db_engine: Engine) -> None:
    """The ``scheduled_tasks.execution_target`` CHECK rejects codes outside the set.

    execution_target is a stable int code (see enum_codecs
    SCHEDULED_TASK_EXECUTION_TARGET, codes 1-2); a code outside that range must
    fail the CHECK.
    """
    with pytest.raises(IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO scheduled_tasks "
                    "(id, name, prompt, rrule, user_id, agent_id, "
                    " timezone, execution_target, created_at) "
                    "VALUES (X'00000000000000000000000000e6bad0', 'n', 'p', "
                    "'FREQ=DAILY;BYHOUR=9;BYMINUTE=0', 'u', 'ag', 'UTC', 99, 1)"
                )
            )


def test_rrule_accepts_recurring_row(db_engine: Engine) -> None:
    """A row with ``rrule`` set inserts cleanly (the recurring trigger)."""
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_tasks "
                "(id, name, prompt, rrule, user_id, agent_id, "
                " timezone, created_at) "
                "VALUES (X'0000000000000000000000000000c40e', 'n', 'p', "
                "'FREQ=DAILY;BYHOUR=9;BYMINUTE=0', 'u', 'ag', 'UTC', 1)"
            )
        )


def test_rrule_is_not_null(db_engine: Engine) -> None:
    """A row omitting ``rrule`` fails the NOT NULL constraint."""
    with pytest.raises(IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO scheduled_tasks "
                    "(id, name, prompt, user_id, agent_id, timezone, "
                    " created_at) "
                    "VALUES (X'000000000000000000000000000000e0', 'n', 'p', "
                    "'u', 'ag', 'UTC', 1)"
                )
            )


def test_state_stored_as_smallint(db_engine: Engine) -> None:
    """The ``scheduled_tasks.state`` column is an integer type, not a VARCHAR."""
    cols = {c["name"]: c for c in sa.inspect(db_engine).get_columns("scheduled_tasks")}
    assert "INT" in str(cols["state"]["type"]).upper()


def test_state_check_rejects_bad_code(db_engine: Engine) -> None:
    """The ``scheduled_tasks.state`` CHECK rejects codes outside the closed set.

    State is stored as a stable int code (see enum_codecs SCHEDULED_TASK_STATE,
    codes 1-3); a code outside that range must fail the CHECK.
    """
    with pytest.raises(IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO scheduled_tasks "
                    "(id, name, prompt, rrule, user_id, agent_id, "
                    " timezone, state, created_at) "
                    "VALUES (X'00000000000000000000000000badc0d', 'n', 'p', "
                    "'FREQ=DAILY;BYHOUR=9;BYMINUTE=0', 'u', 'ag', 'UTC', 99, 1)"
                )
            )


def test_scheduled_task_runs_status_check_rejects_bad_code(db_engine: Engine) -> None:
    """The ``scheduled_task_runs.status`` CHECK rejects codes outside the closed set.

    Status is stored as a stable int code (see enum_codecs
    SCHEDULED_TASK_RUN_STATUS, codes 1-5, plus reserved starting=6); other codes fail
    the CHECK.
    """
    with pytest.raises(IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO scheduled_task_runs "
                    "(id, scheduled_task_id, status, scheduled_at) "
                    "VALUES (X'00000000000000000000000000005bad', "
                    "X'00000000000000000000000000000001', 99, 1)"
                )
            )


@pytest.mark.parametrize("status_code", [5, 6])
@pytest.mark.parametrize("db_engine", _SCHEMA_PATHS, indirect=True)
def test_scheduled_task_runs_status_check_accepts_valid_code(
    db_engine: Engine, status_code: int
) -> None:
    """A valid status code inserts cleanly."""
    runs = sa.Table("scheduled_task_runs", sa.MetaData(), autoload_with=db_engine)
    with db_engine.begin() as conn:
        conn.execute(
            runs.insert().values(
                id=b"1" * 16, scheduled_task_id=b"a" * 16, status=status_code, scheduled_at=1
            )
        )
        assert conn.execute(sa.select(runs.c.status)).scalar_one() == status_code


def test_scheduled_task_runs_status_stored_as_smallint(db_engine: Engine) -> None:
    """The ``status`` column is an integer type, not a VARCHAR."""
    cols = {c["name"]: c for c in sa.inspect(db_engine).get_columns("scheduled_task_runs")}
    assert "INT" in str(cols["status"]["type"]).upper()


@pytest.mark.parametrize(
    ("workspace_id", "task_id", "trigger_key", "scheduled_at", "duplicate"),
    [
        (1, b"a" * 16, "event:source:AbC", 100, True),
        (2, b"a" * 16, "event:source:AbC", 100, False),
        (1, b"b" * 16, "event:source:AbC", 100, False),
        (1, b"a" * 16, "manual:request-1", 100, False),
        (1, b"a" * 16, "event:source:abc", 100, False),
        (1, b"a" * 16, "event:source:AbC", 200, True),
        (1, b"a" * 16, "event:source:AbC ", 100, False),
    ],
)
@pytest.mark.parametrize("db_engine", _SCHEMA_PATHS, indirect=True)
def test_occurrence_uniqueness_is_scoped_to_workspace_and_task(
    db_engine: Engine,
    workspace_id: int,
    task_id: bytes,
    trigger_key: str,
    scheduled_at: int,
    duplicate: bool,
) -> None:
    runs = sa.Table("scheduled_task_runs", sa.MetaData(), autoload_with=db_engine)
    with db_engine.begin() as conn:
        conn.execute(
            runs.insert().values(
                workspace_id=1,
                id=b"1" * 16,
                scheduled_task_id=b"a" * 16,
                status=6,
                scheduled_at=100,
                trigger_key="event:source:AbC",
            )
        )
    second = runs.insert().values(
        workspace_id=workspace_id,
        id=b"2" * 16,
        scheduled_task_id=task_id,
        status=6,
        scheduled_at=scheduled_at,
        trigger_key=trigger_key,
    )
    # MySQL's PAD SPACE collation requires callers to exclude trailing whitespace.
    duplicate |= db_engine.dialect.name == "mysql" and trigger_key.endswith(" ")
    if duplicate:
        with pytest.raises(IntegrityError), db_engine.begin() as conn:
            conn.execute(second)
    else:
        with db_engine.begin() as conn:
            conn.execute(second)


@pytest.fixture
def dispatch_migration_engine(tmp_path: Path) -> Iterator[Engine]:
    """Pin rollback tests without rewinding the shared, head-migrated worker DB."""
    root_uri = os.environ.get("OMNIGENT_TEST_DB_URI")
    admin = sa.create_engine(root_uri, isolation_level="AUTOCOMMIT") if root_uri else None
    uri = f"sqlite:///{tmp_path / 'dispatch.db'}"
    name = f"dispatch_{uuid4().hex}"
    engine = None
    database_created = False
    try:
        if admin is not None:
            quoted = admin.dialect.identifier_preparer.quote(name)
            with admin.connect() as conn:
                conn.execute(sa.text(f"CREATE DATABASE {quoted}"))
            database_created = True
            uri = admin.url.set(database=name).render_as_string(hide_password=False)
        engine = sa.create_engine(uri)
        config = _build_alembic_config(uri)
        if engine.dialect.name == "cockroachdb":
            # CRDB bootstraps metadata at head; pin rollback tests to this revision.
            engine.dispose()
            engine = get_or_create_engine(uri).execution_options(isolation_level="SERIALIZABLE")
            command.downgrade(config, _DISPATCH_REVISION)
        else:
            command.upgrade(config, _DISPATCH_REVISION)
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        clear_engine_cache()
        if admin is not None:
            try:
                if database_created:
                    cascade = " CASCADE" if admin.dialect.name == "cockroachdb" else ""
                    with admin.connect() as conn:
                        conn.execute(sa.text(f"DROP DATABASE {quoted}{cascade}"))
            finally:
                admin.dispose()


def test_dispatch_migration_preserves_history_and_round_trips(
    dispatch_migration_engine: Engine,
) -> None:
    engine = dispatch_migration_engine
    config = _build_alembic_config(engine.url.render_as_string(hide_password=False))
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, _DISPATCH_PREVIOUS)
    if engine.dialect.name == "cockroachdb":
        with engine.begin() as conn:
            # Existing databases may have been created with default_int_size=4.
            for table_name, fields in (
                ("scheduled_tasks", ("created_at", "last_run_at", "updated_at")),
                ("scheduled_task_runs", ("scheduled_at", "fired_at", "finished_at")),
            ):
                # Recreate empty tables because CRDB cannot narrow indexed columns.
                ddl = conn.execute(sa.text(f"SHOW CREATE TABLE {table_name}")).one()[1]
                for field in fields:
                    assert f"{field} INT8" in ddl
                    ddl = ddl.replace(f"{field} INT8", f"{field} INT4")
                conn.execute(sa.text(f"DROP TABLE {table_name}"))
                conn.execute(sa.text(ddl))
    tasks = sa.Table("scheduled_tasks", sa.MetaData(), autoload_with=engine)
    runs = sa.Table("scheduled_task_runs", sa.MetaData(), autoload_with=engine)
    with engine.begin() as conn:
        conn.execute(
            tasks.insert().values(
                id=b"a" * 16,
                name="daily",
                prompt=b"p",
                rrule="FREQ=DAILY",
                agent_id=b"g" * 16,
                timezone="UTC",
                created_at=2**31 - 1,
                last_run_at=2**31 - 1,
                updated_at=2**31 - 1,
            )
        )
        conn.execute(
            runs.insert(),
            [
                {
                    "id": run_id,
                    "scheduled_task_id": b"a" * 16,
                    "status": 3,
                    "scheduled_at": 2**31 - 1,
                    "fired_at": 2**31 - 1,
                    "finished_at": 2**31 - 1,
                }
                for run_id in (b"1" * 16, b"2" * 16)
            ],
        )
        config.attributes["connection"] = conn
        command.upgrade(config, _DISPATCH_REVISION)
    new_tasks = sa.Table("scheduled_tasks", sa.MetaData(), autoload_with=engine)
    new_runs = sa.Table("scheduled_task_runs", sa.MetaData(), autoload_with=engine)
    for table, columns in (
        (new_tasks, ["next_run_at", "project_id", "delegation_ref"]),
        (new_runs, ["host_id", "trigger_key", "lease_expires_at"]),
    ):
        assert all(table.c[column].nullable for column in columns)
        with engine.connect() as conn:
            rows = conn.execute(sa.select(*(table.c[column] for column in columns))).all()
            assert rows and all(tuple(row) == (None, None, None) for row in rows)
    with engine.begin() as conn:
        for table, fields in (
            (new_tasks, ("created_at", "last_run_at", "updated_at")),
            (new_runs, ("scheduled_at", "fired_at", "finished_at")),
        ):
            query = sa.select(*(table.c[field] for field in fields))
            assert all(tuple(row) == (2**31 - 1,) * 3 for row in conn.execute(query))
            conn.execute(table.update().values(**dict.fromkeys(fields, 2**31 + 100)))
            assert all(tuple(row) == (2**31 + 100,) * 3 for row in conn.execute(query))
            conn.execute(table.update().values(**dict.fromkeys(fields, 2**31 - 1)))
        # An old writer can still insert an occurrence with the same timestamp.
        conn.execute(
            runs.insert().values(
                id=b"3" * 16,
                scheduled_task_id=b"a" * 16,
                status=3,
                scheduled_at=2**31 - 1,
            )
        )
        conn.execute(
            new_tasks.update()
            .where(new_tasks.c.id == b"a" * 16)
            .values(
                next_run_at=2**31 + 100,
                project_id=b"p" * 16,
                delegation_ref="delegations/example",
            )
        )
        conn.execute(
            new_runs.update()
            .where(new_runs.c.id == b"1" * 16)
            .values(
                host_id=b"h" * 16,
                trigger_key="schedule:100",
                lease_expires_at=2**31 + 120,
            )
        )
        assert conn.execute(
            sa.select(
                new_tasks.c.next_run_at,
                new_tasks.c.project_id,
                new_tasks.c.delegation_ref,
            )
        ).one() == (2**31 + 100, b"p" * 16, "delegations/example")
        assert conn.execute(
            sa.select(
                new_runs.c.host_id,
                new_runs.c.trigger_key,
                new_runs.c.lease_expires_at,
            ).where(new_runs.c.id == b"1" * 16)
        ).one() == (b"h" * 16, "schedule:100", 2**31 + 120)
        config.attributes["connection"] = conn
        command.downgrade(config, _DISPATCH_PREVIOUS)
    assert {c["name"] for c in sa.inspect(engine).get_columns("scheduled_tasks")} == set(
        tasks.c.keys()
    )
    assert {c["name"] for c in sa.inspect(engine).get_columns("scheduled_task_runs")} == set(
        runs.c.keys()
    )
    for table_name, fields in (
        ("scheduled_tasks", ("created_at", "last_run_at", "updated_at")),
        ("scheduled_task_runs", ("scheduled_at", "fired_at", "finished_at")),
    ):
        for column in sa.inspect(engine).get_columns(table_name):
            if column["name"] in fields and engine.dialect.name != "cockroachdb":
                assert isinstance(column["type"], sa.Integer)
                assert not isinstance(column["type"], sa.BigInteger)
    with engine.begin() as conn:
        assert conn.execute(sa.select(tasks.c.created_at)).scalar_one() == 2**31 - 1
        assert conn.execute(sa.select(runs.c.scheduled_at)).scalars().all() == [2**31 - 1] * 3
        assert conn.execute(sa.select(runs.c.status)).scalars().all() == [3, 3, 3]
        config.attributes["connection"] = conn
        command.upgrade(config, _DISPATCH_REVISION)


@pytest.mark.parametrize("timestamp", [-(2**31) - 1, 2**31])
def test_dispatch_downgrade_refuses_out_of_range_timestamps(
    dispatch_migration_engine: Engine, timestamp: int
) -> None:
    engine = dispatch_migration_engine
    tasks = sa.Table("scheduled_tasks", sa.MetaData(), autoload_with=engine)
    runs = sa.Table("scheduled_task_runs", sa.MetaData(), autoload_with=engine)
    with engine.begin() as conn:
        conn.execute(
            tasks.insert().values(
                id=b"a" * 16,
                name="daily",
                prompt=b"p",
                rrule="FREQ=DAILY",
                agent_id=b"g" * 16,
                timezone="UTC",
                created_at=1,
            )
        )
        conn.execute(
            runs.insert().values(
                id=b"1" * 16,
                scheduled_task_id=b"a" * 16,
                status=3,
                scheduled_at=1,
            )
        )
    config = _build_alembic_config(engine.url.render_as_string(hide_password=False))
    for table, fields in (
        (tasks, ("created_at", "last_run_at", "updated_at")),
        (runs, ("scheduled_at", "fired_at", "finished_at")),
    ):
        for field in fields:
            with engine.begin() as conn:
                conn.execute(table.update().values(**{field: timestamp}))
            with (
                pytest.raises(RuntimeError, match="timestamps outside signed 32-bit"),
                engine.begin() as conn,
            ):
                config.attributes["connection"] = conn
                command.downgrade(config, _DISPATCH_PREVIOUS)
            with engine.begin() as conn:
                assert conn.execute(sa.select(table.c[field])).scalar_one() == timestamp
                assert (
                    conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
                    == _DISPATCH_REVISION
                )
                conn.execute(table.update().values(**{field: 1}))
            assert "next_run_at" in {
                c["name"] for c in sa.inspect(engine).get_columns("scheduled_tasks")
            }
            assert "trigger_key" in {
                c["name"] for c in sa.inspect(engine).get_columns("scheduled_task_runs")
            }


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql", "mysql", "cockroachdb"])
def test_dispatch_downgrade_requires_live_connection(dialect: str) -> None:
    if dialect == "cockroachdb":
        pytest.importorskip("sqlalchemy_cockroachdb")
    config = _build_alembic_config(f"{dialect}://")
    config.output_buffer = StringIO()
    with pytest.raises(RuntimeError, match="Downgrade requires a live connection"):
        command.downgrade(config, f"{_DISPATCH_REVISION}:{_DISPATCH_PREVIOUS}", sql=True)
    assert "ALTER TABLE" not in config.output_buffer.getvalue()


def test_dispatch_downgrade_refuses_starting_runs(dispatch_migration_engine: Engine) -> None:
    db_engine = dispatch_migration_engine
    runs = sa.Table("scheduled_task_runs", sa.MetaData(), autoload_with=db_engine)
    with db_engine.begin() as conn:
        conn.execute(
            runs.insert().values(
                id=b"1" * 16,
                scheduled_task_id=b"a" * 16,
                status=6,
                scheduled_at=1,
            )
        )
    config = _build_alembic_config(db_engine.url.render_as_string(hide_password=False))
    with pytest.raises(RuntimeError, match="Reconcile starting"), db_engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, _DISPATCH_PREVIOUS)
    with db_engine.connect() as conn:
        assert conn.execute(sa.select(runs.c.status)).scalar_one() == 6
        assert (
            conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            == _DISPATCH_REVISION
        )
    assert "trigger_key" in {
        c["name"] for c in sa.inspect(db_engine).get_columns("scheduled_task_runs")
    }


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
