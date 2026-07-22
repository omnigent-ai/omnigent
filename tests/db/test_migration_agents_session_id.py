"""Tests for the agents schema migration that replaces session_id with kind."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine

from omnigent.db.utils import (
    _build_alembic_config,
    clear_engine_cache,
    get_or_create_engine,
)


@pytest.fixture
def db_engine(tmp_path) -> Iterator[Engine]:
    """Fresh SQLite database with the full migration chain applied."""
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_agents_kind_column_exists_and_is_not_nullable(db_engine: Engine) -> None:
    """agents.kind is a NOT NULL column added by the migration."""
    columns = {c["name"]: c for c in sa.inspect(db_engine).get_columns("agents")}
    assert "kind" in columns, "agents.kind column must exist after migration"
    assert not columns["kind"]["nullable"], "agents.kind must be NOT NULL"


def test_agents_session_id_column_removed(db_engine: Engine) -> None:
    """agents.session_id must no longer exist after the migration."""
    columns = {c["name"] for c in sa.inspect(db_engine).get_columns("agents")}
    assert "session_id" not in columns, "agents.session_id must be dropped by migration"


def test_agents_session_id_index_removed(db_engine: Engine) -> None:
    """ix_agents_session_id must no longer exist after the migration."""
    index_names = {i["name"] for i in sa.inspect(db_engine).get_indexes("agents")}
    assert "ix_agents_session_id" not in index_names


def test_ix_conversations_agent_id_present(db_engine: Engine) -> None:
    """The agent-lookup index lives back on conversations at head.

    The agent binding merged back onto conversations (dropping the
    agent_configuration table), so the index is ix_conversations_agent_id.
    """
    assert "agent_configuration" not in set(sa.inspect(db_engine).get_table_names())
    conv_indexes = {i["name"] for i in sa.inspect(db_engine).get_indexes("conversations")}
    assert "ix_conversations_agent_id" in conv_indexes


def test_agents_name_index_exists(db_engine: Engine) -> None:
    """The template-name partial unique index is replaced by a plain name index.

    Template-name uniqueness now lives in the store (MySQL has no partial
    indexes); the DB keeps only a non-unique lookup index on
    ``(workspace_id, name, kind, id)`` — kind is included so the template
    lookup seeks past same-named session copies.
    """
    indexes = {i["name"]: i for i in sa.inspect(db_engine).get_indexes("agents")}
    assert "ix_agents_template_name" not in indexes
    assert "ix_agents_name" in indexes
    assert not indexes["ix_agents_name"]["unique"]
    assert indexes["ix_agents_name"]["column_names"] == ["workspace_id", "name", "kind", "id"]


def test_template_agent_kind_stored_and_read(db_engine: Engine) -> None:
    """A template agent inserted with kind='template' round-trips correctly."""
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                # kind=1 → 'template'
                "INSERT INTO agents (id, created_at, name, bundle_location, version, kind)"
                " VALUES (:id, :ts, :name, :loc, 1, 1)"
            ),
            {
                "id": "23803e78ca1677e73a1d8c6275de4150",
                "ts": 1700000001,
                "name": "my-template",
                "loc": "23803e78ca1677e73a1d8c6275de4150/bundle",
            },
        )
        kind = conn.execute(
            sa.text("SELECT kind FROM agents WHERE id = :id"),
            {"id": "23803e78ca1677e73a1d8c6275de4150"},
        ).scalar_one()
    assert kind == 1


def test_session_agent_kind_stored_and_read(db_engine: Engine) -> None:
    """A session-scoped agent inserted with kind='session' round-trips correctly."""
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                # kind=2 → 'session'
                "INSERT INTO agents (id, created_at, name, bundle_location, version, kind)"
                " VALUES (:id, :ts, :name, :loc, 1, 2)"
            ),
            {
                "id": "372d0296768feff7262c605c5553d1da",
                "ts": 1700000001,
                "name": "my-session",
                "loc": "372d0296768feff7262c605c5553d1da/bundle",
            },
        )
        kind = conn.execute(
            sa.text("SELECT kind FROM agents WHERE id = :id"),
            {"id": "372d0296768feff7262c605c5553d1da"},
        ).scalar_one()
    assert kind == 2


def test_agents_session_id_fk_accepts_existing_session(db_engine: Engine) -> None:
    """conversations.agent_id (forward pointer) accepts a valid agent id."""
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                # kind=2 → 'session'
                "INSERT INTO agents (id, created_at, name, bundle_location, version, kind)"
                " VALUES (:id, :ts, :name, :loc, 1, 2)"
            ),
            {
                "id": "552f255351da28d9c68a67cc9758840d",
                "ts": 1700000001,
                "name": "bound-agent",
                "loc": "552f255351da28d9c68a67cc9758840d/bundle",
            },
        )
        # The agent binding lives on the conversations row itself.
        conn.execute(
            sa.text(
                "INSERT INTO conversations"
                " (id, created_at, updated_at, root_conversation_id, agent_id)"
                " VALUES (:id, :ts, :ts, :id, :agent_id)"
            ),
            {
                "id": "e6c4a1ce71909cfba7d30a314c5f94ee",
                "ts": 1700000002,
                "agent_id": "552f255351da28d9c68a67cc9758840d",
            },
        )
        stored = conn.execute(
            sa.text("SELECT agent_id FROM conversations WHERE id = :id"),
            {"id": "e6c4a1ce71909cfba7d30a314c5f94ee"},
        ).scalar_one()
    assert stored == "552f255351da28d9c68a67cc9758840d"


def test_agents_session_id_fk_rejects_missing_session(db_engine: Engine) -> None:
    """Without DB FK, conversations.agent_id accepts any value including nonexistent agents.

    Referential integrity is now the application's responsibility.
    """
    # No IntegrityError expected — FK has been removed.
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversations"
                " (id, created_at, updated_at, root_conversation_id, agent_id)"
                " VALUES (:id, :ts, :ts, :id, :agent_id)"
            ),
            {
                "id": "5eca720dc2bc6cdc3a99028d7bd0f917",
                "ts": 1700000002,
                "agent_id": "5ff5b2e31fe10beb80134394037b17b0",
            },
        )
    # Clean up
    with db_engine.begin() as conn:
        conn.execute(
            sa.text("DELETE FROM conversations WHERE id = '5eca720dc2bc6cdc3a99028d7bd0f917'")
        )


def test_agents_allow_duplicate_template_names_at_db_layer(
    db_engine: Engine,
) -> None:
    """The DB no longer rejects duplicate template names.

    Uniqueness moved from a partial unique index to the store layer (MySQL
    has no partial indexes), so a raw double-insert succeeds; the guard lives
    in ``SqlAlchemyAgentStore.create`` (see tests/stores/test_agent_store.py).
    """
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO agents (id, created_at, name, bundle_location, version, kind)"
                " VALUES (:id1, :ts, 'dup-template', :loc1, 1, 1),"
                "        (:id2, :ts, 'dup-template', :loc2, 1, 1)"
            ),
            {
                "id1": "ef9a08aaf68eccf43b76051e6d818c5e",
                "id2": "5cdc35664c8c12c1fe200de768af454b",
                "ts": 1700000001,
                "loc1": "ef9a08aaf68eccf43b76051e6d818c5e/bundle",
                "loc2": "5cdc35664c8c12c1fe200de768af454b/bundle",
            },
        )
        count = conn.execute(
            sa.text("SELECT COUNT(*) FROM agents WHERE name = 'dup-template'")
        ).scalar_one()
        assert count == 2
        conn.execute(sa.text("DELETE FROM agents WHERE name = 'dup-template'"))


def test_agents_session_id_allows_duplicate_names_for_distinct_sessions(
    db_engine: Engine,
) -> None:
    """Two session-scoped agent copies can reuse the same spec name."""
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO agents (id, created_at, name, bundle_location, version, kind)"
                " VALUES (:id1, :ts, 'shared-name', :loc1, 1, 2),"
                "        (:id2, :ts, 'shared-name', :loc2, 1, 2)"
            ),
            {
                "id1": "122d503fe436c3e819c4e481fc9e959b",
                "id2": "2db4293f4feb844664bb1f50af9508a7",
                "ts": 1700000001,
                "loc1": "122d503fe436c3e819c4e481fc9e959b/bundle",
                "loc2": "2db4293f4feb844664bb1f50af9508a7/bundle",
            },
        )
        count = conn.execute(
            sa.text("SELECT COUNT(*) FROM agents WHERE name = 'shared-name'")
        ).scalar_one()
    assert count == 2


def test_upgrade_does_not_cascade_delete_conversations(tmp_path: Path) -> None:
    """Upgrade must not cascade-delete conversations bound to session-scoped agents.

    On SQLite, batch_alter_table drops and recreates the agents table.  If
    PRAGMA foreign_keys is ON, the DROP fires the ON DELETE CASCADE on
    conversations.agent_id → agents.id and silently wipes every conversation
    that owns an agent.  This test asserts that upgrade preserves them.
    """
    db_path = tmp_path / "upgrade_cascade.db"
    uri = f"sqlite:///{db_path}"

    # Build a raw engine (no auto-migration) to set up the pre-our-migration state.
    raw_engine = sa.create_engine(uri)

    # Migrate to the revision just before ours.
    config = _build_alembic_config(uri)
    with raw_engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "n1a2b3c4d5e6")

    # Seed one template agent, one session-scoped agent, and the conversation
    # bound to it — exactly the data that would be wiped by the cascade bug.
    with raw_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO agents (id, created_at, name, bundle_location, version)"
                " VALUES ('23803e78ca1677e73a1d8c6275de4150', 1, 'my-template',"
                " '23803e78ca1677e73a1d8c6275de4150/b', 1),"
                "        ('372d0296768feff7262c605c5553d1da', 2, 'my-session',"
                " '372d0296768feff7262c605c5553d1da/b', 1)"
            )
        )
        conn.execute(
            sa.text(
                # Seeded at n1 and upgraded only to o1 — both precede the enum→
                # SMALLINT migration (q1), so conversations.kind is still a string.
                "INSERT INTO conversations"
                " (id, created_at, updated_at, root_conversation_id, kind, agent_id)"
                " VALUES ('8e32600337d08f59ad381caf96a90659', 3, 3,"
                " '8e32600337d08f59ad381caf96a90659', 'default',"
                " '372d0296768feff7262c605c5553d1da')"
            )
        )
        conn.execute(
            sa.text(
                "UPDATE agents SET session_id = '8e32600337d08f59ad381caf96a90659'"
                " WHERE id = '372d0296768feff7262c605c5553d1da'"
            )
        )

    # Run our migration.
    config2 = _build_alembic_config(uri)
    with raw_engine.begin() as conn:
        config2.attributes["connection"] = conn
        command.upgrade(config2, "o1a2b3c4d5e6")

    with raw_engine.begin() as conn:
        conv_ids = list(conn.execute(sa.text("SELECT id FROM conversations")).scalars())
        agent_kinds = {
            row[0]: row[1] for row in conn.execute(sa.text("SELECT id, kind FROM agents"))
        }

    assert "8e32600337d08f59ad381caf96a90659" in conv_ids, (
        "Upgrade must not cascade-delete bound conversations"
    )
    assert agent_kinds.get("372d0296768feff7262c605c5553d1da") == "session"
    assert agent_kinds.get("23803e78ca1677e73a1d8c6275de4150") == "template"

    raw_engine.dispose()
    clear_engine_cache()


def test_agents_session_id_downgrade_round_trip(tmp_path: Path) -> None:
    """Downgrade restores session_id from conversations.agent_id and drops kind.

    Uses a raw engine (no auto-migration) to avoid SQLite FK enforcement issues:
    the o1a2b3c4d5e6 downgrade re-adds fk_agents_session_id (ON DELETE CASCADE),
    and subsequent batch_alter_table calls on conversations would cascade-delete
    agents if PRAGMA foreign_keys is ON. A raw engine keeps FK enforcement off.
    """
    db_path = tmp_path / "downgrade.db"
    uri = f"sqlite:///{db_path}"

    # Use a raw engine (no auto-migration) so PRAGMA foreign_keys stays OFF,
    # avoiding cascade issues from the re-added fk_agents_session_id FK.
    raw_engine = sa.create_engine(uri)

    # Migrate to current head first.
    config = _build_alembic_config(uri)
    with raw_engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "head")

    # Seed data on the upgraded schema: one template, one session-scoped agent.
    # This runs against the full chain (head), where agents.kind and
    # conversations.kind are int codes (1 = "template"/"default", 2 = "session").
    with raw_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO agents"
                " (workspace_id, id, created_at, name, bundle_location, version, kind)"
                " VALUES (0, '23803e78ca1677e73a1d8c6275de4150', 1,"
                " 'my-template', '23803e78ca1677e73a1d8c6275de4150/b', 1, 1),"
                "        (0, '372d0296768feff7262c605c5553d1da', 2,"
                " 'my-session', '372d0296768feff7262c605c5553d1da/b', 1, 2)"
            )
        )
        # agent_id lives back on conversations at head (the agent_configuration
        # table was merged away). The downgrade chain moves it out to
        # agent_configuration and back before the older downgrades read it.
        conn.execute(
            sa.text(
                "INSERT INTO conversations"
                " (workspace_id, id, created_at, updated_at, root_conversation_id, title,"
                " agent_id)"
                " VALUES (0, '8e32600337d08f59ad381caf96a90659', 3, 3,"
                " '8e32600337d08f59ad381caf96a90659', '',"
                " '372d0296768feff7262c605c5553d1da')"
            )
        )
        # kind lives on omnigent_conversation_metadata at head; insert a
        # matching row so the aa1b2c3d4e5f downgrade can restore kind to
        # conversations without leaving a NULL (which would break u1 downgrade).
        conn.execute(
            sa.text(
                "INSERT INTO omnigent_conversation_metadata"
                " (workspace_id, id, kind)"
                " VALUES (0, '8e32600337d08f59ad381caf96a90659', 1)"
            )
        )

    # Downgrade to n1a2b3c4d5e6 (runs o1a2b3c4d5e6 downgrade which restores session_id).
    config2 = _build_alembic_config(uri)
    with raw_engine.begin() as conn:
        config2.attributes["connection"] = conn
        command.downgrade(config2, "n1a2b3c4d5e6")

    # kind must be gone, session_id must be back.
    columns = {c["name"] for c in sa.inspect(raw_engine).get_columns("agents")}
    assert "kind" not in columns
    assert "session_id" in columns

    # The session-scoped agent should have session_id back-populated from
    # conversations.agent_id; the template agent should have NULL.
    with raw_engine.begin() as conn:
        rows = {
            row[0]: row[1]
            for row in conn.execute(sa.text("SELECT id, session_id FROM agents ORDER BY id"))
        }
    assert rows["23803e78ca1677e73a1d8c6275de4150"] is None
    assert rows["372d0296768feff7262c605c5553d1da"] == "8e32600337d08f59ad381caf96a90659"

    raw_engine.dispose()
    clear_engine_cache()
