"""Tests for dropping the redundant conversations timestamp sort indexes.

``ix_conversations_created_at`` and ``ix_conversations_updated_at`` were bare
``(workspace_id, <ts>, id)`` sort indexes. Every query that sorts those columns
already narrows rows by a more selective key — the ACL id-set (resolved via the
PK), the default sidebar (served by ``ix_conversations_archived_updated``), or
parent/root listings (their own indexes) — so neither bare index is the chosen
access path, while ``updated_at`` is rewritten on every item append. Migration
``f4a1c8b2d3e6`` drops both; these tests assert the drop and that the downgrade
restores them.
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
    clear_engine_cache,
    get_or_create_engine,
)

_DROP_REVISION = "f4a1c8b2d3e6"
_PRE_DROP_REVISION = "d1e2f3a4b5c6"

_DROPPED = ("ix_conversations_created_at", "ix_conversations_updated_at")


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """
    Fresh SQLite DB with the full alembic chain applied; cleaned up after.

    :param tmp_path: Pytest-managed temp directory for the SQLite file.
    :returns: Engine pointed at the migrated database (at head).
    """
    uri = f"sqlite:///{tmp_path / 'test.db'}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def _conv_indexes(engine: Engine) -> set[str]:
    """Return the set of index names on ``conversations`` by reflection."""
    return {i["name"] for i in sa.inspect(engine).get_indexes("conversations")}


def test_timestamp_indexes_absent_at_head(db_engine: Engine) -> None:
    """
    At head the two bare sort indexes are gone, but the sidebar index survives.

    A failure means the drop migration didn't apply (the indexes would keep
    absorbing writes) or that it over-reached and removed the archived+updated
    index that actually backs the default ``GET /v1/sessions`` listing.
    """
    idx = _conv_indexes(db_engine)
    for name in _DROPPED:
        assert name not in idx, f"{name} should be dropped at head; found it in {sorted(idx)}."
    assert "ix_conversations_archived_updated" in idx, (
        "ix_conversations_archived_updated must survive — it backs the default sidebar list."
    )


def test_downgrade_restores_indexes(tmp_path: Path) -> None:
    """
    Downgrade recreates both composite indexes; a re-upgrade drops them again.

    The downgrade leg matters because the ``get_or_create_engine`` fixtures only
    ever run ``upgrade head`` — this is the one place the ``downgrade`` body is
    exercised, proving the chain stays reversible.
    """
    uri = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    cfg = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    try:
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, _DROP_REVISION)
        assert not (set(_DROPPED) & _conv_indexes(engine)), (
            "the two timestamp indexes should be absent at the drop revision."
        )

        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, _PRE_DROP_REVISION)
        restored = _conv_indexes(engine)
        for name in _DROPPED:
            assert name in restored, f"downgrade must recreate {name}; have {sorted(restored)}."

        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, _DROP_REVISION)
        assert not (set(_DROPPED) & _conv_indexes(engine)), (
            "re-upgrade after downgrade should drop the indexes again."
        )
    finally:
        engine.dispose()
        clear_engine_cache()
