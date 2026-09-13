"""Tests for the agent_configuration split migration (bb2c3d4e5f6a)."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache


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


def test_agent_configuration_round_trip(tmp_path: Path) -> None:
    """Upgrade moves the agent binding + overrides; downgrade restores them."""
    db_path = tmp_path / "agent_configuration.db"
    uri = f"sqlite:///{db_path}"
    raw_engine = sa.create_engine(uri)

    # Schema as of the revision before the split: agent_id and the
    # overrides still live on conversations.
    _upgrade(uri, raw_engine, "aa1b2c3d4e5f")
    with raw_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversations"
                " (workspace_id, id, created_at, updated_at, root_conversation_id,"
                "  title, agent_id, reasoning_effort, model_override,"
                "  cost_control_mode_override, harness_override)"
                " VALUES (0, 'conv_1', 1, 1, 'conv_1', '', 'ag_1', 'high',"
                "         'claude-opus-4-7', 'off', 'pi'),"
                "        (0, 'conv_2', 2, 2, 'conv_2', 'two', NULL, NULL,"
                "         NULL, NULL, NULL)"
            )
        )

    # Upgrade: one agent_configuration row per conversation, columns dropped.
    _upgrade(uri, raw_engine, "bb2c3d4e5f6a")
    conv_cols = {c["name"] for c in sa.inspect(raw_engine).get_columns("conversations")}
    assert "agent_id" not in conv_cols
    assert "model_override" not in conv_cols
    with raw_engine.begin() as conn:
        rows = {
            r[0]: r[1:]
            for r in conn.execute(
                sa.text(
                    "SELECT conversation_id, agent_id, reasoning_effort, model_override,"
                    " cost_control_mode_override, harness_override"
                    " FROM agent_configuration ORDER BY conversation_id"
                )
            )
        }
    assert rows["conv_1"] == ("ag_1", "high", "claude-opus-4-7", "off", "pi")
    assert rows["conv_2"] == (None, None, None, None, None)

    # Downgrade: columns restored with their values, table gone.
    _downgrade(uri, raw_engine, "aa1b2c3d4e5f")
    conv_cols = {c["name"] for c in sa.inspect(raw_engine).get_columns("conversations")}
    assert "agent_id" in conv_cols
    assert "agent_configuration" not in sa.inspect(raw_engine).get_table_names()
    with raw_engine.begin() as conn:
        restored = conn.execute(
            sa.text(
                "SELECT agent_id, reasoning_effort, model_override FROM conversations"
                " WHERE id = 'conv_1'"
            )
        ).one()
    assert restored == ("ag_1", "high", "claude-opus-4-7")

    raw_engine.dispose()
    clear_engine_cache()
