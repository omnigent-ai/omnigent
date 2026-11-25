"""Migration ``c5e1a7d3f9b2`` adds derived merged-PR rows and their refresh markers."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine

_TABLES = {"project_merged_prs", "project_merged_pr_refreshes"}


def test_merged_pr_tables_round_trip(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    get_or_create_engine(uri)
    clear_engine_cache()
    cfg = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    try:
        inspector = sa.inspect(engine)
        assert _TABLES <= set(inspector.get_table_names())
        assert [index["name"] for index in inspector.get_indexes("project_merged_prs")] == [
            "ix_project_merged_prs_merged_at"
        ]
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "d2f6a8c4e1b9")
        assert not _TABLES & set(sa.inspect(engine).get_table_names())
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "c5e1a7d3f9b2")
        assert _TABLES <= set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()
        clear_engine_cache()
