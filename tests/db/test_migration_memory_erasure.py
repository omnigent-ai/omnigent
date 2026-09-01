from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config

_REVISION = "mc2a3b4c5d6e"
_PREVIOUS = "mc1a2b3c4d5e"
_TABLES = {
    "memory_erasure_attempts",
    "memory_erasure_requests",
    "memory_erasure_tasks",
}


def test_memory_erasure_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'migration.db'}"
    config = _build_alembic_config(uri)

    command.upgrade(config, _REVISION)
    engine = sa.create_engine(uri)
    inspector = sa.inspect(engine)
    assert set(inspector.get_table_names()) >= _TABLES
    task_indexes = {item["name"] for item in inspector.get_indexes("memory_erasure_tasks")}
    assert task_indexes >= {
        "ix_memory_erasure_tasks_claim",
        "ix_memory_erasure_tasks_request",
    }

    command.downgrade(config, _PREVIOUS)
    assert _TABLES.isdisjoint(sa.inspect(engine).get_table_names())
    engine.dispose()
