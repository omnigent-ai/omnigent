"""Tests for the ``current_workspace_id`` / ``workspace_scope`` seam.

The stores hardcode no workspace id: reads, filters, and inserts resolve it
through ``current_workspace_id()`` (a ContextVar). OSS leaves it at the
default (0); a multi-tenant deployment binds a real id per request via
``workspace_scope``. These tests pin that behaviour so the seam stays the
single place a workspace id is injected.
"""

from __future__ import annotations

import sqlalchemy as sa

from omnigent.db.db_models import (
    DEFAULT_WORKSPACE_ID,
    current_workspace_id,
    workspace_scope,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore


def test_default_workspace_is_zero() -> None:
    """With nothing bound, the resolver yields the OSS default (0)."""
    assert current_workspace_id() == DEFAULT_WORKSPACE_ID == 0


def test_workspace_scope_sets_and_resets() -> None:
    """``workspace_scope`` binds inside the block and restores on exit."""
    assert current_workspace_id() == 0
    with workspace_scope(42):
        assert current_workspace_id() == 42
        with workspace_scope(7):
            assert current_workspace_id() == 7
        assert current_workspace_id() == 42
    assert current_workspace_id() == 0


def test_insert_stamps_scoped_workspace(db_uri: str) -> None:
    """An ORM insert stamps ``workspace_id`` from the active context."""
    store = SqlAlchemyAgentStore(db_uri)
    store.create(agent_id="ag_ws0", name="n0", bundle_location="loc")
    with workspace_scope(42):
        store.create(agent_id="ag_ws42", name="n42", bundle_location="loc")

    engine = sa.create_engine(db_uri)
    with engine.connect() as conn:
        stored = dict(conn.exec_driver_sql("SELECT id, workspace_id FROM agents").fetchall())
    engine.dispose()
    assert stored == {"ag_ws0": 0, "ag_ws42": 42}


def test_reads_are_isolated_per_workspace(db_uri: str) -> None:
    """A row created in one workspace is invisible from another."""
    store = SqlAlchemyAgentStore(db_uri)
    store.create(agent_id="ag_default", name="d", bundle_location="loc")
    with workspace_scope(42):
        store.create(agent_id="ag_tenant", name="t", bundle_location="loc")
        # In workspace 42: sees its own row, not workspace 0's.
        assert store.get("ag_tenant") is not None
        assert store.get("ag_default") is None
    # Back in the default workspace: the reverse.
    assert store.get("ag_default") is not None
    assert store.get("ag_tenant") is None
