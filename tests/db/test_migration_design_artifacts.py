"""Tests for the design_artifacts migration (nn1a2b3c4d5e)."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache


def _run(uri: str, engine: sa.Engine, fn, revision: str) -> None:  # type: ignore[no-untyped-def]
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        fn(config, revision)


def test_upgrade_creates_table_downgrade_drops_it(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'design.db'}"
    engine = sa.create_engine(uri)

    _run(uri, engine, command.upgrade, "head")
    inspector = sa.inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("design_artifacts")}
    assert columns == {"workspace_id", "session_id", "path", "kind", "updated_at", "deleted"}
    pk = inspector.get_pk_constraint("design_artifacts")["constrained_columns"]
    assert pk == ["workspace_id", "session_id", "path"]
    indexes = {i["name"]: i["column_names"] for i in inspector.get_indexes("design_artifacts")}
    assert indexes == {"ix_design_artifacts_session": ["workspace_id", "session_id"]}
    assert inspector.get_foreign_keys("design_artifacts") == []

    _run(uri, engine, command.downgrade, "mm1a2b3c4d5e")
    assert "design_artifacts" not in sa.inspect(engine).get_table_names()

    engine.dispose()
    clear_engine_cache()
