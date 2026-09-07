"""Tests for the trusted personal quota-route backfill migration."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore


def _upgrade(uri: str, engine: sa.Engine, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, revision)


def _insert_project(
    engine: sa.Engine,
    *,
    project_id: str,
    name: str,
    config: bytes | None = None,
) -> None:
    """Insert a pre-head row without constructing an auto-migrating store."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO projects "
                "(workspace_id, id, name, user_id, created_at, updated_at, config) "
                "VALUES (0, :id, :name, NULL, 1, NULL, :config)"
            ),
            {"id": bytes.fromhex(project_id), "name": name, "config": config},
        )


def test_backfills_only_closed_personal_project_names(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'quota-routes.db'}"
    engine = sa.create_engine(uri)
    _upgrade(uri, engine, "ga1b2c3d4e5f")
    _insert_project(engine, project_id="1" * 32, name="chatgpt-playground")
    _insert_project(engine, project_id="2" * 32, name="unrelated-project")

    _upgrade(uri, engine, "head")

    store = SqlAlchemyProjectStore(uri)
    personal = store.get("1" * 32, user_id=None)
    unrelated = store.get("2" * 32, user_id=None)
    assert personal is not None
    assert personal.config["quota_route"] == "personal-llmq"
    assert unrelated is not None
    assert unrelated.config == {}
    engine.dispose()
    clear_engine_cache()


def test_refuses_conflicting_existing_route_metadata(tmp_path: Path) -> None:
    """A migration must not silently bless a contradictory route."""
    uri = f"sqlite:///{tmp_path / 'quota-route-conflict.db'}"
    engine = sa.create_engine(uri)
    _upgrade(uri, engine, "ga1b2c3d4e5f")
    _insert_project(
        engine,
        project_id="3" * 32,
        name="planar-jacobian",
        config=b'\x00\x00{"quota_route":"work-vertex"}',
    )

    with pytest.raises(RuntimeError, match="conflicting routing metadata"):
        _upgrade(uri, engine, "head")
    engine.dispose()
    clear_engine_cache()
