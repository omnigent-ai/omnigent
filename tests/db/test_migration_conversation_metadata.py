"""Tests for the conversation-metadata split migration (aa1b2c3d4e5f)."""

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


def test_metadata_split_round_trip_with_host_bound_row(tmp_path: Path) -> None:
    """Round-trip a HOST-BOUND conversation (host_id + workspace set).

    The downgrade re-creates ``ck_conversations_workspace_required_for_host``
    (host_id IS NULL OR workspace IS NOT NULL) before restoring data
    column-by-column, so it must restore ``workspace`` before ``host_id`` —
    the reverse order fires the constraint on every host-bound row while its
    workspace is still NULL. An empty-DB round trip cannot catch this; a
    seeded host-bound row can.
    """
    db_path = tmp_path / "metadata_split.db"
    uri = f"sqlite:///{db_path}"
    raw_engine = sa.create_engine(uri)

    # Schema as of the revision before the split.
    _upgrade(uri, raw_engine, "z5a2b3c4d5e6")
    with raw_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversations"
                " (workspace_id, id, created_at, updated_at, root_conversation_id,"
                "  title, kind, archived, host_id, workspace, git_branch, runner_id)"
                " VALUES (0, 'conv_hostbound', 1, 1, 'conv_hostbound', '', 1, 0,"
                "         'host_1', '/home/user/proj', 'feature/x', 'runner_1'),"
                "        (0, 'conv_plain', 2, 2, 'conv_plain', 'two', 2, 1,"
                "         NULL, NULL, NULL, NULL)"
            )
        )

    # Upgrade: operational columns move to omnigent_conversation_metadata.
    _upgrade(uri, raw_engine, "aa1b2c3d4e5f")
    with raw_engine.begin() as conn:
        rows = {
            r[0]: r[1:]
            for r in conn.execute(
                sa.text(
                    "SELECT id, kind, archived, host_id, workspace, git_branch, runner_id"
                    " FROM omnigent_conversation_metadata ORDER BY id"
                )
            )
        }
    assert rows["conv_hostbound"] == (1, 0, "host_1", "/home/user/proj", "feature/x", "runner_1")
    assert rows["conv_plain"] == (2, 1, None, None, None, None)

    # Downgrade past the split: must not trip the re-created check constraint
    # on the host-bound row, and must restore every value.
    _downgrade(uri, raw_engine, "z5a2b3c4d5e6")
    assert "omnigent_conversation_metadata" not in sa.inspect(raw_engine).get_table_names()
    with raw_engine.begin() as conn:
        restored = conn.execute(
            sa.text(
                "SELECT kind, archived, host_id, workspace, git_branch, runner_id"
                " FROM conversations WHERE id = 'conv_hostbound'"
            )
        ).one()
    assert restored == (1, 0, "host_1", "/home/user/proj", "feature/x", "runner_1")

    raw_engine.dispose()
    clear_engine_cache()
