"""Migration ``e5c2a9f7d3b1`` scopes agents to a project.

At head the new columns and index exist and are nullable, so rows written by
older binaries (which never set them) stay valid. The downgrade removes them
and keeps every agent and identity row.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine

_REVISION = "e5c2a9f7d3b1"
_PREVIOUS = "c9e5a7b3d1f2"
_AGENT_COLUMNS = {"project_id"}


def _columns(engine: sa.Engine, table: str) -> dict[str, bool]:
    return {c["name"]: c["nullable"] for c in sa.inspect(engine).get_columns(table)}


def _indexes(engine: sa.Engine) -> dict[str, list[str]]:
    return {i["name"]: i["column_names"] for i in sa.inspect(engine).get_indexes("agents")}


def test_columns_and_index_at_head(tmp_path: Path) -> None:
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    try:
        agents = _columns(engine, "agents")
        assert all(agents[name] for name in _AGENT_COLUMNS)
        assert _columns(engine, "project_identities")["project_agent_id"]
        assert _indexes(engine)["ix_agents_owner_project_created"] == [
            "workspace_id",
            "kind",
            "created_by",
            "project_id",
            "created_at",
            "id",
        ]
    finally:
        clear_engine_cache()


def test_downgrade_keeps_rows(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    cfg = _build_alembic_config(uri)
    # Earlier revisions need the server's own migration runner (autocommit blocks).
    get_or_create_engine(uri)
    clear_engine_cache()
    engine = sa.create_engine(uri)
    try:
        with engine.begin() as conn:
            # A project agent and its seat, as the new store would write them.
            conn.execute(
                sa.text(
                    "INSERT INTO agents (workspace_id, id, created_at, name, bundle_location,"
                    " version, kind, created_by, project_id) VALUES (0, :id, 1, 'reviewer', 'loc',"
                    " 1, 2, 'owner@example.com', :pid)"
                ),
                {"id": b"\x01" * 16, "pid": b"\x02" * 16},
            )
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, _PREVIOUS)
        assert not _AGENT_COLUMNS & set(_columns(engine, "agents"))
        assert "project_agent_id" not in _columns(engine, "project_identities")
        assert "ix_agents_owner_project_created" not in _indexes(engine)
        with engine.connect() as conn:
            # The row survives as an ordinary user agent owned by the project owner.
            row = conn.execute(sa.text("SELECT name, kind, created_by FROM agents")).one()
        assert tuple(row) == ("reviewer", 2, "owner@example.com")
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, _REVISION)
        assert set(_columns(engine, "agents")) >= _AGENT_COLUMNS
    finally:
        engine.dispose()
        clear_engine_cache()
