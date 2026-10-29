"""Tests for the ``session_browser_tabs`` migration (a3f7412aa11a)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.compression import CompressedText
from omnigent.db.db_models import SqlSessionBrowserTabs
from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_PREVIOUS = "d0a1f879a01d"
_REVISION = "a3f7412aa11a"
_TABLE = "session_browser_tabs"


def _migrate(uri: str, engine: sa.Engine, step: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        getattr(command, step)(config, revision)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[tuple[str, sa.Engine]]:
    uri = f"sqlite:///{tmp_path / 'tabs.db'}"
    engine = sa.create_engine(uri)
    try:
        yield uri, engine
    finally:
        engine.dispose()
        clear_engine_cache()


def test_model_matches_the_migrated_table(database: tuple[str, sa.Engine]) -> None:
    uri, engine = database
    _migrate(uri, engine, "upgrade", _REVISION)

    inspector = sa.inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns(_TABLE)}
    assert set(columns) == set(SqlSessionBrowserTabs.__table__.c.keys())
    assert all(not column["nullable"] for column in columns.values())
    assert inspector.get_pk_constraint(_TABLE)["constrained_columns"] == [
        "workspace_id",
        "conversation_id",
        "user_id",
    ]
    assert isinstance(SqlSessionBrowserTabs.__table__.c.tabs.type, CompressedText)


def test_downgrade_drops_the_table(database: tuple[str, sa.Engine]) -> None:
    uri, engine = database
    _migrate(uri, engine, "upgrade", _REVISION)

    _migrate(uri, engine, "downgrade", _PREVIOUS)

    assert _TABLE not in sa.inspect(engine).get_table_names()
