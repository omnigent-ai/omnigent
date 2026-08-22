"""Migration coverage for the durable turn-operation journal."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect

from omnigent.db.utils import _build_alembic_config


def test_turn_operations_upgrade_and_downgrade(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path}/turn-operations-migration.db"
    config = _build_alembic_config(uri)

    command.upgrade(config, "head")
    engine = create_engine(uri)
    inspector = inspect(engine)
    assert "turn_operations" in inspector.get_table_names()
    assert {column["name"] for column in inspector.get_columns("turn_operations")} == {
        "workspace_id",
        "id",
        "conversation_id",
        "principal_hash",
        "idempotency_key_hash",
        "request_hash",
        "request_json",
        "state",
        "item_id",
        "created_at",
        "updated_at",
        "terminal_at",
        "error_code",
        "error",
    }
    unique_constraints = {
        constraint["name"]: tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("turn_operations")
    }
    assert unique_constraints["uq_turn_operations_replay_key"] == (
        "workspace_id",
        "conversation_id",
        "principal_hash",
        "idempotency_key_hash",
    )
    indexes = {index["name"] for index in inspector.get_indexes("turn_operations")}
    assert "ix_turn_operations_conversation_state" in indexes

    engine.dispose()
    command.downgrade(config, "za2b3c4d5e6f")
    downgraded = create_engine(uri)
    assert "turn_operations" not in inspect(downgraded).get_table_names()
    downgraded.dispose()
