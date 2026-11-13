"""Tests for the scheduled_tasks kind migration."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import IntegrityError

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_REVISION = "27d16059fab9"
_PREVIOUS = "b4e2d7a91c35"

_TASK = {
    "name": "nightly",
    "prompt": b"p",
    "rrule": "FREQ=DAILY;BYHOUR=9",
    "agent_id": b"g" * 16,
    "timezone": "UTC",
    "created_at": 1,
}


def _migrate(uri: str, engine: sa.Engine, revision: str, *, down: bool = False) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        (command.downgrade if down else command.upgrade)(config, revision)


def _tasks(engine: sa.Engine) -> sa.Table:
    return sa.Table("scheduled_tasks", sa.MetaData(), autoload_with=engine)


@pytest.fixture
def engine_and_uri(tmp_path: Path):
    uri = f"sqlite:///{tmp_path / 'tasks.db'}"
    engine = sa.create_engine(uri)
    try:
        yield engine, uri
    finally:
        engine.dispose()
        clear_engine_cache()


def test_existing_tasks_become_automations_and_reminders_round_trip(engine_and_uri) -> None:
    engine, uri = engine_and_uri
    _migrate(uri, engine, _PREVIOUS)
    with engine.begin() as conn:
        conn.execute(_tasks(engine).insert().values(id=b"a" * 16, **_TASK))

    _migrate(uri, engine, _REVISION)
    tasks = _tasks(engine)
    with engine.begin() as conn:
        # A task saved before the migration is an ordinary automation.
        assert conn.execute(sa.select(tasks.c.kind)).scalar_one() == 1
        conn.execute(tasks.insert().values(id=b"b" * 16, kind=2, **_TASK))
        kinds = dict(conn.execute(sa.select(tasks.c.id, tasks.c.kind)).tuples().all())
    assert kinds == {b"a" * 16: 1, b"b" * 16: 2}

    _migrate(uri, engine, _PREVIOUS, down=True)
    assert "kind" not in {c["name"] for c in sa.inspect(engine).get_columns("scheduled_tasks")}
    with engine.begin() as conn:
        count = conn.execute(sa.select(sa.func.count()).select_from(_tasks(engine))).scalar_one()
    assert count == 2

    _migrate(uri, engine, _REVISION)
    with engine.begin() as conn:
        assert conn.execute(sa.select(_tasks(engine).c.kind)).scalars().all() == [1, 1]


def test_kind_check_rejects_unknown_codes(engine_and_uri) -> None:
    engine, uri = engine_and_uri
    _migrate(uri, engine, _REVISION)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(_tasks(engine).insert().values(id=b"c" * 16, kind=3, **_TASK))
