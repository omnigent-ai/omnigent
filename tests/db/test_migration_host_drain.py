"""Tests for the host launch-fence (drain) migration."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_PARENT = "d0a1f879a01d"
_DRAIN_COLUMNS = {
    "drain_generation",
    "drain_started_at",
    "drain_requested_by",
    "drain_acked_generation",
    "drain_acked_at",
    "drain_supported",
}


def _run(uri: str, engine: sa.Engine, action: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        getattr(command, action)(config, revision)


def _host_columns(engine: sa.Engine) -> dict[str, dict[str, object]]:
    return {column["name"]: column for column in sa.inspect(engine).get_columns("hosts")}


def test_drain_migration_is_the_single_head() -> None:
    script = ScriptDirectory.from_config(_build_alembic_config("sqlite://"))
    assert script.get_heads() == ["mn1a2b3c4d5e"]
    revision = script.get_revision("mn1a2b3c4d5e")
    assert revision is not None and revision.down_revision == _PARENT


def test_upgrade_adds_drain_columns_with_safe_defaults(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'host-drain.db'}"
    engine = sa.create_engine(uri)

    _run(uri, engine, "upgrade", _PARENT)
    assert _DRAIN_COLUMNS.isdisjoint(_host_columns(engine))
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO hosts (workspace_id, host_id, user_id, name, status, "
                "created_at, updated_at) VALUES (0, :host_id, 'alice', 'laptop', 1, 1, 1)"
            ),
            {"host_id": bytes.fromhex("0123456789abcdef0123456789abcdef")},
        )

    _run(uri, engine, "upgrade", "mn1a2b3c4d5e")
    columns = _host_columns(engine)
    assert set(columns) >= _DRAIN_COLUMNS
    assert columns["drain_generation"]["nullable"] is False
    with engine.connect() as connection:
        row = connection.execute(
            sa.text("SELECT drain_generation, drain_started_at, drain_supported FROM hosts")
        ).one()
    # An existing host starts undrained, and its drain support is unknown.
    assert tuple(row) == (0, None, None)

    _run(uri, engine, "downgrade", _PARENT)
    assert _DRAIN_COLUMNS.isdisjoint(_host_columns(engine))

    engine.dispose()
    clear_engine_cache()


def test_upgrade_tolerates_schema_built_from_current_metadata(tmp_path: Path) -> None:
    """A CRDB-style baseline already has the columns; re-running is a no-op."""
    uri = f"sqlite:///{tmp_path / 'host-drain-current.db'}"
    engine = sa.create_engine(uri)
    _run(uri, engine, "upgrade", "mn1a2b3c4d5e")
    with engine.begin() as connection:
        connection.execute(sa.text(f"UPDATE alembic_version SET version_num = '{_PARENT}'"))

    _run(uri, engine, "upgrade", "mn1a2b3c4d5e")
    assert set(_host_columns(engine)) >= _DRAIN_COLUMNS

    engine.dispose()
    clear_engine_cache()
