"""Tests for the workspace_id migration (r1a2b3c4d5e6)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine

from omnigent.db.db_models import DEFAULT_WORKSPACE_ID
from omnigent.db.utils import (
    _build_alembic_config,
    clear_engine_cache,
    get_or_create_engine,
)

# Every table and the primary key it had BEFORE the workspace_id migration
# (r1a2b3c4d5e6). After that migration each key was ``["workspace_id",
# *original]``. Subsequent migrations may have changed some PKs further
# (e.g. v1a2b3c4d5e6 changed hosts to (workspace_id, host_id)), so this
# map records the pre-r-migration original columns only — not the final
# head state for every table.
_ORIGINAL_PKS: dict[str, list[str]] = {
    "agents": ["id"],
    "files": ["id"],
    "users": ["id"],
    "account_tokens": ["id"],
    "session_permissions": ["user_id", "conversation_id"],
    "conversations": ["id"],
    "conversation_items": ["id"],
    "conversation_labels": ["conversation_id", "key"],
    "comments": ["id"],
    "policies": ["id"],
    "hosts": ["owner", "name"],
    "user_daily_cost": ["user_id", "day_utc"],
}

# Tables whose PK was changed again by a migration that came after
# r1a2b3c4d5e6. The value is the expected PK at the current head.
# ``test_workspace_id_leads_the_primary_key`` uses these so that later
# migrations don't break the r-migration test.
_LATER_PK_OVERRIDES: dict[str, list[str]] = {
    # v1a2b3c4d5e6 replaced (workspace_id, owner, name) with
    # (workspace_id, host_id) for the hosts table.
    "hosts": ["workspace_id", "host_id"],
    # y1a2b3c4d5e6 widened conversation_items to insert conversation_id
    # between workspace_id and id.
    "conversation_items": ["workspace_id", "conversation_id", "id"],
}


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """Fresh SQLite database with the full migration chain applied."""
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


@pytest.mark.parametrize("table", sorted(_ORIGINAL_PKS))
def test_workspace_id_column_exists_and_is_not_nullable(db_engine: Engine, table: str) -> None:
    """Every table gains a NOT NULL ``workspace_id`` column."""
    columns = {c["name"]: c for c in sa.inspect(db_engine).get_columns(table)}
    assert "workspace_id" in columns, f"{table}.workspace_id must exist after migration"
    assert not columns["workspace_id"]["nullable"], f"{table}.workspace_id must be NOT NULL"


@pytest.mark.parametrize("table", sorted(_ORIGINAL_PKS))
def test_workspace_id_leads_the_primary_key(db_engine: Engine, table: str) -> None:
    """workspace_id is the leading PK column on every table.

    For tables whose PK was later changed by a post-r-migration (see
    ``_LATER_PK_OVERRIDES``), the expected value is the override rather than
    the r-migration result.
    """
    pk = sa.inspect(db_engine).get_pk_constraint(table)["constrained_columns"]
    expected = _LATER_PK_OVERRIDES.get(table, ["workspace_id", *_ORIGINAL_PKS[table]])
    assert pk == expected


def test_existing_rows_and_omitted_inserts_default_to_zero(db_engine: Engine) -> None:
    """server_default backfills existing rows and fills omitted inserts with 0."""
    with db_engine.begin() as conn:
        # Insert without specifying workspace_id — the DB server_default applies.
        conn.execute(
            sa.text(
                "INSERT INTO agents"
                " (id, created_at, name, bundle_location, version, kind)"
                " VALUES ('ag_ws', 1, 'n', 'loc', 1, 1)"  # kind=1 → 'template'
            )
        )
        workspace_id = conn.execute(
            sa.text("SELECT workspace_id FROM agents WHERE id = 'ag_ws'")
        ).scalar_one()
    assert workspace_id == DEFAULT_WORKSPACE_ID


def test_agent_round_trip_via_store(db_engine: Engine) -> None:
    """A store insert/read cycle still works once workspace_id is in the PK."""
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

    store = SqlAlchemyAgentStore(str(db_engine.url))
    created = store.create(
        agent_id="ag_rt",
        name="round-trip",
        bundle_location="loc",
    )
    fetched = store.get(created.id)
    assert fetched is not None
    assert fetched.id == "ag_rt"


def test_downgrade_removes_workspace_id_and_restores_pk(tmp_path: Path) -> None:
    """Downgrade drops workspace_id and restores each original primary key."""
    db_path = tmp_path / "downgrade.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)

    # Start at head (includes r1a2b3c4d5e6).
    assert "workspace_id" in {c["name"] for c in sa.inspect(engine).get_columns("agents")}

    # Downgrade one step to the prior head.
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "q1a2b3c4d5e6")

    inspector = sa.inspect(engine)
    for table, original_pk in _ORIGINAL_PKS.items():
        columns = {c["name"] for c in inspector.get_columns(table)}
        assert "workspace_id" not in columns, f"{table}.workspace_id must be dropped by downgrade"
        assert inspector.get_pk_constraint(table)["constrained_columns"] == original_pk

    engine.dispose()
    clear_engine_cache()
