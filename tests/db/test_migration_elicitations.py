"""Tests for the elicitations-table migration (zb2b3c4d5e6f)."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config


def _upgrade(uri: str, engine: sa.Engine, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, revision)


def _downgrade(uri: str, engine: sa.Engine, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, revision)


def test_migration_graph_has_a_single_head(tmp_path: Path) -> None:
    """Adding the elicitations migration must not fork the revision graph.

    A second head makes ``alembic upgrade head`` fail with "multiple heads",
    which silently skips creating the table on a real deploy — disabling
    restart survival entirely while every unit test still passes.
    """
    from alembic.script import ScriptDirectory

    uri = f"sqlite:///{tmp_path / 'heads.db'}"
    script = ScriptDirectory.from_config(_build_alembic_config(uri))

    assert len(script.get_heads()) == 1


def test_upgrade_creates_the_table_and_its_index(tmp_path: Path) -> None:
    """The table and the one read path's index both land."""
    uri = f"sqlite:///{tmp_path / 'elicitations.db'}"
    engine = sa.create_engine(uri)

    _upgrade(uri, engine, "zb2b3c4d5e6f")

    inspector = sa.inspect(engine)
    assert "elicitations" in inspector.get_table_names()
    columns = {col["name"] for col in inspector.get_columns("elicitations")}
    assert columns == {"workspace_id", "id", "conversation_id", "created_at", "event"}
    indexes = {index["name"] for index in inspector.get_indexes("elicitations")}
    assert "ix_elicitations_conversation_id" in indexes


def test_primary_key_is_workspace_scoped(tmp_path: Path) -> None:
    """Two tenants may hold the same elicitation id without colliding.

    Every other table partitions on ``workspace_id`` as the leading primary-key
    member; a prompt id that is only unique per tenant must not be global here.
    """
    uri = f"sqlite:///{tmp_path / 'elicitations_pk.db'}"
    engine = sa.create_engine(uri)
    _upgrade(uri, engine, "zb2b3c4d5e6f")

    pk = sa.inspect(engine).get_pk_constraint("elicitations")

    assert pk["constrained_columns"] == ["workspace_id", "id"]


def test_downgrade_drops_the_table(tmp_path: Path) -> None:
    """The migration is reversible: it only adds, so undoing it only drops."""
    uri = f"sqlite:///{tmp_path / 'elicitations_down.db'}"
    engine = sa.create_engine(uri)
    _upgrade(uri, engine, "zb2b3c4d5e6f")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO elicitations"
                " (workspace_id, id, conversation_id, created_at, event)"
                " VALUES (0, 'elicit_x', :conv, 1, :event)"
            ),
            {"conv": bytes(16), "event": b"{}"},
        )

    _downgrade(uri, engine, "ga1b2c3d4e5f")

    assert "elicitations" not in sa.inspect(engine).get_table_names()
