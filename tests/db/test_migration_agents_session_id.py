"""Tests for the ``agents.session_id`` migration."""

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
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """
    Create a fresh SQLite database with the full migration chain.

    :param tmp_path: Per-test temporary directory.
    :returns: SQLAlchemy engine with migrations applied.
    """
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_agents_session_id_column_is_nullable_and_indexed(db_engine: Engine) -> None:
    """The migration adds nullable, uniquely indexed ``agents.session_id``."""
    columns = sa.inspect(db_engine).get_columns("agents")
    session_id_columns = [column for column in columns if column["name"] == "session_id"]
    assert len(session_id_columns) == 1, (
        f"Expected one agents.session_id column, got {len(session_id_columns)}. "
        f"If 0, the migration did not add the column."
    )
    session_id_column = session_id_columns[0]
    assert session_id_column["nullable"], "agents.session_id must allow template agents"

    indexes = sa.inspect(db_engine).get_indexes("agents")
    session_indexes = [index for index in indexes if index["name"] == "ix_agents_session_id"]
    assert len(session_indexes) == 1, (
        f"Expected ix_agents_session_id, got {[index['name'] for index in indexes]}"
    )
    # Unique enforces that two agent rows cannot claim the same
    # concrete session id while still allowing multiple NULL template
    # agents on supported databases.
    assert bool(session_indexes[0]["unique"]) is True


def test_agents_name_unique_index_is_template_scoped(db_engine: Engine) -> None:
    """Registered agent names stay unique while session copies may share them.

    The later ``add_owner_to_agents`` migration (o1a2b3c4d5e6) replaces the
    original ``ix_agents_template_name`` with ``ix_agents_builtin_name``
    (built-in name uniqueness, now scoped to ``owner IS NULL`` as well);
    the end-state schema here reflects that successor index. Built-in name
    uniqueness — what this test guards — is unchanged.
    """
    indexes = sa.inspect(db_engine).get_indexes("agents")
    template_indexes = [index for index in indexes if index["name"] == "ix_agents_builtin_name"]
    assert len(template_indexes) == 1, (
        f"Expected ix_agents_builtin_name, got {[index['name'] for index in indexes]}"
    )
    assert bool(template_indexes[0]["unique"]) is True

    with pytest.raises(sa.exc.IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO agents "
                    "(id, created_at, name, bundle_location, version) "
                    "VALUES (:id, :ts, :name, :loc, 1)",
                ),
                {
                    "id": "ag_template_one",
                    "ts": 1700000001,
                    "name": "template-name",
                    "loc": "ag_template_one/bundle",
                },
            )
            conn.execute(
                sa.text(
                    "INSERT INTO agents "
                    "(id, created_at, name, bundle_location, version) "
                    "VALUES (:id, :ts, :name, :loc, 1)",
                ),
                {
                    "id": "ag_template_two",
                    "ts": 1700000002,
                    "name": "template-name",
                    "loc": "ag_template_two/bundle",
                },
            )


def test_agents_session_id_fk_accepts_existing_session(db_engine: Engine) -> None:
    """The FK permits an agent to point at an existing conversation."""
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, root_conversation_id, kind) "
                "VALUES (:id, :ts, :ts, :id, 'default')",
            ),
            {"id": "conv_fk_target", "ts": 1700000000},
        )
        conn.execute(
            sa.text(
                "INSERT INTO agents "
                "(id, created_at, name, bundle_location, version, session_id) "
                "VALUES (:id, :ts, :name, :loc, 1, :session_id)",
            ),
            {
                "id": "ag_session_bound",
                "ts": 1700000001,
                "name": "session-bound-agent",
                "loc": "ag_session_bound/bundle",
                "session_id": "conv_fk_target",
            },
        )
        stored = conn.execute(
            sa.text("SELECT session_id FROM agents WHERE id = :id"),
            {"id": "ag_session_bound"},
        ).scalar_one()
    assert stored == "conv_fk_target"


def test_agents_session_id_fk_rejects_missing_session(db_engine: Engine) -> None:
    """The FK rejects references to nonexistent conversations."""
    with pytest.raises(sa.exc.IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO agents "
                    "(id, created_at, name, bundle_location, version, session_id) "
                    "VALUES (:id, :ts, :name, :loc, 1, :session_id)",
                ),
                {
                    "id": "ag_missing_session",
                    "ts": 1700000002,
                    "name": "missing-session-agent",
                    "loc": "ag_missing_session/bundle",
                    "session_id": "conv_missing",
                },
            )


def test_agents_session_id_unique_index_rejects_duplicate_session(
    db_engine: Engine,
) -> None:
    """Only one agent row can claim a concrete session id."""
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, root_conversation_id, kind) "
                "VALUES (:id, :ts, :ts, :id, 'default')",
            ),
            {"id": "conv_unique_target", "ts": 1700000000},
        )
        conn.execute(
            sa.text(
                "INSERT INTO agents "
                "(id, created_at, name, bundle_location, version, session_id) "
                "VALUES (:id, :ts, :name, :loc, 1, :session_id)",
            ),
            {
                "id": "ag_unique_one",
                "ts": 1700000001,
                "name": "unique-one",
                "loc": "ag_unique_one/bundle",
                "session_id": "conv_unique_target",
            },
        )

    with pytest.raises(sa.exc.IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO agents "
                    "(id, created_at, name, bundle_location, version, session_id) "
                    "VALUES (:id, :ts, :name, :loc, 1, :session_id)",
                ),
                {
                    "id": "ag_unique_two",
                    "ts": 1700000002,
                    "name": "unique-two",
                    "loc": "ag_unique_two/bundle",
                    "session_id": "conv_unique_target",
                },
            )


def test_agents_session_id_allows_duplicate_names_for_distinct_sessions(
    db_engine: Engine,
) -> None:
    """Two session-scoped agent copies can reuse the same spec name."""
    with db_engine.begin() as conn:
        for session_id in ["conv_name_one", "conv_name_two"]:
            conn.execute(
                sa.text(
                    "INSERT INTO conversations "
                    "(id, created_at, updated_at, root_conversation_id, kind) "
                    "VALUES (:id, :ts, :ts, :id, 'default')",
                ),
                {"id": session_id, "ts": 1700000000},
            )
        for agent_id, session_id in [
            ("ag_name_one", "conv_name_one"),
            ("ag_name_two", "conv_name_two"),
        ]:
            conn.execute(
                sa.text(
                    "INSERT INTO agents "
                    "(id, created_at, name, bundle_location, version, session_id) "
                    "VALUES (:id, :ts, :name, :loc, 1, :session_id)",
                ),
                {
                    "id": agent_id,
                    "ts": 1700000001,
                    "name": "shared-session-name",
                    "loc": f"{agent_id}/bundle",
                    "session_id": session_id,
                },
            )
        session_ids = list(
            conn.execute(
                sa.text(
                    "SELECT session_id FROM agents WHERE name = :name ORDER BY session_id",
                ),
                {"name": "shared-session-name"},
            ).scalars()
        )
    assert session_ids == ["conv_name_one", "conv_name_two"]


def test_agents_session_id_downgrade_round_trip(tmp_path: Path) -> None:
    """Downgrade deletes session-scoped rows and restores name uniqueness."""
    db_path = tmp_path / "downgrade.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO agents "
                "(id, created_at, name, bundle_location, version) "
                "VALUES (:id, :ts, :name, :loc, 1)",
            ),
            {
                "id": "ag_downgrade_template",
                "ts": 1700000001,
                "name": "downgrade-shared-name",
                "loc": "ag_downgrade_template/bundle",
            },
        )
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, root_conversation_id, kind) "
                "VALUES (:id, :ts, :ts, :id, 'default')",
            ),
            {"id": "conv_downgrade_session", "ts": 1700000002},
        )
        conn.execute(
            sa.text(
                "INSERT INTO agents "
                "(id, created_at, name, bundle_location, version, session_id) "
                "VALUES (:id, :ts, :name, :loc, 1, :session_id)",
            ),
            {
                "id": "ag_downgrade_session",
                "ts": 1700000003,
                "name": "downgrade-shared-name",
                "loc": "ag_downgrade_session/bundle",
                "session_id": "conv_downgrade_session",
            },
        )
        conn.execute(
            sa.text(
                "UPDATE conversations SET agent_id = :agent_id WHERE id = :conversation_id",
            ),
            {
                "agent_id": "ag_downgrade_session",
                "conversation_id": "conv_downgrade_session",
            },
        )

    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "b3d5e7f91a23")

    inspector = sa.inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("agents")}
    assert "session_id" not in columns
    index_names = {index["name"] for index in inspector.get_indexes("agents")}
    assert "ix_agents_session_id" not in index_names
    assert "ix_agents_template_name" not in index_names

    with engine.begin() as conn:
        rows = [
            tuple(row)
            for row in conn.execute(
                sa.text("SELECT id, name FROM agents ORDER BY id"),
            )
        ]
        session_conversation = conn.execute(
            sa.text("SELECT id FROM conversations WHERE id = :id"),
            {"id": "conv_downgrade_session"},
        ).first()
    assert rows == [("ag_downgrade_template", "downgrade-shared-name")]
    assert session_conversation is None

    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO agents "
                    "(id, created_at, name, bundle_location, version) "
                    "VALUES (:id, :ts, :name, :loc, 1)",
                ),
                {
                    "id": "ag_downgrade_duplicate",
                    "ts": 1700000001,
                    "name": "downgrade-shared-name",
                    "loc": "ag_downgrade_duplicate/bundle",
                },
            )

    engine.dispose()
    clear_engine_cache()
