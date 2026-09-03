from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config

_REVISION = "dc1a2b3c4d5e"
_PREVIOUS = "ga1b2c3d4e5f"
_TABLES = {"dpia_cases", "dpia_case_revisions"}


def test_dpia_case_store_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'migration.db'}"
    config = _build_alembic_config(uri)

    command.upgrade(config, _REVISION)
    engine = sa.create_engine(uri)
    inspector = sa.inspect(engine)
    assert set(inspector.get_table_names()) >= _TABLES
    assert {item["name"] for item in inspector.get_indexes("dpia_cases")} >= {
        "ix_dpia_cases_updated"
    }
    assert {item["name"] for item in inspector.get_indexes("dpia_case_revisions")} >= {
        "ix_dpia_case_revisions_case"
    }

    command.downgrade(config, _PREVIOUS)
    assert _TABLES.isdisjoint(sa.inspect(engine).get_table_names())
    engine.dispose()
