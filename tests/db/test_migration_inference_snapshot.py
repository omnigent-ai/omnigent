"""Migration coverage for saved inference configuration on Omnigent metadata."""

from __future__ import annotations

import io
import json
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from sqlalchemy.dialects import mysql

from omnigent.db.db_models import (
    ConversationBase,
    OmnigentBase,
    SqlConversation,
    SqlConversationMetadata,
)
from omnigent.db.utils import _build_alembic_config

_PREVIOUS_REVISION = "ar1b2c3d4e5f"
_REVISION = "hi1b2c3d4e5f"
_TABLE = "omnigent_conversation_metadata"


def _upgrade(uri: str, engine: sa.Engine, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def _downgrade(uri: str, engine: sa.Engine, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, revision)


def test_snapshot_model_uses_omnigent_text_without_changing_ap_schema() -> None:
    metadata_table = SqlConversationMetadata.__table__
    assert metadata_table.metadata is OmnigentBase.metadata
    snapshot = metadata_table.c.inference_snapshot
    assert snapshot.nullable
    assert isinstance(snapshot.type, sa.Text)
    assert snapshot.type.compile(dialect=mysql.dialect()) == "LONGTEXT"

    conversation_table = SqlConversation.__table__
    assert conversation_table.metadata is ConversationBase.metadata
    assert "inference_snapshot" not in conversation_table.c
    overrides = conversation_table.c.session_overrides
    assert isinstance(overrides.type, sa.String)
    assert not isinstance(overrides.type, sa.Text)
    assert overrides.type.length == 512


def test_mysql_snapshot_migration_uses_longtext() -> None:
    output = io.StringIO()
    config = _build_alembic_config("mysql://localhost/inference_snapshot_test")
    config.output_buffer = output
    command.upgrade(config, f"{_PREVIOUS_REVISION}:{_REVISION}", sql=True)
    assert (
        "ALTER TABLE omnigent_conversation_metadata ADD COLUMN inference_snapshot LONGTEXT"
        in output.getvalue()
    )


def test_snapshot_migration_preserves_rows_and_round_trips_large_json(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'inference-snapshot.db'}"
    engine = sa.create_engine(uri)
    old_id = bytes.fromhex("0123456789abcdef0123456789abcdef")
    new_id = bytes.fromhex("fedcba9876543210fedcba9876543210")
    overrides = '{"model_override":"old-model","reasoning_effort":"high"}'
    snapshot_json = json.dumps(
        {
            "provider": {"id": "gateway", "base_url": "https://gateway.example/v1"},
            "models": [
                {"id": f"model-{index}", "label": f"Gateway model {index}"}
                for index in range(2000)
            ],
        }
    )
    assert len(snapshot_json.encode()) > 65_535

    try:
        _upgrade(uri, engine, _PREVIOUS_REVISION)
        inspector = sa.inspect(engine)
        original_columns = inspector.get_columns(_TABLE)
        assert "inference_snapshot" not in {column["name"] for column in original_columns}
        original_indexes = inspector.get_indexes(_TABLE)
        original_checks = inspector.get_check_constraints(_TABLE)
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO conversations "
                    "(workspace_id, id, created_at, updated_at, root_conversation_id, "
                    "title, session_overrides) VALUES (0, :id, 1, 2, :id, 'Original', :overrides)"
                ),
                {"id": old_id, "overrides": overrides},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO omnigent_conversation_metadata "
                    "(workspace_id, id, kind, runner_id, host_id, workspace) "
                    "VALUES (0, :id, 1, 'runner-1', :id, '/workspace')"
                ),
                {"id": old_id},
            )
            original_row = connection.execute(sa.text(f"SELECT * FROM {_TABLE}")).one()

        _upgrade(uri, engine, _REVISION)
        inspector = sa.inspect(engine)
        columns = {column["name"]: column for column in inspector.get_columns(_TABLE)}
        assert columns["inference_snapshot"]["nullable"] is True
        assert isinstance(columns["inference_snapshot"]["type"], sa.Text)
        ap_columns = {column["name"]: column for column in inspector.get_columns("conversations")}
        assert "inference_snapshot" not in ap_columns
        assert ap_columns["session_overrides"]["type"].length == 512
        with engine.begin() as connection:
            assert connection.scalar(sa.text(f"SELECT inference_snapshot FROM {_TABLE}")) is None
            connection.execute(
                sa.text(
                    "UPDATE omnigent_conversation_metadata "
                    "SET inference_snapshot = :snapshot WHERE id = :id"
                ),
                {"id": old_id, "snapshot": snapshot_json},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO omnigent_conversation_metadata (workspace_id, id, kind) "
                    "VALUES (0, :id, 1)"
                ),
                {"id": new_id},
            )
            snapshots = {
                row.id: row.inference_snapshot
                for row in connection.execute(
                    sa.text(f"SELECT id, inference_snapshot FROM {_TABLE}")
                )
            }
            assert snapshots == {old_id: snapshot_json, new_id: None}

        _downgrade(uri, engine, _PREVIOUS_REVISION)
        inspector = sa.inspect(engine)
        assert [column["name"] for column in inspector.get_columns(_TABLE)] == [
            column["name"] for column in original_columns
        ]
        assert inspector.get_indexes(_TABLE) == original_indexes
        assert inspector.get_check_constraints(_TABLE) == original_checks
        with engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text(f"SELECT * FROM {_TABLE} WHERE id = :id"), {"id": old_id}
                ).one()
                == original_row
            )
            assert connection.scalar(sa.text(f"SELECT COUNT(*) FROM {_TABLE}")) == 2
            assert connection.execute(
                sa.text("SELECT title, updated_at, session_overrides FROM conversations")
            ).one() == ("Original", 2, overrides)

        _upgrade(uri, engine, _REVISION)
        with engine.connect() as connection:
            assert list(
                connection.scalars(sa.text(f"SELECT inference_snapshot FROM {_TABLE}"))
            ) == [None, None]
    finally:
        engine.dispose()
