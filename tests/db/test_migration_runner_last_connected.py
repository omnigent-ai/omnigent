"""Tests for the runner_last_connected additive migration (nn1a2b3c4d5e)."""

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


def test_add_runner_last_connected_preserves_existing_rows(tmp_path: Path) -> None:
    """A rolling upgrade leaves pre-existing metadata rows readable and live.

    During the deploy, replicas on the previous build keep writing only
    ``runner_last_seen``. The additive column must land NULL on every
    pre-existing row so the newest-stamp liveness check treats a row an old
    replica wrote as live rather than as a departed runner, and the downgrade
    must drop the column again without disturbing the preserved stamp.
    """
    db_path = tmp_path / "runner_last_connected.db"
    uri = f"sqlite:///{db_path}"
    engine = sa.create_engine(uri)
    try:
        # Schema just before the additive column, with a bound runner whose only
        # liveness stamp is runner_last_seen, as a previous-build replica writes.
        _upgrade(uri, engine, "mm1a2b3c4d5e")
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO omnigent_conversation_metadata"
                    " (workspace_id, id, kind, runner_id, runner_last_seen)"
                    " VALUES (0, 'conv_live', 1, 'runner_1', 1700000000)"
                )
            )

        _upgrade(uri, engine, "nn1a2b3c4d5e")
        with engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT runner_last_seen, runner_last_connected"
                    " FROM omnigent_conversation_metadata WHERE id = 'conv_live'"
                )
            ).one()
        assert row == (1700000000, None)

        _downgrade(uri, engine, "mm1a2b3c4d5e")
        columns = {
            c["name"] for c in sa.inspect(engine).get_columns("omnigent_conversation_metadata")
        }
        assert "runner_last_connected" not in columns
        with engine.begin() as conn:
            seen = conn.execute(
                sa.text(
                    "SELECT runner_last_seen FROM omnigent_conversation_metadata"
                    " WHERE id = 'conv_live'"
                )
            ).scalar_one()
        assert seen == 1700000000
    finally:
        engine.dispose()
        clear_engine_cache()
