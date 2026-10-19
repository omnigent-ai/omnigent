"""Tests for the ``omnigent_conversation_metadata.live_status_at`` migration.

The column records when the writing replica observed ``live_status``. It is
additive and nullable, so rows written before it read NULL, and the revision
is safe to replay or roll back.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config

# Revision ids bounding the migration under test.
_PRIOR_HEAD = "mm1a2b3c4d5e"
_THIS_REVISION = "nn1a2b3c4d5e"
_TABLE = "omnigent_conversation_metadata"


def _migrate(uri: str, engine: sa.Engine, revision: str, *, downgrade: bool = False) -> None:
    """
    Run Alembic to a target revision on a raw engine.

    :param uri: SQLAlchemy database URI, e.g. ``"sqlite:///tmp/test.db"``.
    :param engine: SQLAlchemy engine under migration.
    :param revision: Alembic target revision, e.g. ``"nn1a2b3c4d5e"``.
    :param downgrade: Downgrade to *revision* instead of upgrading.
    """
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        (command.downgrade if downgrade else command.upgrade)(config, revision)


def _columns(engine: sa.Engine) -> dict[str, dict[str, object]]:
    """Return the metadata table's columns by name."""
    return {str(c["name"]): dict(c) for c in sa.inspect(engine).get_columns(_TABLE)}


def test_live_status_at_is_added_nullable_and_existing_rows_read_null(tmp_path: Path) -> None:
    """Upgrade adds a nullable BigInteger column that reads NULL on older rows."""
    uri = f"sqlite:///{tmp_path / 'live-status-at.db'}"
    engine = sa.create_engine(uri)
    try:
        _migrate(uri, engine, _PRIOR_HEAD)
        assert "live_status_at" not in _columns(engine)
        with engine.begin() as conn:
            conn.execute(
                sa.text(f"INSERT INTO {_TABLE} (id, live_status) VALUES (:id, 1)"),
                {"id": "3f0c3d9a8a1b4c5d9e7f6a5b4c3d2e1f"},
            )

        _migrate(uri, engine, _THIS_REVISION)

        column = _columns(engine)["live_status_at"]
        assert column["nullable"]
        assert isinstance(column["type"], sa.BigInteger)
        with engine.connect() as conn:
            stamps = conn.execute(sa.text(f"SELECT live_status, live_status_at FROM {_TABLE}"))
            assert [tuple(row) for row in stamps] == [(1, None)]
    finally:
        engine.dispose()


def _stamp(uri: str, engine: sa.Engine, revision: str) -> None:
    """Record *revision* as applied without running it, as an interrupted run can."""
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.stamp(config, revision)


def test_live_status_at_downgrades_and_replays(tmp_path: Path) -> None:
    """Downgrade drops the column, and replaying either step is a no-op."""
    uri = f"sqlite:///{tmp_path / 'live-status-at-replay.db'}"
    engine = sa.create_engine(uri)
    try:
        _migrate(uri, engine, _THIS_REVISION)
        # The column exists but the version table says it doesn't yet.
        _stamp(uri, engine, _PRIOR_HEAD)
        _migrate(uri, engine, _THIS_REVISION)
        assert "live_status_at" in _columns(engine)

        _migrate(uri, engine, _PRIOR_HEAD, downgrade=True)
        assert "live_status_at" not in _columns(engine)
        _stamp(uri, engine, _THIS_REVISION)
        _migrate(uri, engine, _PRIOR_HEAD, downgrade=True)
        assert "live_status_at" not in _columns(engine)
    finally:
        engine.dispose()
