"""Extended tests for database utilities (omnigent/db/utils.py).

Covers public utilities NOT already tested in test_utils.py:
- normalize_database_url
- engine caching (get_or_create_engine returns same engine for same URI)
- make_managed_session_maker (commit/rollback semantics, terminator counts,
  write detection, pooled-connection reuse)
- ID generators (generate_file_id, generate_conversation_id, generate_task_id,
  generate_item_id for all types)
- FTS helpers (ensure_fts_table, insert_fts, delete_fts_by_conversation)
- Timestamp helpers (now_epoch, now_epoch_us, utc_day)
- clear_engine_cache
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, event, insert, inspect, select, text
from sqlalchemy import create_engine as create_plain_engine
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from omnigent.db.db_models import OmnigentBase, SqlUser
from omnigent.db.query_context import current_query_name, query_name_scope
from omnigent.db.utils import (
    _ITEM_TYPES,
    _create_engine,
    _is_read_only_statement,
    clear_engine_cache,
    delete_fts_by_conversation,
    ensure_fts_table,
    generate_conversation_id,
    generate_file_id,
    generate_item_id,
    generate_task_id,
    get_or_create_engine,
    insert_fts,
    make_managed_session_maker,
    make_named_managed_session_maker,
    normalize_database_url,
    now_epoch,
    now_epoch_us,
    run_write_transaction,
    shared_read_scope,
    utc_day,
)

# ── normalize_database_url ────────────────────────────


class TestNormalizeDatabaseUrl:
    def test_postgres_prefix(self) -> None:
        url = "postgres://user:pass@host/db"
        assert normalize_database_url(url) == "postgresql+psycopg://user:pass@host/db"

    def test_postgresql_prefix(self) -> None:
        url = "postgresql://user:pass@host/db"
        assert normalize_database_url(url) == "postgresql+psycopg://user:pass@host/db"

    def test_cockroachdb_prefix(self) -> None:
        url = "cockroachdb://user:pass@host/db"
        assert normalize_database_url(url) == "cockroachdb+psycopg://user:pass@host/db"

    def test_cockroachdb_psycopg_passthrough(self) -> None:
        url = "cockroachdb+psycopg://user:pass@host/db"
        assert normalize_database_url(url) == url

    def test_sqlite_passthrough(self) -> None:
        url = "sqlite:///path/to/db.sqlite"
        assert normalize_database_url(url) == url

    def test_already_psycopg_passthrough(self) -> None:
        url = "postgresql+psycopg://user:pass@host/db"
        assert normalize_database_url(url) == url

    def test_empty_string(self) -> None:
        assert normalize_database_url("") == ""

    def test_mysql_passthrough(self) -> None:
        url = "mysql://user:pass@host/db"
        assert normalize_database_url(url) == url


# ── Engine caching ────────────────────────────────────


class TestEngineCaching:
    @pytest.fixture(autouse=True)
    def _clean_cache(self) -> Iterator[None]:
        clear_engine_cache()
        yield
        clear_engine_cache()

    def test_same_uri_returns_same_engine(self, tmp_path: Path) -> None:
        db_path = tmp_path / "cache_test.db"
        uri = f"sqlite:///{db_path}"
        e1 = get_or_create_engine(uri)
        e2 = get_or_create_engine(uri)
        assert e1 is e2

    def test_different_uri_returns_different_engine(self, tmp_path: Path) -> None:
        uri1 = f"sqlite:///{tmp_path / 'a.db'}"
        uri2 = f"sqlite:///{tmp_path / 'b.db'}"
        e1 = get_or_create_engine(uri1)
        e2 = get_or_create_engine(uri2)
        assert e1 is not e2

    def test_clear_engine_cache_removes_engines(self, tmp_path: Path) -> None:
        uri = f"sqlite:///{tmp_path / 'clear.db'}"
        e1 = get_or_create_engine(uri)
        clear_engine_cache()
        e2 = get_or_create_engine(uri)
        assert e1 is not e2


# ── make_managed_session_maker ────────────────────────


class TestManagedSessionMaker:
    def test_auto_commit_on_success(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        with managed() as session:
            session.add(SqlUser(id="commit_test", is_admin=False))

        # Data should be visible in a new session
        with managed() as session:
            loaded = session.get(SqlUser, (0, "commit_test"))
            assert loaded is not None

    def test_auto_rollback_on_exception(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        with pytest.raises(ValueError):
            with managed() as session:
                session.add(SqlUser(id="rollback_test", is_admin=False))
                raise ValueError("simulated error")

        # Data should NOT be visible
        with managed() as session:
            loaded = session.get(SqlUser, (0, "rollback_test"))
            assert loaded is None

    def test_sqlite_foreign_keys_enabled(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        if engine.dialect.name != "sqlite":
            pytest.skip("PRAGMA foreign_keys is SQLite-only")
        managed = make_managed_session_maker(engine)

        with managed() as session:
            result = session.execute(text("PRAGMA foreign_keys")).scalar()
            assert result == 1

    def test_immediate_mode(self, db_uri: str) -> None:
        """immediate=True should not raise and still commit."""
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine, immediate=True)

        with managed() as session:
            session.add(SqlUser(id="immediate_test", is_admin=False))

        with managed() as session:
            loaded = session.get(SqlUser, (0, "immediate_test"))
            assert loaded is not None

    def test_named_session_covers_implicit_flush_and_commit(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_named_managed_session_maker(
            engine,
            query_name_prefix="omnigent.test_store",
        )
        observed_names: list[str | None] = []

        def capture_name(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            if statement.lstrip().upper().startswith("INSERT"):
                observed_names.append(current_query_name())

        event.listen(engine, "before_cursor_execute", capture_name)
        try:
            with managed("insert_user") as session:
                session.add(SqlUser(id="named_session_test", is_admin=False))
        finally:
            event.remove(engine, "before_cursor_execute", capture_name)

        assert observed_names == ["omnigent.test_store.insert_user"]
        assert current_query_name() is None

    def test_nested_query_name_overrides_and_restores_session_name(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_named_managed_session_maker(
            engine,
            query_name_prefix="omnigent.test_store",
        )

        with managed("outer_operation") as session:
            assert current_query_name() == "omnigent.test_store.outer_operation"
            with query_name_scope("omnigent.test_store.inner_query"):
                session.execute(text("SELECT 1"))
                assert current_query_name() == "omnigent.test_store.inner_query"
            assert current_query_name() == "omnigent.test_store.outer_operation"

        assert current_query_name() is None

    def test_named_session_rejects_empty_names(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        with pytest.raises(ValueError, match="query_name_prefix must not be empty"):
            make_named_managed_session_maker(engine, query_name_prefix=" ")

        managed = make_named_managed_session_maker(
            engine,
            query_name_prefix="omnigent.test_store",
        )
        with pytest.raises(ValueError, match="query_name must not be empty"):
            with managed(" "):
                raise AssertionError("empty query name entered its session")


# ── Transaction terminators ───────────────────────────


def _user_ids(db_uri: str) -> set[str]:
    """Read user ids through a separate engine, so only committed rows count."""
    reader = _create_engine(db_uri)
    try:
        with reader.connect() as connection:
            return set(connection.execute(select(SqlUser.id)).scalars())
    finally:
        reader.dispose()


def _upsert_user(dialect_name: str, user_id: str) -> Any:
    if dialect_name == "mysql":
        mysql_statement = mysql_insert(SqlUser).values(id=user_id, is_admin=False)
        return mysql_statement.on_duplicate_key_update(is_admin=True)
    dialect_insert = sqlite_insert if dialect_name == "sqlite" else postgresql_insert
    statement = dialect_insert(SqlUser).values(id=user_id, is_admin=False)
    return statement.on_conflict_do_update(
        index_elements=["workspace_id", "id"], set_={"is_admin": True}
    )


def _write_with_cte(session: Session, user_id: str) -> None:
    if session.get_bind().dialect.name == "mysql":
        pytest.skip("MySQL does not accept a WITH clause before INSERT")
    session.execute(
        text(
            # CockroachDB cannot infer an untyped placeholder inside a CTE.
            "WITH source AS (SELECT CAST(:user_id AS VARCHAR(128)) AS id) "
            "INSERT INTO users (workspace_id, id) SELECT 0, id FROM source"
        ),
        {"user_id": user_id},
    )


def _write_in_savepoint(session: Session, user_id: str) -> None:
    with session.begin_nested():
        session.add(SqlUser(id=user_id, is_admin=False))


_WRITE_PATHS: dict[str, Callable[[Session, str], object]] = {
    "orm_add_pending": lambda session, user_id: session.add(SqlUser(id=user_id, is_admin=False)),
    "orm_add_flushed": lambda session, user_id: (
        session.add(SqlUser(id=user_id, is_admin=False)),
        session.flush(),
    ),
    "session_execute_insert": lambda session, user_id: session.execute(
        insert(SqlUser).values(id=user_id, is_admin=False)
    ),
    "connection_execute_insert": lambda session, user_id: session.connection().execute(
        insert(SqlUser).values(id=user_id, is_admin=False)
    ),
    "text_insert": lambda session, user_id: session.execute(
        text("INSERT INTO users (workspace_id, id) VALUES (0, :user_id)"),
        {"user_id": user_id},
    ),
    "upsert": lambda session, user_id: session.execute(
        _upsert_user(session.get_bind().dialect.name, user_id)
    ),
    "cte_insert": _write_with_cte,
    "begin_nested_add": _write_in_savepoint,
    "merge": lambda session, user_id: session.merge(SqlUser(id=user_id, is_admin=False)),
    "executemany_insert": lambda session, user_id: session.execute(
        insert(SqlUser),
        [
            {"id": user_id, "is_admin": False},
            {"id": f"{user_id}_second", "is_admin": False},
        ],
    ),
}


class TestSingleTransactionTerminator:
    """Read-only and failed sessions send one ROLLBACK; writers COMMIT, then the
    pool's rollback-on-return follows."""

    @pytest.mark.parametrize(
        "statement",
        [
            "SELECT 1",
            "  select id from users",
            "/* omnigent.store.read */ SELECT 1",
            "-- comment\nSELECT 1",
            "(SELECT 1) UNION (SELECT 2)",
            "WITH recent AS (SELECT 1 AS id) SELECT id FROM recent",
            "SELECT updated_at, deleted_at FROM users ORDER BY created_at DESC",
            "SHOW TABLES",
            "EXPLAIN SELECT 1",
        ],
    )
    def test_read_statements_are_read_only(self, statement: str) -> None:
        assert _is_read_only_statement(statement)

    @pytest.mark.parametrize(
        "statement",
        [
            "INSERT INTO users (id) VALUES (1)",
            "update users set is_admin = 1",
            "/* name */ DELETE FROM users",
            "REPLACE INTO users (id) VALUES (1)",
            "WITH moved AS (DELETE FROM users RETURNING id) SELECT id FROM moved",
            "SELECT id FROM users WHERE id = 1 FOR UPDATE",
            "SELECT id FROM users LOCK IN SHARE MODE",
            "SELECT id INTO archived_users FROM users",
            "EXPLAIN ANALYZE DELETE FROM users",
            "SELECT nextval('user_ids')",
            "SELECT pg_notify('channel', 'payload')",
            "CREATE TABLE probe (id INTEGER)",
            "SET LOCAL statement_timeout = 1000",
            "PRAGMA user_version = 3",
            "BEGIN IMMEDIATE",
            "SAVEPOINT sa_savepoint_1",
            "CALL refresh_users()",
            "",
            "/* only a comment */",
        ],
    )
    def test_other_statements_count_as_writes(self, statement: str) -> None:
        assert not _is_read_only_statement(statement)

    def test_read_only_session_ends_with_one_rollback(
        self, db_uri: str, record_terminators: Callable[[Engine], list[str]]
    ) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)
        terminators = record_terminators(engine)

        with managed() as session:
            session.execute(select(SqlUser.id)).all()

        assert terminators == ["ROLLBACK"]

    def test_read_only_session_keeps_loaded_attributes(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)
        with managed() as session:
            session.add(SqlUser(id="loaded_user", is_admin=True))

        with managed() as session:
            loaded = session.get(SqlUser, (0, "loaded_user"))

        # Ending with ROLLBACK must not expire what the session returned.
        assert loaded is not None
        assert loaded.is_admin is True

    def test_failed_session_rolls_back_once(
        self, db_uri: str, record_terminators: Callable[[Engine], list[str]]
    ) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)
        terminators = record_terminators(engine)

        with pytest.raises(ValueError, match="simulated failure"):
            with managed() as session:
                session.execute(insert(SqlUser).values(id="failed_user", is_admin=False))
                raise ValueError("simulated failure")

        assert terminators == ["ROLLBACK"]
        assert "failed_user" not in _user_ids(db_uri)

    def test_sqlite_immediate_session_always_commits(
        self, db_uri: str, record_terminators: Callable[[Engine], list[str]]
    ) -> None:
        engine = get_or_create_engine(db_uri)
        if engine.dialect.name != "sqlite":
            pytest.skip("BEGIN IMMEDIATE is SQLite-only")
        managed = make_managed_session_maker(engine, immediate=True)
        terminators = record_terminators(engine)

        with managed() as session:
            session.execute(select(SqlUser.id)).all()

        assert terminators == ["COMMIT", "ROLLBACK"]

    def test_shared_read_scope_write_commits_once(
        self, db_uri: str, record_terminators: Callable[[Engine], list[str]]
    ) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)
        terminators = record_terminators(engine)

        with shared_read_scope():
            with managed() as session:
                session.execute(select(SqlUser.id)).all()
            with managed() as session:
                session.add(SqlUser(id="scope_writer", is_admin=False))

        assert terminators == ["COMMIT", "ROLLBACK"]
        assert "scope_writer" in _user_ids(db_uri)

    @pytest.mark.parametrize("write_path", sorted(_WRITE_PATHS))
    def test_every_write_path_commits_and_persists(
        self, db_uri: str, record_terminators: Callable[[Engine], list[str]], write_path: str
    ) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_named_managed_session_maker(engine, query_name_prefix="omnigent.test")
        terminators = record_terminators(engine)
        user_id = f"via_{write_path}"

        with managed(write_path) as session:
            _WRITE_PATHS[write_path](session, user_id)

        assert terminators == ["COMMIT", "ROLLBACK"]
        assert user_id in _user_ids(db_uri)

    def test_session_rejects_a_second_bind(self, db_uri: str, tmp_path: Path) -> None:
        engine = get_or_create_engine(db_uri)
        other_engine = get_or_create_engine(f"sqlite:///{tmp_path / 'other_bind.db'}")
        managed = make_managed_session_maker(engine)

        with pytest.raises(ValueError, match="bound to a single connection"):
            with managed() as session:
                session.execute(insert(SqlUser).values(id="first_bind_write", is_admin=False))
                session.execute(select(SqlUser.id), bind_arguments={"bind": other_engine})

        assert "first_bind_write" not in _user_ids(db_uri)
        assert engine.pool.checkedout() == 0
        assert other_engine.pool.checkedout() == 0

    def test_dirty_object_update_commits(
        self, db_uri: str, record_terminators: Callable[[Engine], list[str]]
    ) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)
        with managed() as session:
            session.add(SqlUser(id="dirty_user", is_admin=False))
        terminators = record_terminators(engine)

        with managed() as session:
            user = session.get(SqlUser, (0, "dirty_user"))
            assert user is not None
            user.is_admin = True

        assert terminators == ["COMMIT", "ROLLBACK"]
        with managed() as session:
            reloaded = session.get(SqlUser, (0, "dirty_user"))
        assert reloaded is not None
        assert reloaded.is_admin is True

    def test_delete_commits(
        self, db_uri: str, record_terminators: Callable[[Engine], list[str]]
    ) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)
        with managed() as session:
            session.add(SqlUser(id="deleted_user", is_admin=False))
        terminators = record_terminators(engine)

        with managed() as session:
            user = session.get(SqlUser, (0, "deleted_user"))
            session.delete(user)

        assert terminators == ["COMMIT", "ROLLBACK"]
        assert "deleted_user" not in _user_ids(db_uri)

    def test_write_after_an_explicit_mid_block_commit_commits(
        self, db_uri: str, record_terminators: Callable[[Engine], list[str]]
    ) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)
        terminators = record_terminators(engine)

        with managed() as session:
            session.add(SqlUser(id="before_mid_commit", is_admin=False))
            session.commit()
            session.add(SqlUser(id="after_mid_commit", is_admin=False))

        assert terminators == ["COMMIT", "COMMIT", "ROLLBACK"]
        assert {"before_mid_commit", "after_mid_commit"} <= _user_ids(db_uri)

    def test_flush_failure_at_exit_rolls_back_and_returns_the_connection(
        self, db_uri: str, record_terminators: Callable[[Engine], list[str]]
    ) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)
        with managed() as session:
            session.add(SqlUser(id="duplicate_user", is_admin=False))
        terminators = record_terminators(engine)

        with pytest.raises(IntegrityError):
            with managed() as session:
                session.add(SqlUser(id="unflushed_user", is_admin=False))
                session.add(SqlUser(id="duplicate_user", is_admin=True))

        assert "COMMIT" not in terminators
        assert terminators[-1] == "ROLLBACK"
        assert engine.pool.checkedout() == 0
        assert "unflushed_user" not in _user_ids(db_uri)

    def test_nested_sessions_on_a_shared_connection_pool_keep_outer_writes(self) -> None:
        # StaticPool hands every checkout the same DBAPI connection, so the
        # inner session shares the outer transaction.
        engine = create_plain_engine(
            "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        try:
            OmnigentBase.metadata.create_all(engine, tables=[SqlUser.__table__])
            managed = make_managed_session_maker(engine)

            with managed() as outer:
                outer.add(SqlUser(id="outer_write", is_admin=False))
                outer.flush()
                with managed() as inner:
                    inner.execute(select(SqlUser.id)).all()

            with engine.connect() as connection:
                persisted = set(connection.execute(select(SqlUser.id)).scalars())
            assert "outer_write" in persisted
        finally:
            engine.dispose()

    def test_ddl_commits(
        self, db_uri: str, record_terminators: Callable[[Engine], list[str]]
    ) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)
        terminators = record_terminators(engine)
        try:
            with managed() as session:
                session.execute(text("CREATE TABLE terminator_probe (id INTEGER PRIMARY KEY)"))
            assert terminators == ["COMMIT", "ROLLBACK"]
            assert "terminator_probe" in inspect(engine).get_table_names()
        finally:
            with engine.begin() as connection:
                connection.execute(text("DROP TABLE IF EXISTS terminator_probe"))


class TestPooledConnectionReuse:
    """Returned connections never carry an open transaction to the next user."""

    @pytest.fixture()
    def pooled_engine(self, db_uri: str) -> Iterator[tuple[Engine, list[int]]]:
        """A fresh engine plus the identities of each checked-out DBAPI connection."""
        engine = _create_engine(db_uri)
        # The first connect initialises the dialect, issuing its own ROLLBACK.
        engine.connect().close()
        checkouts: list[int] = []

        def _on_checkout(dbapi_connection: Any, _record: Any, _proxy: Any) -> None:
            checkouts.append(id(dbapi_connection))

        event.listen(engine, "checkout", _on_checkout)
        yield engine, checkouts
        engine.dispose()

    def test_read_only_session_does_not_pin_a_snapshot(
        self, db_uri: str, pooled_engine: tuple[Engine, list[int]]
    ) -> None:
        engine, checkouts = pooled_engine
        reader = make_managed_session_maker(engine)
        writer = make_managed_session_maker(get_or_create_engine(db_uri))

        with reader() as session:
            assert "written_elsewhere" not in set(session.execute(select(SqlUser.id)).scalars())
        with writer() as session:
            session.add(SqlUser(id="written_elsewhere", is_admin=False))
        with reader() as session:
            seen = set(session.execute(select(SqlUser.id)).scalars())

        assert len(set(checkouts)) == 1, "both reads must reuse one pooled connection"
        assert "written_elsewhere" in seen

    def test_connection_is_clean_after_failed_session(
        self, db_uri: str, pooled_engine: tuple[Engine, list[int]]
    ) -> None:
        engine, checkouts = pooled_engine
        managed = make_managed_session_maker(engine)

        with pytest.raises(ValueError, match="simulated failure"):
            with managed() as session:
                session.execute(insert(SqlUser).values(id="abandoned", is_admin=False))
                raise ValueError("simulated failure")
        with managed() as session:
            session.add(SqlUser(id="committed_next", is_admin=False))

        assert len(set(checkouts)) == 1, "both sessions must reuse one pooled connection"
        user_ids = _user_ids(db_uri)
        assert "committed_next" in user_ids
        assert "abandoned" not in user_ids

    def test_raw_dbapi_work_is_rolled_back_on_return(
        self, db_uri: str, pooled_engine: tuple[Engine, list[int]]
    ) -> None:
        engine, checkouts = pooled_engine
        managed = make_managed_session_maker(engine)
        with engine.connect() as connection:
            cursor = connection.connection.driver_connection.cursor()  # type: ignore[union-attr]
            cursor.execute("INSERT INTO users (workspace_id, id) VALUES (0, 'raw_dbapi')")
            cursor.close()
        with engine.connect() as connection:
            # A SQLAlchemy COMMIT first, then raw work that reopens a transaction.
            connection.execute(select(SqlUser.id)).all()
            connection.commit()
            cursor = connection.connection.driver_connection.cursor()  # type: ignore[union-attr]
            cursor.execute("INSERT INTO users (workspace_id, id) VALUES (0, 'raw_after_commit')")
            cursor.close()
        # Had the pool kept either raw transaction open, this COMMIT would publish it.
        with managed() as session:
            session.add(SqlUser(id="after_raw_dbapi", is_admin=False))

        assert len(set(checkouts)) == 1, "every use must share one pooled connection"
        user_ids = _user_ids(db_uri)
        assert "after_raw_dbapi" in user_ids
        assert "raw_dbapi" not in user_ids
        assert "raw_after_commit" not in user_ids

    def test_rollback_that_never_reached_the_driver_is_redone_on_return(
        self, db_uri: str, pooled_engine: tuple[Engine, list[int]]
    ) -> None:
        engine, checkouts = pooled_engine
        managed = make_managed_session_maker(engine)

        def _fail_before_rollback(_connection: Any) -> None:
            raise RuntimeError("simulated rollback listener failure")

        event.listen(engine, "rollback", _fail_before_rollback)
        connection = engine.connect()
        connection.execute(insert(SqlUser).values(id="rollback_skipped", is_admin=False))
        with pytest.raises(RuntimeError, match="simulated rollback listener failure"):
            connection.close()
        event.remove(engine, "rollback", _fail_before_rollback)
        connection.close()
        with managed() as session:
            session.add(SqlUser(id="after_skipped_rollback", is_admin=False))

        assert len(set(checkouts)) == 1, "both uses must share one pooled connection"
        user_ids = _user_ids(db_uri)
        assert "after_skipped_rollback" in user_ids
        assert "rollback_skipped" not in user_ids

    def test_failed_rollback_on_close_returns_the_connection(
        self,
        db_uri: str,
        pooled_engine: tuple[Engine, list[int]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine, _ = pooled_engine
        managed = make_managed_session_maker(engine)
        real_rollback = engine.dialect.do_rollback
        failures = [engine.dialect.loaded_dbapi.OperationalError("simulated rollback failure")]

        def _fail_first_rollback(dbapi_connection: Any) -> None:
            if failures:
                raise failures.pop()
            real_rollback(dbapi_connection)

        monkeypatch.setattr(engine.dialect, "do_rollback", _fail_first_rollback)
        with pytest.raises(OperationalError, match="simulated rollback failure"):
            with managed() as session:
                session.execute(insert(SqlUser).values(id="rollback_failed", is_admin=False))
                raise ValueError("abandon the write")

        assert engine.pool.checkedout() == 0
        with managed() as session:
            session.add(SqlUser(id="after_failed_rollback", is_admin=False))
        user_ids = _user_ids(db_uri)
        assert "after_failed_rollback" in user_ids
        assert "rollback_failed" not in user_ids

    def test_failed_commit_is_rolled_back_on_return(
        self,
        db_uri: str,
        pooled_engine: tuple[Engine, list[int]],
        monkeypatch: pytest.MonkeyPatch,
        record_terminators: Callable[[Engine], list[str]],
    ) -> None:
        engine, _ = pooled_engine
        managed = make_managed_session_maker(engine)
        terminators = record_terminators(engine)
        dbapi_error = engine.dialect.loaded_dbapi.OperationalError
        recorded_commit = engine.dialect.do_commit

        def _failing_commit(dbapi_connection: Any) -> None:
            terminators.append("COMMIT")
            raise dbapi_error("simulated commit failure")

        monkeypatch.setattr(engine.dialect, "do_commit", _failing_commit)
        with pytest.raises(OperationalError, match="simulated commit failure"):
            with managed() as session:
                session.add(SqlUser(id="commit_failed", is_admin=False))
        monkeypatch.setattr(engine.dialect, "do_commit", recorded_commit)

        assert terminators == ["COMMIT", "ROLLBACK"]
        # Had the failed transaction stayed open, this COMMIT would publish it.
        with managed() as session:
            session.add(SqlUser(id="after_failed_commit", is_admin=False))
        user_ids = _user_ids(db_uri)
        assert "after_failed_commit" in user_ids
        assert "commit_failed" not in user_ids

    def test_disconnect_surfaces_the_original_database_error(
        self,
        db_uri: str,
        pooled_engine: tuple[Engine, list[int]],
        record_terminators: Callable[[Engine], list[str]],
    ) -> None:
        engine, _ = pooled_engine
        failing_query = text("SELECT id FROM table_missing_for_disconnect")

        def _treat_as_disconnect(context: Any) -> None:
            if "table_missing_for_disconnect" in (context.statement or ""):
                context.is_disconnect = True

        event.listen(engine, "handle_error", _treat_as_disconnect)
        session_maker = make_named_managed_session_maker(engine, query_name_prefix="omnigent.test")

        with pytest.raises(DBAPIError):
            with session_maker("disconnect") as session:
                session.execute(failing_query)
        with pytest.raises(DBAPIError):
            with engine.connect() as connection:
                connection.execute(failing_query)
        with pytest.raises(DBAPIError):
            run_write_transaction(
                session_maker, "disconnect_write", lambda session: session.execute(failing_query)
            )

        terminators = record_terminators(engine)
        with session_maker("after_disconnect") as session:
            session.add(SqlUser(id="after_disconnect", is_admin=False))
        assert terminators == ["COMMIT", "ROLLBACK"]
        assert "after_disconnect" in _user_ids(db_uri)


# ── ID generators ─────────────────────────────────────


class TestIdGenerators:
    def test_generate_file_id_format(self) -> None:
        fid = generate_file_id()
        assert re.fullmatch(r"[0-9a-f]{32}", fid)

    def test_generate_conversation_id_format(self) -> None:
        cid = generate_conversation_id()
        assert re.fullmatch(r"[0-9a-f]{32}", cid)

    def test_generate_task_id_format(self) -> None:
        # response ids stay prefixed: the column is a polymorphic harness token,
        # not a bare uuid, so it is not narrowed to Uuid16.
        tid = generate_task_id()
        assert re.fullmatch(r"resp_[0-9a-f]{32}", tid)

    def test_generate_item_id_all_types(self) -> None:
        for item_type in _ITEM_TYPES:
            item_id = generate_item_id(item_type)
            assert re.fullmatch(r"[0-9a-f]{32}", item_id), f"{item_type} -> {item_id}"

    def test_generate_item_id_unknown_type_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown item type"):
            generate_item_id("nonexistent_type")

    def test_ids_are_unique(self) -> None:
        ids = {generate_file_id() for _ in range(100)}
        assert len(ids) == 100


# ── FTS helpers ───────────────────────────────────────


class TestFtsHelpers:
    def test_ensure_fts_table_idempotent(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        # Should not raise when called twice
        ensure_fts_table(engine)
        ensure_fts_table(engine)

    def test_insert_and_search_fts(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        if engine.dialect.name != "sqlite":
            pytest.skip("FTS5 virtual table is SQLite-only; no-op on other backends")
        ensure_fts_table(engine)
        managed = make_managed_session_maker(engine)

        with managed() as session:
            insert_fts(
                session,
                "9980c8a9248139f14f4165e5d53088aa",
                "8e32600337d08f59ad381caf96a90659",
                "hello world",
            )
            insert_fts(
                session,
                "0fd4e86b2daa009cd9929641dbd7dab6",
                "8e32600337d08f59ad381caf96a90659",
                "goodbye world",
            )

        with managed() as session:
            rows = session.execute(
                text(
                    "SELECT item_id FROM conversation_items_fts "
                    "WHERE conversation_items_fts MATCH 'hello'"
                )
            ).fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "9980c8a9248139f14f4165e5d53088aa"

    def test_delete_fts_by_conversation(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        if engine.dialect.name != "sqlite":
            pytest.skip("FTS5 virtual table is SQLite-only; no-op on other backends")
        ensure_fts_table(engine)
        managed = make_managed_session_maker(engine)

        with managed() as session:
            insert_fts(
                session,
                "83cd574b729d39c61cf72d568188531a",
                "553a265445caf1cdb034abe0b449485d",
                "searchable text",
            )
            insert_fts(
                session,
                "0917d69fc9d16f420210e37ed3ef5f31",
                "aacdf565fffe01a05b5f40d6c4ac83d7",
                "also searchable",
            )

        with managed() as session:
            delete_fts_by_conversation(session, "553a265445caf1cdb034abe0b449485d")

        with managed() as session:
            deleted = session.execute(
                text(
                    "SELECT item_id FROM conversation_items_fts"
                    " WHERE conversation_id = '553a265445caf1cdb034abe0b449485d'"
                )
            ).fetchall()
            assert len(deleted) == 0

            kept = session.execute(
                text(
                    "SELECT item_id FROM conversation_items_fts "
                    "WHERE conversation_id = 'aacdf565fffe01a05b5f40d6c4ac83d7'"
                )
            ).fetchall()
            assert len(kept) == 1


# ── Timestamp helpers ─────────────────────────────────


class TestTimestampHelpers:
    def test_now_epoch_is_close_to_time(self) -> None:
        before = int(time.time())
        result = now_epoch()
        after = int(time.time())
        assert before <= result <= after

    def test_now_epoch_us_is_microseconds(self) -> None:
        result = now_epoch_us()
        # Should be roughly time.time() * 1_000_000
        expected = int(time.time() * 1_000_000)
        assert abs(result - expected) < 1_000_000  # within 1 second

    def test_now_epoch_us_is_monotonically_increasing(self) -> None:
        """Two consecutive calls should produce non-decreasing values."""
        a = now_epoch_us()
        b = now_epoch_us()
        assert b >= a

    def test_utc_day_known_value(self) -> None:
        # 2026-06-16 00:00:00 UTC = 1781568000
        assert utc_day(1781568000) == "2026-06-16"

    def test_utc_day_midnight_boundary(self) -> None:
        # 1 second before midnight vs midnight
        day_before = utc_day(1781568000 - 1)
        day_at = utc_day(1781568000)
        assert day_before == "2026-06-15"
        assert day_at == "2026-06-16"

    def test_utc_day_format(self) -> None:
        result = utc_day(now_epoch())
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", result)
