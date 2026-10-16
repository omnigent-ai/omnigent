"""The ``web_stable_id`` migration adds the column and its lookup index.

The column records, on a native-mirror user message, the web submission id
whose re-send must resolve to that item instead of pasting the prompt into
the pane again — durable across server restarts, unlike the in-memory
pending index. These tests drive the real SQLite migration path
(``upgrade head`` from the prior revision) and the downgrade leg.
"""

from __future__ import annotations

import uuid
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

# The agents index from main must survive the web-stable-id upgrade and downgrade.
_REVISION_BEFORE = "mm1a2b3c4d5e"
_REVISION = "8e914c88dbfb"

_ITEMS_TABLE = "conversation_items"
_INDEX = "ix_conversation_items_web_stable_id"
_AGENTS_INDEX = "ix_agents_kind_owner_created"


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


@pytest.mark.parametrize("prior_revision", ["ll1a2b3c4d5e", _REVISION_BEFORE])
def test_upgrade_adds_web_stable_id_column_and_index(tmp_path: Path, prior_revision: str) -> None:
    """The startup upgrade path adds the nullable column and its index."""
    uri = f"sqlite:///{tmp_path / 'chat.db'}"
    engine = _stamp_at_revision(uri, prior_revision)
    assert not _column_present(engine), "precondition: column absent pre-upgrade"

    # Reflect the old schema: the current ORM already expects the new column.
    items = sa.Table(_ITEMS_TABLE, sa.MetaData(), autoload_with=engine)
    with engine.begin() as conn:
        conn.execute(
            items.insert().values(
                workspace_id=0,
                conversation_id=uuid.uuid4().bytes,
                id=uuid.uuid4().bytes,
                response_id="before-upgrade",
                created_at=1,
                position=0,
                type=1,
                status=1,
                data='{"role":"user","content":[]}',
                search_text="existing message",
            )
        )
        before = dict(conn.execute(sa.select(items)).mappings().one())

    _initialize_or_verify_schema(engine, uri)

    assert _get_current_db_revision(engine) == _get_head_db_revision(uri)
    assert _column_present(engine)
    assert _index_present(engine)
    assert _AGENTS_INDEX in {ix["name"] for ix in sa.inspect(engine).get_indexes("agents")}

    items = sa.Table(_ITEMS_TABLE, sa.MetaData(), autoload_with=engine)
    with engine.connect() as conn:
        after = dict(conn.execute(sa.select(items)).mappings().one())
    assert after == {**before, "web_stable_id": None}


def test_downgrade_drops_web_stable_id_column_and_index(tmp_path: Path) -> None:
    """The batch-mode downgrade removes the column and index cleanly."""
    uri = f"sqlite:///{tmp_path / 'chat.db'}"
    engine = _stamp_at_revision(uri, _REVISION)
    assert _column_present(engine)
    assert _index_present(engine)

    config = _build_alembic_config(uri)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, _REVISION_BEFORE)

    assert _get_current_db_revision(engine) == _REVISION_BEFORE
    assert not _column_present(engine)
    assert not _index_present(engine)
    assert _AGENTS_INDEX in {ix["name"] for ix in sa.inspect(engine).get_indexes("agents")}
