"""Tests for the managed sandbox logical-deletion migration."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.db_models import SqlHost
from omnigent.db.utils import _build_alembic_config, clear_engine_cache


def _migrate(uri: str, engine: sa.Engine, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def _downgrade(uri: str, engine: sa.Engine, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, revision)


def test_hosts_deleted_at_model_uses_big_integer() -> None:
    assert isinstance(SqlHost.__table__.c.deleted_at.type, sa.BigInteger)
    assert {
        "ix_hosts_sandbox_scan",
        "ix_hosts_terminating_sandbox_scan",
    }.issubset({index.name for index in SqlHost.__table__.indexes})


def test_upgrade_adds_hosts_deleted_at_and_downgrade_removes_it(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'managed-sandbox-deleted-at.db'}"
    engine = sa.create_engine(uri)

    _migrate(uri, engine, "gb1b2c3d4e5f")
    assert "deleted_at" not in {
        column["name"] for column in sa.inspect(engine).get_columns("hosts")
    }

    _migrate(uri, engine, "gc1b2c3d4e5f")
    columns = {column["name"]: column for column in sa.inspect(engine).get_columns("hosts")}
    assert columns["deleted_at"]["nullable"] is True
    assert isinstance(columns["deleted_at"]["type"], sa.Integer)

    _downgrade(uri, engine, "gb1b2c3d4e5f")
    assert "deleted_at" not in {
        column["name"] for column in sa.inspect(engine).get_columns("hosts")
    }

    engine.dispose()
    clear_engine_cache()


def test_upgrade_widens_hosts_deleted_at_and_preserves_data(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'managed-sandbox-deleted-at-bigint.db'}"
    engine = sa.create_engine(uri)

    _migrate(uri, engine, "ge1b2c3d4e5f")
    columns = {column["name"]: column for column in sa.inspect(engine).get_columns("hosts")}
    assert isinstance(columns["deleted_at"]["type"], sa.Integer)
    assert not isinstance(columns["deleted_at"]["type"], sa.BigInteger)

    deleted_at = 2_147_483_647
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO hosts (
                    workspace_id, host_id, user_id, name, status,
                    created_at, updated_at, deleted_at
                ) VALUES (
                    :workspace_id, :host_id, :user_id, :name, :status,
                    :created_at, :updated_at, :deleted_at
                )
                """
            ),
            {
                "workspace_id": 0,
                "host_id": bytes.fromhex("0123456789abcdef0123456789abcdef"),
                "user_id": "alice@example.com",
                "name": "managed-bigint",
                "status": 2,
                "created_at": 1,
                "updated_at": 1,
                "deleted_at": deleted_at,
            },
        )

    _migrate(uri, engine, "gg1b2c3d4e5f")
    columns = {column["name"]: column for column in sa.inspect(engine).get_columns("hosts")}
    assert isinstance(columns["deleted_at"]["type"], sa.BigInteger)
    assert {
        "ix_hosts_sandbox_scan",
        "ix_hosts_terminating_sandbox_scan",
    }.issubset({index["name"] for index in sa.inspect(engine).get_indexes("hosts")})
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT deleted_at FROM hosts")) == deleted_at

    _downgrade(uri, engine, "ge1b2c3d4e5f")
    columns = {column["name"]: column for column in sa.inspect(engine).get_columns("hosts")}
    assert isinstance(columns["deleted_at"]["type"], sa.Integer)
    assert not isinstance(columns["deleted_at"]["type"], sa.BigInteger)
    assert {
        "ix_hosts_sandbox_scan",
        "ix_hosts_terminating_sandbox_scan",
    }.isdisjoint({index["name"] for index in sa.inspect(engine).get_indexes("hosts")})
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT deleted_at FROM hosts")) == deleted_at

    engine.dispose()
    clear_engine_cache()


def test_upgrade_tolerates_current_schema_stamped_at_crdb_baseline(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'managed-sandbox-current-schema.db'}"
    engine = sa.create_engine(uri)

    _migrate(uri, engine, "gg1b2c3d4e5f")
    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE alembic_version SET version_num = 'gf1b2c3d4e5f'"))

    _migrate(uri, engine, "gg1b2c3d4e5f")

    columns = {column["name"]: column for column in sa.inspect(engine).get_columns("hosts")}
    assert isinstance(columns["deleted_at"]["type"], sa.BigInteger)
    assert {
        "ix_hosts_sandbox_scan",
        "ix_hosts_terminating_sandbox_scan",
    }.issubset({index["name"] for index in sa.inspect(engine).get_indexes("hosts")})

    engine.dispose()
    clear_engine_cache()
