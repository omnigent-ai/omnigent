"""Migration ``nn1a2b3c4d5e`` drops ``ix_agents_created_at``.

Both agent listings walk ``ix_agents_kind_owner_created``, so the old index is
gone at head, and the listings still seek an index rather than scan the table.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import event

from omnigent.db.utils import (
    _build_alembic_config,
    _get_current_db_revision,
    _run_migrations,
    clear_engine_cache,
    get_or_create_engine,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

_OLD = "ix_agents_created_at"
_SHARED = "ix_agents_kind_owner_created"


def _agent_indexes(engine: sa.Engine) -> dict[str, list[str]]:
    return {i["name"]: i["column_names"] for i in sa.inspect(engine).get_indexes("agents")}


def test_index_is_gone_at_head(tmp_path: Path) -> None:
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    try:
        assert _OLD not in _agent_indexes(engine)
        assert _SHARED in _agent_indexes(engine)
    finally:
        clear_engine_cache()


def test_downgrade_restores_index(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    cfg = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    try:
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "nn1a2b3c4d5e")
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "mm1a2b3c4d5e")
        assert _agent_indexes(engine)[_OLD] == ["workspace_id", "created_at", "id"]
    finally:
        engine.dispose()
        clear_engine_cache()


def test_both_listings_seek_the_shared_index(tmp_path: Path) -> None:
    """The store's own listing SQL, explained: an index seek, never a table scan."""
    uri = f"sqlite:///{tmp_path / 'plans.db'}"
    store = SqlAlchemyAgentStore(uri)
    for i in range(3):
        store.create(f"{i + 1:032x}", f"server{i}", f"x/{i}")
        own = f"{i + 100:032x}"
        store.create_user_agent(own, f"mine{i}", f"{own}/sha", owner="alice@example.com")
    engine = get_or_create_engine(uri)
    seen: list[tuple[str, tuple[object, ...]]] = []

    def record(_conn, _cursor, statement, parameters, _context, _many) -> None:
        seen.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", record)
    try:
        store.list(limit=2)
        server = seen[-1]
        store.list_user_agents("alice@example.com", limit=1, after=f"{101:032x}")
        mine = seen[-1]
    finally:
        event.remove(engine, "before_cursor_execute", record)
    try:
        with engine.connect() as conn:
            plans = {
                label: " / ".join(
                    row[-1] for row in conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {sql}", params)
                )
                for label, (sql, params) in (("server", server), ("mine", mine))
            }
    finally:
        clear_engine_cache()

    for label, plan in plans.items():
        assert f"USING INDEX {_SHARED}" in plan, (label, plan)
        assert "SCAN agents" not in plan, (label, plan)
        assert "TEMP B-TREE" not in plan, f"{label}: the index must supply the order: {plan}"
    assert "created_at<?" in plans["mine"], "a deep page must seek to the cursor"


def test_each_direction_can_be_retried(tmp_path: Path) -> None:
    """A step whose index DDL already took effect (MySQL commits DDL before alembic
    records the revision) finds that work done and still succeeds."""
    uri = f"sqlite:///{tmp_path / 'retry.db'}"
    cfg = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    try:
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "mm1a2b3c4d5e")
            conn.exec_driver_sql(f"DROP INDEX {_OLD}")
            command.upgrade(cfg, "nn1a2b3c4d5e")
        assert _OLD not in _agent_indexes(engine)
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            conn.exec_driver_sql(f"CREATE INDEX {_OLD} ON agents (workspace_id, created_at, id)")
            command.downgrade(cfg, "mm1a2b3c4d5e")
        assert _agent_indexes(engine)[_OLD] == ["workspace_id", "created_at", "id"]
    finally:
        engine.dispose()
        clear_engine_cache()


@pytest.mark.skipif(
    not os.environ.get("OMNIGENT_TEST_DB_URI", "").startswith("postgresql"),
    reason="needs PostgreSQL (OMNIGENT_TEST_DB_URI)",
)
def test_postgres_downgrade_retry_rebuilds_an_invalid_index() -> None:
    """A downgrade retried after its concurrent build failed rebuilds the index instead
    of keeping the INVALID one that IF NOT EXISTS alone would accept."""
    root = sa.make_url(os.environ["OMNIGENT_TEST_DB_URI"])
    name = f"nn1_recovery_{os.getpid()}"
    uri = root.set(database=name).render_as_string(hide_password=False)
    admin = sa.create_engine(root, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    engine = sa.create_engine(uri)
    setup = sa.create_engine(uri, isolation_level="AUTOCOMMIT")
    try:
        _run_migrations(engine, uri)
        with setup.connect() as conn:  # what an interrupted CREATE INDEX CONCURRENTLY leaves
            conn.execute(sa.text(f"CREATE INDEX {_OLD} ON agents (workspace_id, created_at, id)"))
            conn.execute(
                sa.text(
                    "UPDATE pg_index SET indisvalid = false "
                    "WHERE indexrelid = CAST(:i AS regclass)"
                ),
                {"i": _OLD},
            )
        cfg = _build_alembic_config(uri)
        with engine.connect() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "mm1a2b3c4d5e")
        with setup.connect() as conn:
            valid = conn.execute(
                sa.text(
                    "SELECT i.indisvalid FROM pg_index i "
                    "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :name"
                ),
                {"name": _OLD},
            ).scalar()
        assert valid is True
    finally:
        engine.dispose()
        setup.dispose()
        clear_engine_cache()
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_mysql_resumes_after_the_index_ddl_committed(db_uri: str, direction: str) -> None:
    """MySQL commits DDL before alembic records the revision: a step interrupted right
    after its index DDL is retried and finds that work already done."""
    engine = get_or_create_engine(db_uri)
    if engine.dialect.name != "mysql":
        pytest.skip("requires OMNIGENT_TEST_DB_URI pointing to MySQL")
    config = _build_alembic_config(db_uri)

    def migrate(target: str, *, down: bool = False) -> None:
        with engine.connect() as conn:
            config.attributes["connection"] = conn
            (command.downgrade if down else command.upgrade)(config, target)

    if direction == "upgrade":
        start, target, ddl = "mm1a2b3c4d5e", "nn1a2b3c4d5e", f"DROP INDEX {_OLD}"
    else:
        start, target, ddl = "nn1a2b3c4d5e", "mm1a2b3c4d5e", f"CREATE INDEX {_OLD}"
    migrate(start, down=True)

    def interrupt(_conn, _cursor, statement, _params, _context, _many) -> None:
        if statement.lstrip().startswith(ddl):  # compiled DROP INDEX starts with a newline
            raise RuntimeError("injected interruption after committed DDL")

    try:
        event.listen(engine, "after_cursor_execute", interrupt)
        try:
            with pytest.raises(RuntimeError, match="injected interruption"):
                migrate(target, down=direction == "downgrade")
        finally:
            event.remove(engine, "after_cursor_execute", interrupt)
        assert _get_current_db_revision(engine) == start
        migrate(target, down=direction == "downgrade")
        assert _get_current_db_revision(engine) == target
        assert (_OLD in _agent_indexes(engine)) is (direction == "downgrade")
    finally:
        migrate("head")  # leave the shared worker database at head
