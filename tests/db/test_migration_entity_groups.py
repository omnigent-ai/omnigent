"""Tests for the ``entity_groups`` table + ``entities.group_id`` migrations.

Guards that the schema reaches head with the new table/column, that adding
``group_id`` backfills existing entity rows as NULL, and that the code-owned
built-in ids can never collide with generated ids.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from omnigent.db.utils import (
    clear_engine_cache,
    generate_entity_group_id,
    generate_entity_id,
    get_or_create_engine,
)
from omnigent.entities.builtins import builtin_entities, builtin_groups


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """Fresh SQLite DB with the full alembic chain applied; cleaned up after."""
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_entity_groups_table_and_group_id_present(db_engine: Engine) -> None:
    """The migrations create entity_groups and add entities.group_id."""
    insp = sa.inspect(db_engine)
    group_cols = {c["name"] for c in insp.get_columns("entity_groups")}
    assert {"id", "name", "icon_key", "icon_artifact_key", "icon_content_type"} <= group_cols
    entity_cols = {c["name"] for c in insp.get_columns("entities")}
    assert "group_id" in entity_cols


def test_existing_entity_backfills_group_id_null(db_engine: Engine) -> None:
    """An entity row inserted without group_id reads back NULL (nullable add)."""
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO entities (id, created_at, updated_at, title, instruction) "
                "VALUES ('ent_x', 1, 1, 'T', 'i')"
            )
        )
        group_id = conn.execute(
            sa.text("SELECT group_id FROM entities WHERE id = 'ent_x'")
        ).scalar_one()
    assert group_id is None


def test_builtin_ids_disjoint_from_generated() -> None:
    """Built-in ids use a non-hex infix, so they can't collide with minted ids."""
    for g in builtin_groups():
        assert g.id.startswith("grp_builtin_")
    for e in builtin_entities():
        assert e.id.startswith("ent_builtin_")
    # Generated ids are grp_/ent_ + 32 hex chars: never contain "builtin".
    assert "builtin" not in generate_entity_group_id()
    assert "builtin" not in generate_entity_id()
