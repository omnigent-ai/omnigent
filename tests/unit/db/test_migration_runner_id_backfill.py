"""Tests for the PR2 runner_id backfill migration."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.migrations.versions.e9f2a7c4d1b8_backfill_unbound_runner_id import (
    OFFLINE_MIGRATED_RUNNER_ID,
)
from omnigent.db.utils import (
    _build_alembic_config,
)


def _new_engine(uri: str) -> sa.Engine:
    """
    Create a raw migration-test engine without auto-upgrading to head.

    :param uri: SQLAlchemy database URI, e.g.
        ``"sqlite:///tmp/test.db"``.
    :returns: SQLAlchemy engine with SQLite foreign keys enabled.
    """
    engine = sa.create_engine(uri)
    with engine.connect() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=ON"))
    return engine


def _upgrade(engine: sa.Engine, uri: str, revision: str) -> None:
    """
    Run Alembic upgrade to a target revision on a raw engine.

    :param engine: SQLAlchemy engine under migration.
    :param uri: SQLAlchemy database URI, e.g.
        ``"sqlite:///tmp/test.db"``.
    :param revision: Alembic target revision, e.g.
        ``"d7a6b3c91f48"``.
    :returns: None.
    """
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, revision)


def _downgrade(engine: sa.Engine, uri: str, revision: str) -> None:
    """
    Run Alembic downgrade to a target revision on a raw engine.

    :param engine: SQLAlchemy engine under migration.
    :param uri: SQLAlchemy database URI, e.g.
        ``"sqlite:///tmp/test.db"``.
    :param revision: Alembic target revision, e.g.
        ``"d7a6b3c91f48"``.
    :returns: None.
    """
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, revision)


def _conversation_runner_ids(engine: sa.Engine) -> dict[str, str | None]:
    """
    Return conversation runner ids keyed by conversation id.

    :param engine: SQLAlchemy engine to inspect.
    :returns: Mapping of conversation id to runner id.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT id, runner_id FROM conversations ORDER BY id"),
        ).mappings()
        return {str(row["id"]): row["runner_id"] for row in rows}


def test_runner_id_backfill_marks_only_unbound_conversations(tmp_path: Path) -> None:
    """Upgrade converts pre-existing NULL runner ids to an offline sentinel."""
    uri = f"sqlite:///{tmp_path / 'runner-backfill.db'}"
    engine = _new_engine(uri)
    try:
        _upgrade(engine, uri, "d7a6b3c91f48")
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO conversations (id, created_at, updated_at, kind, runner_id) "
                    "VALUES (:id, :ts, :ts, 'default', :runner_id)",
                ),
                [
                    {"id": "conv_bound", "ts": 1700000000, "runner_id": "runner_existing"},
                    {"id": "conv_unbound", "ts": 1700000001, "runner_id": None},
                ],
            )

        _upgrade(engine, uri, "e9f2a7c4d1b8")

        rows = _conversation_runner_ids(engine)
        assert rows == {
            "conv_bound": "runner_existing",
            "conv_unbound": OFFLINE_MIGRATED_RUNNER_ID,
        }
    finally:
        engine.dispose()


def test_runner_id_backfill_downgrade_round_trips_sentinel(tmp_path: Path) -> None:
    """Downgrade restores the pre-PR2 NULL representation."""
    uri = f"sqlite:///{tmp_path / 'runner-backfill-downgrade.db'}"
    engine = _new_engine(uri)
    try:
        _upgrade(engine, uri, "d7a6b3c91f48")
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO conversations (id, created_at, updated_at, kind, runner_id) "
                    "VALUES (:id, :ts, :ts, 'default', :runner_id)",
                ),
                [
                    {"id": "conv_bound", "ts": 1700000000, "runner_id": "runner_existing"},
                    {"id": "conv_unbound", "ts": 1700000001, "runner_id": None},
                ],
            )
        _upgrade(engine, uri, "e9f2a7c4d1b8")

        _downgrade(engine, uri, "d7a6b3c91f48")

        rows = _conversation_runner_ids(engine)
        assert rows == {
            "conv_bound": "runner_existing",
            "conv_unbound": None,
        }
    finally:
        engine.dispose()
