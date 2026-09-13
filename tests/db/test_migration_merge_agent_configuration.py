"""Tests for the agent_configuration merge migration (b7e4d2c9a1f3).

Reverses the earlier split: the agent_configuration companion table folds back
onto conversations as an indexed ``agent_id`` column plus a ``session_overrides``
JSON blob.
"""

from __future__ import annotations

import json
from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.db_models import uuid_to_bytes
from omnigent.db.utils import _build_alembic_config, clear_engine_cache

# Revision before the merge (agent_configuration still exists) and the merge itself.
_PRE_MERGE = "a2b7c3d8e4f9"
_MERGE = "b7e4d2c9a1f3"

_CONV_BOUND = "a9930027fd3e2e979e65844f7af7bf88"
_CONV_DEFAULT = "b0041138ae4f3fa8af76955a8b086c99"
_AGENT = "112c4ebea353b873df12de9d02f539ab"


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


def _seed(engine: sa.Engine) -> None:
    """One conversation with an agent + overrides, one on all defaults."""
    with engine.begin() as conn:
        for cid in (_CONV_BOUND, _CONV_DEFAULT):
            conn.execute(
                sa.text(
                    "INSERT INTO conversations (workspace_id, id, created_at, updated_at,"
                    " title, root_conversation_id) VALUES (0, :id, 1, 1, :t, :id)"
                ),
                {"id": uuid_to_bytes(cid), "t": f"conv {cid[:6]}"},
            )
        conn.execute(
            sa.text(
                "INSERT INTO agent_configuration (workspace_id, conversation_id, agent_id,"
                " reasoning_effort, model_override, cost_control_mode_override, harness_override)"
                " VALUES (0, :cid, :aid, 'high', 'claude-opus-4-8', NULL, NULL)"
            ),
            {"cid": uuid_to_bytes(_CONV_BOUND), "aid": uuid_to_bytes(_AGENT)},
        )
        conn.execute(
            sa.text(
                "INSERT INTO agent_configuration (workspace_id, conversation_id, agent_id)"
                " VALUES (0, :cid, NULL)"
            ),
            {"cid": uuid_to_bytes(_CONV_DEFAULT)},
        )


def test_merge_upgrade_folds_binding_and_overrides(tmp_path: Path) -> None:
    """Upgrade drops the table, copies agent_id, and packs overrides into a blob."""
    uri = f"sqlite:///{tmp_path / 'merge.db'}"
    engine = sa.create_engine(uri)
    try:
        _upgrade(uri, engine, _PRE_MERGE)
        _seed(engine)
        _upgrade(uri, engine, _MERGE)

        insp = sa.inspect(engine)
        assert "agent_configuration" not in set(insp.get_table_names())
        cols = {c["name"] for c in insp.get_columns("conversations")}
        assert {"agent_id", "session_overrides"} <= cols
        conv_indexes = {i["name"] for i in insp.get_indexes("conversations")}
        assert "ix_conversations_agent_id" in conv_indexes

        with engine.begin() as conn:
            aid, blob = conn.execute(
                sa.text("SELECT agent_id, session_overrides FROM conversations WHERE id = :id"),
                {"id": uuid_to_bytes(_CONV_BOUND)},
            ).one()
            assert bytes(aid) == uuid_to_bytes(_AGENT)
            # Only the set overrides are stored; unset ones are omitted.
            assert json.loads(blob) == {
                "reasoning_effort": "high",
                "model_override": "claude-opus-4-8",
            }

            aid2, blob2 = conn.execute(
                sa.text("SELECT agent_id, session_overrides FROM conversations WHERE id = :id"),
                {"id": uuid_to_bytes(_CONV_DEFAULT)},
            ).one()
            # All-default session keeps both NULL.
            assert aid2 is None and blob2 is None
    finally:
        clear_engine_cache()


def test_merge_copies_varchar_stored_agent_id(tmp_path: Path) -> None:
    """A hex-string agent_id (the Postgres/MySQL VARCHAR form) copies as bytes.

    The split migration declared agent_configuration.agent_id as VARCHAR, so on
    a non-SQLite fork the source holds a hex string. The migration must still
    land the correct 16 raw bytes in the binary conversations.agent_id column —
    this is the coercion the hardened Python copy handles.
    """
    uri = f"sqlite:///{tmp_path / 'merge_varchar.db'}"
    engine = sa.create_engine(uri)
    try:
        _upgrade(uri, engine, _PRE_MERGE)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO conversations (workspace_id, id, created_at, updated_at,"
                    " title, root_conversation_id) VALUES (0, :id, 1, 1, 't', :id)"
                ),
                {"id": uuid_to_bytes(_CONV_BOUND)},
            )
            # agent_id + conversation_id inserted as plain hex strings, not bytes.
            conn.execute(
                sa.text(
                    "INSERT INTO agent_configuration (workspace_id, conversation_id, agent_id)"
                    " VALUES (0, :cid, :aid)"
                ),
                {"cid": _CONV_BOUND, "aid": _AGENT},
            )
        _upgrade(uri, engine, _MERGE)

        with engine.begin() as conn:
            aid = conn.execute(
                sa.text("SELECT agent_id FROM conversations WHERE id = :id"),
                {"id": uuid_to_bytes(_CONV_BOUND)},
            ).scalar_one()
            assert bytes(aid) == uuid_to_bytes(_AGENT)
    finally:
        clear_engine_cache()


def test_merge_round_trip_restores_agent_configuration(tmp_path: Path) -> None:
    """Downgrade recreates the table and fans the blob back out to columns."""
    uri = f"sqlite:///{tmp_path / 'merge_rt.db'}"
    engine = sa.create_engine(uri)
    try:
        _upgrade(uri, engine, _PRE_MERGE)
        _seed(engine)
        _upgrade(uri, engine, _MERGE)
        _downgrade(uri, engine, _PRE_MERGE)

        insp = sa.inspect(engine)
        assert "agent_configuration" in set(insp.get_table_names())
        cols = {c["name"] for c in insp.get_columns("conversations")}
        assert "agent_id" not in cols and "session_overrides" not in cols

        with engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT agent_id, reasoning_effort, model_override,"
                    " cost_control_mode_override, harness_override"
                    " FROM agent_configuration WHERE conversation_id = :cid"
                ),
                {"cid": uuid_to_bytes(_CONV_BOUND)},
            ).one()
            assert bytes(row[0]) == uuid_to_bytes(_AGENT)
            assert row[1] == "high"
            assert row[2] == "claude-opus-4-8"
            assert row[3] is None and row[4] is None

            row2 = conn.execute(
                sa.text(
                    "SELECT agent_id, reasoning_effort FROM agent_configuration"
                    " WHERE conversation_id = :cid"
                ),
                {"cid": uuid_to_bytes(_CONV_DEFAULT)},
            ).one()
            assert row2[0] is None and row2[1] is None
    finally:
        clear_engine_cache()
