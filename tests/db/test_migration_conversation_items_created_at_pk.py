"""Tests for the conversation_items created_at PK-widening migration (z8a2b3c4d5e6).

The migration makes the table partition-ready: ``created_at`` joins the
primary key and the unique position index so a deployment can
``PARTITION BY (created_at)`` with pure DDL. Partition-readiness leans on
``created_at`` being immutable, so that invariant is pinned here too.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine

from omnigent.db.utils import (
    _build_alembic_config,
    clear_engine_cache,
    get_or_create_engine,
)
from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

_TABLE = "conversation_items"
_POSITION_INDEX = "ix_conversation_items_conversation_id_position"
_HEAD_PK = ["workspace_id", "conversation_id", "id", "created_at"]
_PRIOR_PK = ["workspace_id", "conversation_id", "id"]
_PRIOR_INDEX = ["workspace_id", "conversation_id", "position"]

# An UPDATE targeting the items table itself (not the FTS shadow table).
_ITEMS_UPDATE_RE = re.compile(r'^\s*UPDATE\s+["\'`]?conversation_items["\'`]?\b', re.IGNORECASE)


def _pk(engine: Engine) -> list[str]:
    return sa.inspect(engine).get_pk_constraint(_TABLE)["constrained_columns"]


def _position_index(engine: Engine) -> dict[str, Any]:
    indexes = sa.inspect(engine).get_indexes(_TABLE)
    return next(ix for ix in indexes if ix["name"] == _POSITION_INDEX)


def _message(text: str, response_id: str = "resp_1") -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id=response_id,
        data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
    )


def test_head_pk_includes_created_at_and_position_index_is_plain(tmp_path: Path) -> None:
    """At head the PK still ends in created_at, but the position index is plain.

    z8 added created_at to both the PK and the (then-UNIQUE) position index for
    partition-readiness. A later migration (c7d2e9f4a1b8) repointed the position
    index to a plain ``(workspace_id, conversation_id, position)`` — strict
    position uniqueness is owned by the next_position allocator, not the DB. The
    PK keeps created_at, so the table stays partition-ready.
    """
    uri = f"sqlite:///{tmp_path / 'head.db'}"
    engine = get_or_create_engine(uri)
    try:
        assert _pk(engine) == _HEAD_PK
        index = _position_index(engine)
        assert index["column_names"] == _PRIOR_INDEX  # created_at dropped by c7d2e9f4a1b8
        assert not index["unique"]  # uniqueness moved to the app allocator
    finally:
        engine.dispose()
        clear_engine_cache()


def test_downgrade_restores_prior_key_shapes(tmp_path: Path) -> None:
    """Downgrading one step drops created_at back out of both keys."""
    uri = f"sqlite:///{tmp_path / 'downgrade.db'}"
    engine = get_or_create_engine(uri)
    try:
        assert _pk(engine) == _HEAD_PK

        config = _build_alembic_config(uri)
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.downgrade(config, "z7a2b3c4d5e6")

        assert _pk(engine) == _PRIOR_PK
        index = _position_index(engine)
        assert index["column_names"] == _PRIOR_INDEX
        assert index["unique"]
    finally:
        engine.dispose()
        clear_engine_cache()


def test_item_created_at_is_immutable(tmp_path: Path) -> None:
    """
    No store operation UPDATEs conversation_items or changes an item's
    created_at. A future partitioned deployment routes rows by created_at,
    so a post-insert write would silently relocate rows between partitions.
    """
    uri = f"sqlite:///{tmp_path / 'immutable.db'}"
    engine = get_or_create_engine(uri)
    item_updates: list[str] = []

    @sa.event.listens_for(engine, "before_cursor_execute")
    def _capture(_conn, _cursor, statement, _params, _context, _executemany):  # type: ignore[no-untyped-def]
        if _ITEMS_UPDATE_RE.match(statement):
            item_updates.append(statement)

    try:
        store = SqlAlchemyConversationStore(str(engine.url))
        conv = store.create_conversation()
        store.append(conv.id, [_message("first"), _message("second")])
        before = {item.id: item.created_at for item in store.list_items(conv.id).data}

        # Operations that touch the conversation and its items.
        store.update_conversation(conv.id, title="renamed")
        store.update_conversation(conv.id, archived=True)
        store.append(conv.id, [_message("third", response_id="resp_2")])

        after = {item.id: item.created_at for item in store.list_items(conv.id).data}
        assert all(after[item_id] == ts for item_id, ts in before.items())
        assert item_updates == [], (
            f"conversation_items must be insert/delete-only; saw UPDATE(s): {item_updates!r}"
        )
    finally:
        sa.event.remove(engine, "before_cursor_execute", _capture)
        engine.dispose()
        clear_engine_cache()
