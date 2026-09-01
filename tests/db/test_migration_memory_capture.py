from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config

_REVISION = "mc1a2b3c4d5e"
_PREVIOUS = "ga1b2c3d4e5f"
_TABLES = {
    "memory_capture_attempts",
    "memory_capture_intents",
    "memory_capture_jobs",
    "memory_capture_reviews",
}


def test_memory_capture_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'migration.db'}"
    config = _build_alembic_config(uri)

    command.upgrade(config, _REVISION)
    engine = sa.create_engine(uri)
    inspector = sa.inspect(engine)
    assert set(inspector.get_table_names()) >= _TABLES
    job_indexes = {item["name"] for item in inspector.get_indexes("memory_capture_jobs")}
    assert job_indexes >= {
        "ix_memory_capture_jobs_claim",
        "ix_memory_capture_jobs_conversation",
    }

    command.downgrade(config, _PREVIOUS)
    assert _TABLES.isdisjoint(sa.inspect(engine).get_table_names())
    engine.dispose()
