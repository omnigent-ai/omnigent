"""Migration ``a3c7e1f5b9d2`` adds a separate table for role KPI revisions."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine


def _tables(engine: sa.Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def test_role_kpi_table_round_trips(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    get_or_create_engine(uri)
    clear_engine_cache()
    cfg = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    try:
        assert "project_role_kpi_revisions" in _tables(engine)
        pk = sa.inspect(engine).get_pk_constraint("project_role_kpi_revisions")
        assert pk["constrained_columns"] == [
            "workspace_id",
            "project_id",
            "identity_id",
            "revision",
        ]
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "f2b6d9a4c8e1")
        assert "project_role_kpi_revisions" not in _tables(engine)
        assert "project_kpi_revisions" in _tables(engine)
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "a3c7e1f5b9d2")
        assert "project_role_kpi_revisions" in _tables(engine)
    finally:
        engine.dispose()
        clear_engine_cache()
