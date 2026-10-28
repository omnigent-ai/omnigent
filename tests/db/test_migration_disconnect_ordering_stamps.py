"""Tests for the ``omnigent_conversation_metadata.live_status_at`` migration.

The column records when the writing replica observed ``live_status``. It is
additive and nullable, so rows written before it read NULL, and the revision
is safe to replay or roll back.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.db_models import OmnigentBase
from omnigent.db.utils import _build_alembic_config

# Revision ids bounding the migration under test.
_PRIOR_HEAD = "c8e4f2a6b901"
_THIS_REVISION = "ng12ab34cd56"
_MAIN_HEAD = "b2e4f6a8c013"
_MERGE_REVISION = "f3a7c9e2b014"
_TABLE = "omnigent_conversation_metadata"


def _migrate(uri: str, engine: sa.Engine, revision: str, *, downgrade: bool = False) -> None:
    """
    Run Alembic to a target revision on a raw engine.

    :param uri: SQLAlchemy database URI, e.g. ``"sqlite:///tmp/test.db"``.
    :param engine: SQLAlchemy engine under migration.
    :param revision: Alembic target revision, e.g. ``"ng12ab34cd56"``.
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
        assert not {"live_status_at", "runner_last_connected"}.intersection(_columns(engine))
        with engine.begin() as conn:
            conn.execute(
                sa.text(f"INSERT INTO {_TABLE} (id, live_status) VALUES (:id, 1)"),
                {"id": "3f0c3d9a8a1b4c5d9e7f6a5b4c3d2e1f"},
            )

        _migrate(uri, engine, _THIS_REVISION)

        column = _columns(engine)["live_status_at"]
        assert column["nullable"]
        assert isinstance(column["type"], sa.BigInteger)
        connected = _columns(engine)["runner_last_connected"]
        assert connected["nullable"]
        assert isinstance(connected["type"], sa.Integer)
        with engine.connect() as conn:
            stamps = conn.execute(
                sa.text(f"SELECT live_status, live_status_at, runner_last_connected FROM {_TABLE}")
            )
            assert [tuple(row) for row in stamps] == [(1, None, None)]
    finally:
        engine.dispose()


def _stamp(uri: str, engine: sa.Engine, revision: str | list[str]) -> None:
    """Record *revision* as applied without running it, as an interrupted run can."""
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.stamp(config, revision, purge=True)


def test_live_status_at_downgrades_and_replays(tmp_path: Path) -> None:
    """Downgrade drops the column, and replaying either step is a no-op."""
    uri = f"sqlite:///{tmp_path / 'live-status-at-replay.db'}"
    engine = sa.create_engine(uri)
    try:
        _migrate(uri, engine, _THIS_REVISION)
        # The column exists but the version table says it doesn't yet.
        _stamp(uri, engine, _PRIOR_HEAD)
        _migrate(uri, engine, _THIS_REVISION)
        assert {"live_status_at", "runner_last_connected"} <= _columns(engine).keys()
        row_id = "3f0c3d9a8a1b4c5d9e7f6a5b4c3d2e1f"
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    f"INSERT INTO {_TABLE} "
                    "(id, live_status, live_status_at, runner_last_connected) "
                    "VALUES (:id, 1, 2000, 3)"
                ),
                {"id": row_id},
            )

        _migrate(uri, engine, _PRIOR_HEAD, downgrade=True)
        assert not {"live_status_at", "runner_last_connected"}.intersection(_columns(engine))
        _stamp(uri, engine, _THIS_REVISION)
        _migrate(uri, engine, _PRIOR_HEAD, downgrade=True)
        assert not {"live_status_at", "runner_last_connected"}.intersection(_columns(engine))
        with engine.connect() as conn:
            assert conn.execute(sa.text(f"SELECT id, live_status FROM {_TABLE}")).all() == [
                (row_id, 1)
            ]
    finally:
        engine.dispose()


@pytest.mark.parametrize("starting_revision", ["base", _THIS_REVISION, _MAIN_HEAD])
def test_disconnect_stamps_join_current_schema(tmp_path: Path, starting_revision: str) -> None:
    uri = f"sqlite:///{tmp_path / 'merged-ordering-stamps.db'}"
    engine = sa.create_engine(uri)
    row_id = bytes.fromhex("0123456789abcdef0123456789abcdef")
    try:
        _migrate(uri, engine, starting_revision)
        if starting_revision != "base":
            with engine.begin() as conn:
                conn.execute(
                    sa.text(f"INSERT INTO {_TABLE} (id, live_status) VALUES (:id, 1)"),
                    {"id": row_id},
                )

        _migrate(uri, engine, "head")
        assert {"pull_requests", "live_status_at", "runner_last_connected"} <= _columns(
            engine
        ).keys()
        with engine.connect() as conn:
            assert (
                conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
                == _MERGE_REVISION
            )
            if starting_revision != "base":
                assert conn.execute(sa.text(f"SELECT id, live_status FROM {_TABLE}")).all() == [
                    (row_id, 1)
                ]

        _migrate(uri, engine, _THIS_REVISION, downgrade=True)
        with engine.connect() as conn:
            assert set(
                conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars()
            ) == {
                "d0a1f879a01d",
                _THIS_REVISION,
            }
        _migrate(uri, engine, "head")
    finally:
        engine.dispose()


@pytest.mark.parametrize("starting_revision", ["base", "e7c9a2d4f601", "d0a1f879a01d"])
def test_runner_connection_fence_schema_replays_and_retains_tombstones(
    tmp_path: Path, starting_revision: str
) -> None:
    uri = f"sqlite:///{tmp_path / 'runner-fences.db'}"
    engine = sa.create_engine(uri)
    try:
        _migrate(uri, engine, starting_revision)
        _migrate(uri, engine, "head")
        table = sa.Table("runner_connections", sa.MetaData(), autoload_with=engine)
        # Fresh bootstrap and interrupted-bootstrap validation use ORM metadata.
        model_table = OmnigentBase.metadata.tables["runner_connections"]
        assert {
            column.name: (
                column.type.compile(dialect=engine.dialect),
                column.nullable,
                column.primary_key,
            )
            for column in model_table.columns
        } == {
            column.name: (
                column.type.compile(dialect=engine.dialect),
                column.nullable,
                column.primary_key,
            )
            for column in table.columns
        }
        assert set(table.primary_key.columns.keys()) == {"workspace_id", "runner_id"}
        epoch = bytes.fromhex("0123456789abcdef0123456789abcdef")
        with engine.begin() as conn:
            for workspace in (0, 1):
                conn.execute(
                    table.insert().values(
                        workspace_id=workspace,
                        runner_id="runner",
                        epoch=epoch,
                        state=2,
                        last_seen=None,
                        last_connected=123,
                        heartbeat_epoch=epoch,
                    )
                )
        # The sibling migration remains applied; replay only the fence revision.
        _stamp(uri, engine, ["e7c9a2d4f601", "d0a1f879a01d"])
        _migrate(uri, engine, "head")
        with engine.connect() as conn:
            rows = conn.execute(sa.select(table).order_by(table.c.workspace_id)).all()
            assert [
                (r.workspace_id, r.state, r.last_seen, r.last_connected, r.epoch) for r in rows
            ] == [
                (0, 2, None, 123, epoch),
                (1, 2, None, 123, epoch),
            ]
        _migrate(uri, engine, "e7c9a2d4f601", downgrade=True)
        assert not sa.inspect(engine).has_table("runner_connections")
        _migrate(uri, engine, "head")
        with engine.connect() as conn:
            assert conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0
    finally:
        engine.dispose()
