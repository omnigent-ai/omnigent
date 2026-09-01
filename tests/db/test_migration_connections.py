"""Tests for the connections migration (ga1b2c3d4e5f).

The single-head test is the guard: a stacked PR whose migration chains off a
revision that isn't a real ancestor leaves the tree with two heads, and
``alembic upgrade head`` — which runs on every server boot and every DB-touching
test — then raises. Asserting a single head catches that before CI does.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config, clear_engine_cache


def _upgrade(uri: str, engine: sa.Engine, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, revision)


def _downgrade(uri: str, engine: sa.Engine, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, revision)


def test_single_alembic_head() -> None:
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))
    heads = script.get_heads()
    assert heads == ["ga1b2c3d4e5f"], f"expected a single head, got {heads!r}"


def test_upgrade_creates_table_downgrade_drops_it(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'connections.db'}"
    engine = sa.create_engine(uri)

    # Upgrading to head exercises the full chain onto our migration — this is
    # exactly the call that raises on multiple heads.
    _upgrade(uri, engine, "head")
    inspector = sa.inspect(engine)
    assert "connections" in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("connections")}
    assert {
        "workspace_id",
        "user_id",
        "provider",
        "account_id",
        "secret_enc",
        "metadata_json",
        "created_at",
        "updated_at",
    } <= columns
    pk = set(inspector.get_pk_constraint("connections")["constrained_columns"])
    assert pk == {"workspace_id", "user_id", "provider", "account_id"}

    _downgrade(uri, engine, "za2b3c4d5e6f")
    assert "connections" not in sa.inspect(engine).get_table_names()

    engine.dispose()
    clear_engine_cache()
