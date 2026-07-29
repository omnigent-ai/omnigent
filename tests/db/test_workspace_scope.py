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
    store.create(agent_id="ce2e8b9df3fda6a891350c75625640bf", name="n0", bundle_location="loc")
    with workspace_scope(42):
        store.create(
            agent_id="dcfa34ef0735a8784bdfa70b6cad0142", name="n42", bundle_location="loc"
        )

    engine = sa.create_engine(db_uri)
    with engine.connect() as conn:
        # Raw driver read bypasses the Uuid16 type, so ids come back as the raw
        # 16 bytes — decode to bare hex to compare (this test checks workspace_id).
        rows = conn.exec_driver_sql("SELECT id, workspace_id FROM agents").fetchall()
        stored = {(k.hex() if isinstance(k, (bytes, bytearray)) else k): v for k, v in rows}
    engine.dispose()
    assert stored == {
        "ce2e8b9df3fda6a891350c75625640bf": 0,
        "dcfa34ef0735a8784bdfa70b6cad0142": 42,
    }


def test_reads_are_isolated_per_workspace(db_uri: str) -> None:
    """A row created in one workspace is invisible from another."""
    store = SqlAlchemyAgentStore(db_uri)
    store.create(agent_id="dfcff8d2c8d4ff3cd5be9f2a7194d409", name="d", bundle_location="loc")
    with workspace_scope(42):
        store.create(agent_id="da4899778b7c60ee14cfaee729dbb171", name="t", bundle_location="loc")
        # In workspace 42: sees its own row, not workspace 0's.
        assert store.get("da4899778b7c60ee14cfaee729dbb171") is not None
        assert store.get("dfcff8d2c8d4ff3cd5be9f2a7194d409") is None
    # Back in the default workspace: the reverse.
    assert store.get("dfcff8d2c8d4ff3cd5be9f2a7194d409") is not None
    assert store.get("da4899778b7c60ee14cfaee729dbb171") is None
