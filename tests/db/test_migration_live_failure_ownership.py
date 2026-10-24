"""Additive storage for conditional status/error recovery, with no runtime consumer."""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.orm import Session

from omnigent.db.db_models import ConversationBase, OmnigentBase, SqlConversationMetadata
from omnigent.db.utils import _build_alembic_config

_PREVIOUS = "c8e4f2a6b901"
_REVISION = "d3a9e7b1c402"
_TABLE = "omnigent_conversation_metadata"
_ID = bytes.fromhex("0123456789abcdef0123456789abcdef")


def _migrate(engine: sa.Engine, revision: str, *, downgrade: bool = False) -> None:
    config = _build_alembic_config(str(engine.url))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        (command.downgrade if downgrade else command.upgrade)(config, revision)


def _columns(engine: sa.Engine, table: str) -> list[dict[str, object]]:
    return [
        {**column, "type": str(column["type"])} for column in sa.inspect(engine).get_columns(table)
    ]


@pytest.fixture
def prior_engine(tmp_path: Path) -> Iterator[sa.Engine]:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'failure-ownership.db'}")
    try:
        _migrate(engine, _PREVIOUS)
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO omnigent_conversation_metadata "
                    "(workspace_id, id, kind, runner_id, live_status) "
                    "VALUES (0, :id, 1, 'runner-original', 4)"
                ),
                {"id": _ID},
            )
        yield engine
    finally:
        engine.dispose()


@pytest.mark.parametrize("partial", ["none", "column", "complete_unstamped"])
def test_upgrade_preserves_legacy_state_and_resumes_partial_ddl(
    prior_engine: sa.Engine, partial: str
) -> None:
    inspector = sa.inspect(prior_engine)
    old_columns = _columns(prior_engine, _TABLE)
    old_indexes = inspector.get_indexes(_TABLE)
    old_checks = inspector.get_check_constraints(_TABLE)
    ap_columns = _columns(prior_engine, "conversations")
    with prior_engine.connect() as connection:
        old_row = connection.execute(sa.text(f"SELECT * FROM {_TABLE}")).one()
    if partial == "column":
        with prior_engine.begin() as connection:
            connection.execute(
                sa.text(f"ALTER TABLE {_TABLE} ADD COLUMN live_status_version BLOB")
            )
    elif partial == "complete_unstamped":
        _migrate(prior_engine, _REVISION)
        with prior_engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE alembic_version SET version_num = :previous"),
                {"previous": _PREVIOUS},
            )

    _migrate(prior_engine, _REVISION)
    inspector = sa.inspect(prior_engine)
    columns = {column["name"]: column for column in inspector.get_columns(_TABLE)}
    for name in ("live_status_version", "live_error_state"):
        assert columns[name]["nullable"]
        assert columns[name]["default"] is None
        assert isinstance(columns[name]["type"], sa.LargeBinary)
    assert inspector.get_indexes(_TABLE) == old_indexes
    assert _columns(prior_engine, "conversations") == ap_columns
    with prior_engine.begin() as connection:
        row = connection.execute(sa.text(f"SELECT * FROM {_TABLE}")).one()
        assert row[:-2] == old_row
        assert row[-2:] == (None, None)
        # A legacy writer can still update only the old status field.
        connection.execute(sa.text(f"UPDATE {_TABLE} SET live_status = 1"))

    _migrate(prior_engine, _PREVIOUS, downgrade=True)
    inspector = sa.inspect(prior_engine)
    assert _columns(prior_engine, _TABLE) == old_columns
    assert inspector.get_indexes(_TABLE) == old_indexes
    assert inspector.get_check_constraints(_TABLE) == old_checks
    with prior_engine.connect() as connection:
        assert connection.execute(
            sa.text(f"SELECT runner_id, live_status FROM {_TABLE}")
        ).one() == ("runner-original", 1)


@pytest.mark.parametrize(
    "version,snapshot",
    [(b"v" * 15, None), (b"v" * 17, None), (None, b"error"), (b"v" * 16, b"x" * 16384)],
    ids=["short_version", "long_version", "snapshot_without_version", "oversized_snapshot"],
)
def test_database_rejects_unbounded_or_unowned_state(
    prior_engine: sa.Engine, version: bytes | None, snapshot: bytes | None
) -> None:
    _migrate(prior_engine, _REVISION)
    with pytest.raises(sa.exc.IntegrityError), prior_engine.begin() as connection:
        connection.execute(
            sa.text(
                f"UPDATE {_TABLE} SET live_status_version = :version, live_error_state = :state"
            ),
            {"version": version, "state": snapshot},
        )


def test_snapshot_and_status_share_a_versioned_atomic_update(prior_engine: sa.Engine) -> None:
    _migrate(prior_engine, _REVISION)
    original_version, newer_version, recovered_version = (
        bytes([value]) * 16 for value in (1, 2, 3)
    )
    snapshot = b"x" * 16383
    with prior_engine.begin() as connection:
        connection.execute(
            sa.text(
                f"UPDATE {_TABLE} SET live_status_version = :version, live_error_state = :state"
            ),
            {"version": original_version, "state": snapshot},
        )
        connection.execute(
            sa.text(
                f"UPDATE {_TABLE} SET live_status_version = :version, live_error_state = :state"
            ),
            {"version": newer_version, "state": b"newer failure"},
        )
        recovery = sa.text(
            f"UPDATE {_TABLE} SET live_status = 1, live_status_version = :next_version, "
            "live_error_state = NULL WHERE workspace_id = :workspace_id AND id = :id "
            "AND runner_id = :runner AND live_status_version = :expected"
        )
        params = {
            "workspace_id": 0,
            "id": _ID,
            "runner": "runner-original",
            "expected": original_version,
            "next_version": recovered_version,
        }
        assert connection.execute(recovery, params).rowcount == 0
        assert connection.execute(
            sa.text(f"SELECT live_status, live_status_version, live_error_state FROM {_TABLE}")
        ).one() == (4, newer_version, b"newer failure")
        params["expected"] = newer_version
        params["workspace_id"] = 1
        assert connection.execute(recovery, params).rowcount == 0
        params["workspace_id"] = 0
        params["runner"] = "replacement-runner"
        assert connection.execute(recovery, params).rowcount == 0
        params["runner"] = "runner-original"
        assert connection.execute(recovery, params).rowcount == 1
        assert connection.execute(
            sa.text(f"SELECT live_status, live_status_version, live_error_state FROM {_TABLE}")
        ).one() == (1, recovered_version, None)


@pytest.mark.parametrize("populated", [False, True])
@pytest.mark.parametrize("same_workspace", [False, True])
def test_downgrade_checks_later_batches_and_preserves_used_state(
    prior_engine: sa.Engine, populated: bool, same_workspace: bool
) -> None:
    _migrate(prior_engine, _REVISION)
    rows = [
        {
            "workspace_id": 1 if same_workspace else value,
            "id": value.to_bytes(16, "big"),
        }
        for value in range(1, 202)
    ]
    with prior_engine.begin() as connection:
        connection.execute(
            sa.text(
                f"INSERT INTO {_TABLE} (workspace_id, id, kind) VALUES (:workspace_id, :id, 1)"
            ),
            rows,
        )
        if populated:
            connection.execute(
                sa.text(
                    f"UPDATE {_TABLE} SET live_status_version = :version "
                    "WHERE workspace_id = :workspace_id AND id = :id"
                ),
                {**rows[-1], "version": b"v" * 16},
            )
    page_plans: list[str] = []

    def inspect_page(connection, _cursor, statement, parameters, _context, _executemany):
        if (
            statement.startswith("SELECT")
            and ".live_status_version" in statement
            and "WHERE" in statement
        ):
            page_plans.extend(
                row[3]
                for row in connection.exec_driver_sql(
                    "EXPLAIN QUERY PLAN " + statement, parameters
                )
            )

    sa.event.listen(prior_engine, "before_cursor_execute", inspect_page)
    if populated:
        with pytest.raises(RuntimeError, match="retain its columns"):
            _migrate(prior_engine, _PREVIOUS, downgrade=True)
        with prior_engine.connect() as connection:
            assert (
                connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == _REVISION
            )
            assert (
                connection.scalar(
                    sa.text(
                        f"SELECT live_status_version FROM {_TABLE} "
                        "WHERE workspace_id = :workspace_id AND id = :id"
                    ),
                    rows[-1],
                )
                == b"v" * 16
            )
    else:
        _migrate(prior_engine, _PREVIOUS, downgrade=True)
        assert "live_error_state" not in {
            column["name"] for column in sa.inspect(prior_engine).get_columns(_TABLE)
        }
    with prior_engine.connect() as connection:
        assert connection.scalar(sa.text(f"SELECT COUNT(*) FROM {_TABLE}")) == 202
    assert page_plans
    assert all("SEARCH" in plan and "SCAN" not in plan for plan in page_plans)


@pytest.mark.parametrize("dialect,uuid_type", [("mysql", "BINARY(16)"), ("postgresql", "BYTEA")])
def test_offline_ddl_has_binary_bounds_without_defaults(dialect: str, uuid_type: str) -> None:
    output = io.StringIO()
    config = _build_alembic_config(f"{dialect}://localhost/schema_test")
    config.output_buffer = output
    command.upgrade(config, f"{_PREVIOUS}:{_REVISION}", sql=True)
    sql = output.getvalue()
    assert f"live_status_version {uuid_type}" in sql
    assert "length(live_error_state) <= 16383" in sql
    assert "live_error_state IS NULL OR live_status_version IS NOT NULL" in sql
    assert "DEFAULT" not in sql


def test_reserved_columns_do_not_change_orm_reads_or_writes(prior_engine: sa.Engine) -> None:
    reserved = {"live_status_version", "live_error_state"}
    assert reserved.issubset(SqlConversationMetadata.__table__.c.keys())
    assert reserved.isdisjoint(sa.inspect(SqlConversationMetadata).attrs.keys())
    assert reserved.isdisjoint(column["name"] for column in _columns(prior_engine, _TABLE))
    statements: list[str] = []

    def record_sql(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    sa.event.listen(prior_engine, "before_cursor_execute", record_sql)
    with Session(prior_engine) as session:
        existing = session.scalars(sa.select(SqlConversationMetadata)).one()
        existing.live_status = 1
        session.add(SqlConversationMetadata(id="fedcba9876543210fedcba9876543210", kind=1))
        session.commit()
    assert any(statement.startswith("INSERT") for statement in statements)
    assert any(statement.startswith("UPDATE") for statement in statements)
    assert any(statement.startswith("SELECT") for statement in statements)
    assert all(name not in statement for name in reserved for statement in statements)


def test_create_all_and_stamp_bootstrap_matches_migrated_ownership_schema(
    prior_engine: sa.Engine, tmp_path: Path
) -> None:
    _migrate(prior_engine, _REVISION)
    reserved = {"live_status_version", "live_error_state"}
    migrated_columns = [
        column for column in _columns(prior_engine, _TABLE) if column["name"] in reserved
    ]
    migrated_checks = {
        check["name"]: check["sqltext"]
        for check in sa.inspect(prior_engine).get_check_constraints(_TABLE)
        if check["name"].startswith("ck_conv_meta_live_")
    }
    bootstrap = sa.create_engine(f"sqlite:///{tmp_path / 'bootstrap.db'}")
    try:
        # Fresh CockroachDB uses model create_all + stamp, bypassing migration upgrade.
        OmnigentBase.metadata.create_all(bootstrap)
        ConversationBase.metadata.create_all(bootstrap)
        config = _build_alembic_config(str(bootstrap.url))
        with bootstrap.begin() as connection:
            config.attributes["connection"] = connection
            command.stamp(config, _REVISION)
        assert [
            column for column in _columns(bootstrap, _TABLE) if column["name"] in reserved
        ] == migrated_columns
        assert {
            check["name"]: check["sqltext"]
            for check in sa.inspect(bootstrap).get_check_constraints(_TABLE)
            if check["name"].startswith("ck_conv_meta_live_")
        } == migrated_checks
        _migrate(bootstrap, _PREVIOUS, downgrade=True)
        assert reserved.isdisjoint(column["name"] for column in _columns(bootstrap, _TABLE))
        _migrate(bootstrap, _REVISION)
        assert [
            column for column in _columns(bootstrap, _TABLE) if column["name"] in reserved
        ] == migrated_columns
    finally:
        bootstrap.dispose()
