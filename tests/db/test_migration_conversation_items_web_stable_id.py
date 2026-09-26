"""The ``web_stable_id`` migration adds the column and its lookup index.

The column records, on a native-mirror user message, the web submission id
whose re-send must resolve to that item instead of pasting the prompt into
the pane again — durable across server restarts, unlike the in-memory
pending index. These tests drive the real SQLite migration path
(``upgrade head`` from the prior revision) and the downgrade leg.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine

from omnigent.db.utils import (
    _build_alembic_config,
    _create_engine,
    _get_current_db_revision,
    _get_head_db_revision,
    _initialize_or_verify_schema,
    clear_engine_cache,
)

# Revision one step before ``mm1a2b3c4d5e`` (which adds ``web_stable_id``).
_REVISION_BEFORE = "ll1a2b3c4d5e"

_ITEMS_TABLE = "conversation_items"
_INDEX = "ix_conversation_items_web_stable_id"


def _stamp_at_revision(uri: str, revision: str) -> Engine:
    """Build a real SQLite DB migrated exactly up to ``revision``."""
    engine = _create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)
    assert _get_current_db_revision(engine) == revision
    return engine


def _column_present(engine: Engine) -> bool:
    cols = {c["name"] for c in sa.inspect(engine).get_columns(_ITEMS_TABLE)}
    return "web_stable_id" in cols


def _index_present(engine: Engine) -> bool:
    names = {ix["name"] for ix in sa.inspect(engine).get_indexes(_ITEMS_TABLE)}
    return _INDEX in names


@pytest.fixture(autouse=True)
def _clean_engine_cache() -> Iterator[None]:
    """Ensure no cached engine leaks between tests."""
    try:
        yield
    finally:
        clear_engine_cache()


def test_upgrade_adds_web_stable_id_column_and_index(tmp_path: Path) -> None:
    """The startup upgrade path adds the nullable column and its index."""
    uri = f"sqlite:///{tmp_path / 'chat.db'}"
    engine = _stamp_at_revision(uri, _REVISION_BEFORE)
    assert not _column_present(engine), "precondition: column absent pre-upgrade"

    _initialize_or_verify_schema(engine, uri)

    assert _get_current_db_revision(engine) == _get_head_db_revision(uri)
    assert _column_present(engine)
    assert _index_present(engine)

    # Pre-existing rows read back with a NULL mapping — the column must not
    # disturb them (it is only written for new native mirrors).
    with engine.connect() as conn:
        assert conn.execute(sa.text(f"SELECT COUNT(*) FROM {_ITEMS_TABLE}")).scalar_one() == 0


def test_downgrade_drops_web_stable_id_column_and_index(tmp_path: Path) -> None:
    """The batch-mode downgrade removes the column and index cleanly."""
    uri = f"sqlite:///{tmp_path / 'chat.db'}"
    engine = _stamp_at_revision(uri, "mm1a2b3c4d5e")
    assert _column_present(engine)
    assert _index_present(engine)

    config = _build_alembic_config(uri)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, _REVISION_BEFORE)

    assert _get_current_db_revision(engine) == _REVISION_BEFORE
    assert not _column_present(engine)
    assert not _index_present(engine)
