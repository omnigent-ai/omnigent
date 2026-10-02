"""Migration ``mm1a2b3c4d5e`` admits the comment level (5) in session_permissions."""

from __future__ import annotations

from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations

from omnigent.db.cockroachdb import _crdb_server_version, _prepare_crdb_schema_transaction
from omnigent.db.utils import _build_alembic_config, get_or_create_engine

_TABLE = "session_permissions"
_CHECK = "ck_session_permissions_level"
_PREVIOUS = "ll1a2b3c4d5e"
_REVISION = "mm1a2b3c4d5e"
_MIGRATION = "omnigent.db.migrations.versions.mm1a2b3c4d5e_session_permissions_comment_level"
_CONVERSATION = bytes([7]) * 16
# A MySQL CHECK violation is an OperationalError (3819); other backends raise IntegrityError.
_CHECK_VIOLATION = (sa.exc.IntegrityError, sa.exc.OperationalError)


def _migrate(engine: sa.Engine, revision: str, *, downgrade: bool = False) -> None:
    config = _build_alembic_config(engine.url.render_as_string(hide_password=False))
    with engine.connect() as connection:
        if engine.dialect.name == "cockroachdb":
            _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
        config.attributes["connection"] = connection
        (command.downgrade if downgrade else command.upgrade)(config, revision)
        connection.commit()


def _insert(engine: sa.Engine, rows: list[tuple[int, str, int]]) -> None:
    table = sa.Table(_TABLE, sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(
            table.insert(),
            [
                {
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "conversation_id": _CONVERSATION,
                    "level": level,
                }
                for workspace_id, user_id, level in rows
            ],
        )


def _levels(engine: sa.Engine) -> dict[tuple[int, str], int]:
    with engine.connect() as connection:
        rows = connection.execute(sa.text(f"SELECT workspace_id, user_id, level FROM {_TABLE}"))
        return {(row.workspace_id, row.user_id): row.level for row in rows}


def test_comment_level_round_trips_through_migration(db_uri: str) -> None:
    engine = get_or_create_engine(db_uri)
    batch_size = import_module(_MIGRATION)._BATCH_SIZE
    # Enough comment grants to cross batch boundaries, spread over two workspaces,
    # interleaved with grants the downgrade must leave alone.
    seeded = [(0, "owner@example.com", 4), (17, "reader@example.com", 1)]
    seeded += [(i % 2 * 17, f"c{i:05d}@example.com", 5) for i in range(2 * batch_size + 3)]
    seeded += [(0, f"e{i:05d}@example.com", 2) for i in range(5)]
    mysql_plans: list[str] = []
    postgres_plans: dict[str, str] = {}

    def explain(conn, cursor, statement, parameters, context, executemany):
        dialect = engine.dialect.name
        is_continuation = statement.startswith(f"SELECT {_TABLE}.workspace_id") and (
            "WHERE" in statement
        )
        if dialect == "mysql" and is_continuation and not mysql_plans:
            mysql_plans.append(
                conn.exec_driver_sql("EXPLAIN ANALYZE " + statement, parameters).scalar_one()
            )
        elif dialect == "postgresql" and (
            is_continuation or statement.startswith(f"UPDATE {_TABLE} SET level")
        ):
            # Plan only (no ANALYZE: the UPDATE must not run twice). With sequential
            # scans priced out, a predicate the index can't seek shows up as a Filter.
            kind = "update" if executemany else "select"
            params = parameters[0] if executemany else parameters
            conn.exec_driver_sql("SET enable_seqscan = off")
            try:
                rows = conn.exec_driver_sql("EXPLAIN " + statement, params).all()
            finally:
                conn.exec_driver_sql("RESET enable_seqscan")
            # Keep the deepest batch: its cursor has the most earlier rows behind it.
            postgres_plans[kind] = "\n".join(row[0] for row in rows)

    try:
        _insert(engine, seeded)

        sa.event.listen(engine, "before_cursor_execute", explain)
        try:
            _migrate(engine, _PREVIOUS, downgrade=True)
        finally:
            sa.event.remove(engine, "before_cursor_execute", explain)
        if engine.dialect.name == "mysql":
            # Continuation batches must be primary-key range reads, not full scans.
            assert mysql_plans
            assert f"index range scan on {_TABLE} using primary" in mysql_plans[0].lower()
        if engine.dialect.name == "postgresql":
            # The cursor is an index condition, so rows scanned per batch stay
            # bounded however deep the cursor is; each demotion is a key lookup.
            select_plan, update_plan = postgres_plans["select"], postgres_plans["update"]
            assert "Index Cond" in select_plan and "Filter" not in select_plan, select_plan
            assert "Index Cond" in update_plan and "Seq Scan" not in update_plan, update_plan
        expected = {
            (workspace_id, user_id): 1 if level == 5 else level
            for workspace_id, user_id, level in seeded
        }
        # Downgrade keeps each commenter's access as read rather than deleting it.
        assert _levels(engine) == expected
        with pytest.raises(_CHECK_VIOLATION):
            _insert(engine, [(0, "rejected@example.com", 5)])

        _migrate(engine, _REVISION)
        _insert(engine, [(0, "second-commenter@example.com", 5)])
        assert _levels(engine)[(0, "second-commenter@example.com")] == 5
        with pytest.raises(_CHECK_VIOLATION):
            _insert(engine, [(0, "unknown@example.com", 6)])
    finally:
        _migrate(engine, "head")


def test_upgrade_resumes_after_the_constraint_was_dropped(db_uri: str) -> None:
    """A nontransactional backend can stop between the drop and the re-add."""
    engine = get_or_create_engine(db_uri)
    if engine.dialect.name == "sqlite":
        pytest.skip("SQLite rebuilds the table atomically")
    try:
        _migrate(engine, _PREVIOUS, downgrade=True)
        with engine.connect() as connection:
            # Prepare like _migrate: CockroachDB 23.2 needs a SERIALIZABLE schema
            # transaction, and preparation may commit, so not inside begin().
            if engine.dialect.name == "cockroachdb":
                _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
            connection.execute(sa.text(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {_CHECK}"))
            connection.commit()

        _migrate(engine, _REVISION)

        checks = {c["name"] for c in sa.inspect(engine).get_check_constraints(_TABLE)}
        assert _CHECK in checks
        # The widened constraint, not a re-created old one: 5 is admitted, 6 isn't.
        _insert(engine, [(0, "commenter@example.com", 5)])
        with pytest.raises(_CHECK_VIOLATION):
            _insert(engine, [(0, "unknown@example.com", 6)])
    finally:
        _migrate(engine, "head")


def test_offline_upgrade_is_refused() -> None:
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True})
    with Operations.context(context):
        with pytest.raises(RuntimeError, match="requires an online migration"):
            import_module(_MIGRATION).upgrade()


def test_offline_downgrade_is_refused() -> None:
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True})
    with Operations.context(context):
        with pytest.raises(RuntimeError, match="requires an online migration"):
            import_module(_MIGRATION).downgrade()


def test_interrupted_downgrade_resumes_without_touching_other_grants(db_uri: str) -> None:
    engine = get_or_create_engine(db_uri)
    batch_size = import_module(_MIGRATION)._BATCH_SIZE
    seeded = [(0, f"c{i:05d}@example.com", 5) for i in range(2 * batch_size + 3)]
    seeded += [(0, "owner@example.com", 4), (17, "editor@example.com", 2)]
    updates = 0

    def fail_second_batch(conn, cursor, statement, parameters, context, executemany):
        nonlocal updates
        if statement.startswith(f"UPDATE {_TABLE} SET level"):
            updates += 1
            if updates == 2:
                raise RuntimeError("interrupted second batch")

    try:
        _insert(engine, seeded)
        sa.event.listen(engine, "before_cursor_execute", fail_second_batch)
        try:
            with pytest.raises(RuntimeError, match="interrupted second batch"):
                _migrate(engine, _PREVIOUS, downgrade=True)
        finally:
            sa.event.remove(engine, "before_cursor_execute", fail_second_batch)
        partial = _levels(engine)
        assert partial[(0, "owner@example.com")] == 4
        assert partial[(17, "editor@example.com")] == 2
        # The first batch committed before the interruption; the rest is untouched.
        commenters = [level for (_, user), level in partial.items() if user.startswith("c")]
        assert commenters.count(1) == batch_size
        assert commenters.count(5) == len(commenters) - batch_size == batch_size + 3

        _migrate(engine, _PREVIOUS, downgrade=True)

        expected = {(ws, user): 1 if level == 5 else level for ws, user, level in seeded}
        assert _levels(engine) == expected
        with pytest.raises(_CHECK_VIOLATION):
            _insert(engine, [(0, "rejected@example.com", 5)])
    finally:
        _migrate(engine, "head")
