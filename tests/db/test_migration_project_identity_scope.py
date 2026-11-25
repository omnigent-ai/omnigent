"""Migration ``f2b6d9a4c8e1`` adds a nullable scope to project identities."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine


def _columns(engine: sa.Engine) -> dict[str, bool]:
    return {c["name"]: c["nullable"] for c in sa.inspect(engine).get_columns("project_identities")}


def test_scope_column_round_trips(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    get_or_create_engine(uri)
    clear_engine_cache()
    cfg = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    try:
        assert _columns(engine)["scope"] is True
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "b4d8f2a6c0e3")
        assert "scope" not in _columns(engine)
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "f2b6d9a4c8e1")
        assert "scope" in _columns(engine)
    finally:
        engine.dispose()
        clear_engine_cache()
