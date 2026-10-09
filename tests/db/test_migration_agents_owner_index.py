"""Migration ``mm1a2b3c4d5e`` adds ``ix_agents_kind_owner_created``.

The new-session picker lists the caller's own agents newest first by walking
this index (equality on kind and owner, order on created_at), so it must exist
at head with the full primary key as its suffix, and the downgrade must remove it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import (
    _build_alembic_config,
    _run_migrations,
    clear_engine_cache,
    get_or_create_engine,
)

_INDEX = "ix_agents_kind_owner_created"


def _agent_indexes(engine: sa.Engine) -> dict[str, list[str]]:
    return {i["name"]: i["column_names"] for i in sa.inspect(engine).get_indexes("agents")}


def _is_unique(engine: sa.Engine) -> bool:
    return next(i for i in sa.inspect(engine).get_indexes("agents") if i["name"] == _INDEX)[
        "unique"
    ]


def test_index_at_head(tmp_path: Path) -> None:
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    try:
        assert _agent_indexes(engine)[_INDEX] == [
            "workspace_id",
            "kind",
            "created_by",
            "created_at",
            "id",
        ]
        assert not _is_unique(engine), "session copies share owner+name, so it can't be unique"
    finally:
        clear_engine_cache()


def test_downgrade_drops_index(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    cfg = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    try:
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "mm1a2b3c4d5e")
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "ll1a2b3c4d5e")
        assert _INDEX not in _agent_indexes(engine)
    finally:
        engine.dispose()
        clear_engine_cache()


def _pg_index_valid(conn: sa.Connection, schema: str) -> bool | None:
    return conn.execute(
        sa.text(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relname = :index AND n.nspname = :schema"
        ),
        {"index": _INDEX, "schema": schema},
    ).scalar()


@pytest.mark.skipif(
    not os.environ.get("OMNIGENT_TEST_DB_URI", "").startswith("postgresql"),
    reason="needs PostgreSQL (OMNIGENT_TEST_DB_URI)",
)
def test_postgres_retry_rebuilds_only_its_own_invalid_index() -> None:
    """A retry after a failed concurrent build rebuilds the agents index, and an
    INVALID index with the same name in another schema is left alone."""
    root = sa.make_url(os.environ["OMNIGENT_TEST_DB_URI"])
    name = f"mm1_recovery_{os.getpid()}"
    uri = root.set(database=name).render_as_string(hide_password=False)
    admin = sa.create_engine(root, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    setup = sa.create_engine(uri, isolation_level="AUTOCOMMIT")
    invalidate = "UPDATE pg_index SET indisvalid = false WHERE indexrelid = CAST(:i AS regclass)"
    try:
        with setup.connect() as conn:
            conn.execute(sa.text("CREATE SCHEMA other"))
            conn.execute(sa.text("CREATE TABLE other.t (x int)"))
            conn.execute(sa.text(f"CREATE INDEX {_INDEX} ON other.t (x)"))
            conn.execute(sa.text(invalidate), {"i": f"other.{_INDEX}"})
        _run_migrations(sa.create_engine(uri), uri)
        with setup.connect() as conn:
            conn.execute(sa.text(invalidate), {"i": f"public.{_INDEX}"})
            conn.execute(sa.text("UPDATE alembic_version SET version_num = 'll1a2b3c4d5e'"))
        _run_migrations(sa.create_engine(uri), uri)
        with setup.connect() as conn:
            assert _pg_index_valid(conn, "public") is True
            assert _pg_index_valid(conn, "other") is False, "another schema's index was touched"
    finally:
        setup.dispose()
        clear_engine_cache()
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
