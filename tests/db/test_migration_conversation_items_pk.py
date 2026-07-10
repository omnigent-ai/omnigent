"""Tests for the conversation_items PK-widening migration (y1a2b3c4d5e6)."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine

from omnigent.db.utils import (
    _build_alembic_config,
    clear_engine_cache,
    get_or_create_engine,
)

_TABLE = "conversation_items"
_HEAD_PK = ["workspace_id", "conversation_id", "id"]
_PRIOR_PK = ["workspace_id", "id"]


def _pk(engine: Engine) -> list[str]:
    return sa.inspect(engine).get_pk_constraint(_TABLE)["constrained_columns"]


def test_head_widens_conversation_items_pk(tmp_path: Path) -> None:
    """At head the PK is ``(workspace_id, conversation_id, id)``."""
    uri = f"sqlite:///{tmp_path / 'head.db'}"
    engine = get_or_create_engine(uri)
    try:
        assert _pk(engine) == _HEAD_PK
    finally:
        engine.dispose()
        clear_engine_cache()


def test_downgrade_restores_prior_conversation_items_pk(tmp_path: Path) -> None:
    """Downgrading one step drops conversation_id back out of the PK."""
    uri = f"sqlite:///{tmp_path / 'downgrade.db'}"
    engine = get_or_create_engine(uri)
    try:
        assert _pk(engine) == _HEAD_PK

        config = _build_alembic_config(uri)
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.downgrade(config, "x1a2b3c4d5e6")

        assert _pk(engine) == _PRIOR_PK
    finally:
        engine.dispose()
        clear_engine_cache()


def test_items_round_trip_via_store(tmp_path: Path) -> None:
    """Append + list still works with conversation_id in the PK."""
    from omnigent.entities.conversation import MessageData, NewConversationItem
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    uri = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    engine = get_or_create_engine(uri)
    try:
        store = SqlAlchemyConversationStore(str(engine.url))
        conv = store.create_conversation()
        store.append(
            conv.id,
            [
                NewConversationItem(
                    type="message",
                    response_id="resp_1",
                    data=MessageData(
                        role="user",
                        content=[{"type": "input_text", "text": "hi"}],
                    ),
                )
            ],
        )
        items = store.list_items(conv.id).data
        assert [i.type for i in items] == ["message"]
    finally:
        engine.dispose()
        clear_engine_cache()
