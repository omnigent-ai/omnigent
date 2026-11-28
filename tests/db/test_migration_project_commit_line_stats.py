"""Migration ``e3b7d1f5a9c2`` adds per-commit line counts for Changes landed."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine


def test_commit_line_stats_table_round_trips(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    get_or_create_engine(uri)
    clear_engine_cache()
    cfg = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    try:
        assert "project_commit_line_stats" in sa.inspect(engine).get_table_names()
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "b8d4f2a6c1e3")
        assert "project_commit_line_stats" not in sa.inspect(engine).get_table_names()
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "e3b7d1f5a9c2")
        assert "project_commit_line_stats" in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()
        clear_engine_cache()
