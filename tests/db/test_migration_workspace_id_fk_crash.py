"""Regression test: PK-widening upgrade vs a surviving foreign key.

Migration ``r1a2b3c4d5e6`` widens every primary key to
``(workspace_id, <existing pk cols>)`` and, on PostgreSQL, drops the old PK
with ``DROP CONSTRAINT`` first. It assumes ``p1a2b3c4d5e6`` already removed
every FK — but ``p1a2b3c4d5e6`` only drops FKs from a hardcoded list of five
tables, so any FK outside that list survives (e.g. a Postgres-default-named
``host_permissions_user_id_fkey`` referencing ``users.id`` on an
operator-added table). PostgreSQL then refuses
``ALTER TABLE users DROP CONSTRAINT users_pkey`` with
``DependentObjectsStillExist`` — and because migrations run at every server
boot, the deployment crash-loops.

This test rebuilds that exact upgrade scenario on a real PostgreSQL database:
stamp a scratch DB at ``o1a2b3c4d5e6`` (pre-FK-removal), populate it with real
rows, add an out-of-chain table holding a default-named FK to ``users.id``,
then run the boot-path upgrade to head. On the unfixed chain the upgrade
raises ``DependentObjectsStillExist`` (this test FAILS); with the fix the
upgrade reaches head, the pre-existing rows survive, and the operator's FK is
still present and enforced (re-bound to a parallel UNIQUE constraint on the
referenced columns rather than silently dropped).

PostgreSQL-only: SQLite rebuilds tables via ``batch_alter_table`` and never
executes ``DROP CONSTRAINT``, so the bug cannot manifest there. Runs in the
Postgres-backed ``databricks`` lane using the same ``OMNIGENT_TEST_DB_URI``
base the shared fixtures use, but on its own scratch database because it must
start below head (the shared per-worker DB is already migrated to head).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, _run_migrations

# Revision just before the FK-removal pair (p drops FKs, q is r's parent).
_PRE_FK_REMOVAL_REVISION = "o1a2b3c4d5e6"

_SCRATCH_DB_NAME = f"omnigent_test_pk_widen_fk_{os.environ.get('PYTEST_XDIST_WORKER', 'w0')}"


@pytest.fixture
def pg_scratch_uri() -> Iterator[str]:
    """A fresh, empty PostgreSQL database URI (dropped afterwards).

    Derives the scratch database from ``OMNIGENT_TEST_DB_URI`` (the same base
    the Postgres lane's shared fixtures use). Skips on SQLite / when no
    Postgres base is configured — the bug is PostgreSQL-specific DDL.
    """
    base_uri = os.environ.get("OMNIGENT_TEST_DB_URI", "")
    if not base_uri:
        pytest.skip("requires a PostgreSQL OMNIGENT_TEST_DB_URI")

    root_engine = sa.create_engine(base_uri, isolation_level="AUTOCOMMIT")
    if root_engine.dialect.name != "postgresql":
        root_engine.dispose()
        pytest.skip("DROP CONSTRAINT on a PK is PostgreSQL-only DDL")

    with root_engine.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{_SCRATCH_DB_NAME}"'))
        conn.execute(sa.text(f'CREATE DATABASE "{_SCRATCH_DB_NAME}"'))

    uri = re.sub(r"/[^/]*(\?.*)?$", f"/{_SCRATCH_DB_NAME}", base_uri)
    try:
        yield uri
    finally:
        with root_engine.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{_SCRATCH_DB_NAME}" WITH (FORCE)'))
        root_engine.dispose()


def _upgrade_to(uri: str, engine: sa.engine.Engine, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.connect() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, revision)


def _seed_populated_db_with_surviving_fk(engine: sa.engine.Engine) -> None:
    """Populate the pre-upgrade DB and add an out-of-chain FK to users.id.

    Mirrors the reported real-world shape: a table the FK-removal migration's
    hardcoded list does not cover (``host_permissions``), whose FK carries
    PostgreSQL's default constraint name (``<table>_<col>_fkey``), still
    referencing ``users.id`` when the PK-widening migration runs.
    """
    with engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO users (id, is_admin) VALUES ('alice@example.com', false)")
        )
        conn.execute(
            sa.text(
                "INSERT INTO conversations"
                " (id, created_at, updated_at, kind, root_conversation_id)"
                " VALUES ('c0ffee', 1, 1, 'default', 'c0ffee')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO session_permissions (user_id, conversation_id, level)"
                " VALUES ('alice@example.com', 'c0ffee', 4)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE host_permissions ("
                " user_id VARCHAR(128) NOT NULL"
                "   REFERENCES users(id) ON DELETE CASCADE,"
                " host_id VARCHAR(64) NOT NULL,"
                " level INTEGER NOT NULL,"
                " PRIMARY KEY (user_id, host_id))"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO host_permissions (user_id, host_id, level)"
                " VALUES ('alice@example.com', 'h1', 3)"
            )
        )


@pytest.mark.databricks
def test_boot_upgrade_survives_fk_referencing_widened_pk(pg_scratch_uri: str) -> None:
    """The boot-path upgrade must reach head despite a surviving FK.

    On the unfixed chain this fails inside ``r1a2b3c4d5e6`` with
    ``DependentObjectsStillExist`` (``cannot drop constraint users_pkey on
    table users because other objects depend on it``) — the crash that loops
    server startup on populated deployments. After the fix the upgrade
    completes: the PK is widened and the pre-existing rows survive.
    """
    engine = sa.create_engine(pg_scratch_uri)
    try:
        _upgrade_to(pg_scratch_uri, engine, _PRE_FK_REMOVAL_REVISION)
        _seed_populated_db_with_surviving_fk(engine)

        # Sanity: the FK this test is about actually exists, with the
        # Postgres default name that p1a2b3c4d5e6's convention misses.
        fk_names = {fk["name"] for fk in sa.inspect(engine).get_foreign_keys("host_permissions")}
        assert "host_permissions_user_id_fkey" in fk_names

        # The server boot path: every startup runs alembic upgrade head.
        _run_migrations(engine, pg_scratch_uri)

        # Reached head: the widened PK is in place and data survived.
        insp = sa.inspect(engine)
        assert insp.get_pk_constraint("users")["constrained_columns"] == [
            "workspace_id",
            "id",
        ]
        with engine.connect() as conn:
            level = conn.execute(
                sa.text(
                    "SELECT level FROM host_permissions"
                    " WHERE user_id = 'alice@example.com' AND host_id = 'h1'"
                )
            ).scalar_one()
        assert level == 3

        # The operator's FK was preserved (not silently dropped) …
        fks = sa.inspect(engine).get_foreign_keys("host_permissions")
        by_name = {fk["name"]: fk for fk in fks}
        assert "host_permissions_user_id_fkey" in by_name
        fk = by_name["host_permissions_user_id_fkey"]
        assert fk["referred_table"] == "users"
        assert fk["referred_columns"] == ["id"]
        assert fk["options"].get("ondelete", "").upper() == "CASCADE"

        # … and is genuinely enforced: a dangling reference must be rejected,
        # and the original ON DELETE CASCADE must still fire.
        with engine.connect() as conn:
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(
                    sa.text(
                        "INSERT INTO host_permissions (user_id, host_id, level)"
                        " VALUES ('ghost@example.com', 'h9', 1)"
                    )
                )
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM users WHERE id = 'alice@example.com'"))
            remaining = conn.execute(sa.text("SELECT count(*) FROM host_permissions")).scalar_one()
        assert remaining == 0
    finally:
        engine.dispose()


@pytest.mark.databricks
def test_widening_preserves_every_surviving_fk_shape(pg_scratch_uri: str) -> None:
    """All FK shapes bound to a widened PK survive: custom-named, multiple
    FKs onto the same columns (one shared parallel UNIQUE), and an FK whose
    child is itself a widened table."""
    engine = sa.create_engine(pg_scratch_uri)
    try:
        _upgrade_to(pg_scratch_uri, engine, _PRE_FK_REMOVAL_REVISION)
        with engine.begin() as conn:
            conn.execute(
                sa.text("INSERT INTO users (id, is_admin) VALUES ('bob@example.com', false)")
            )
            # Two external children referencing users.id: one default-named,
            # one custom-named — they must share a single parallel UNIQUE.
            conn.execute(
                sa.text(
                    "CREATE TABLE audit_log ("
                    " entry_id INTEGER PRIMARY KEY,"
                    " actor VARCHAR(128) NOT NULL REFERENCES users(id))"
                )
            )
            conn.execute(
                sa.text(
                    "CREATE TABLE api_keys ("
                    " key_id INTEGER PRIMARY KEY,"
                    " owner VARCHAR(128),"
                    " CONSTRAINT custom_owner_ref FOREIGN KEY (owner)"
                    "  REFERENCES users(id) ON DELETE SET NULL)"
                )
            )
            # An operator FK whose child is itself a widened table (one the
            # FK-removal migration's hardcoded list does not cover).
            conn.execute(
                sa.text(
                    "ALTER TABLE comments ADD CONSTRAINT comments_operator_user_ref"
                    " FOREIGN KEY (created_by) REFERENCES users(id)"
                )
            )

        _run_migrations(engine, pg_scratch_uri)

        insp = sa.inspect(engine)
        assert {fk["name"] for fk in insp.get_foreign_keys("audit_log")} == {
            "audit_log_actor_fkey"
        }
        assert {fk["name"] for fk in insp.get_foreign_keys("api_keys")} == {"custom_owner_ref"}
        assert "comments_operator_user_ref" in {
            fk["name"] for fk in insp.get_foreign_keys("comments")
        }

        # Exactly one parallel UNIQUE(id) backs all three FKs.
        unique_sets = [tuple(uc["column_names"]) for uc in insp.get_unique_constraints("users")]
        assert unique_sets.count(("id",)) == 1

        # Enforcement survived the round-trip.
        with engine.connect() as conn:
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(
                    sa.text("INSERT INTO audit_log (entry_id, actor) VALUES (1, 'nobody')")
                )
    finally:
        engine.dispose()
