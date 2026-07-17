"""Tests for the conversation_items.data TEXT -> BLOB migration (c4d5e6f7a8b9).

The migration makes ``data`` a binary column so the app layer can store it
zstd-compressed (and, where a column encryptor is installed, encrypted). These
tests pin the type change, that item payloads still round-trip through the
opaque column, and that the downgrade decompresses every value back to
plaintext before restoring ``TEXT``.
"""

from __future__ import annotations

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
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_TABLE = "conversation_items"
_PRIOR_REVISION = "d1e2f3a4b5c6"  # the migration before this one


def _data_column(engine: Engine) -> dict[str, Any]:
    columns = sa.inspect(engine).get_columns(_TABLE)
    return next(col for col in columns if col["name"] == "data")


def _message(text: str, response_id: str = "resp_1") -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id=response_id,
        data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
    )


def test_head_data_column_is_binary(tmp_path: Path) -> None:
    """At head ``data`` is a binary column, not text."""
    uri = f"sqlite:///{tmp_path / 'head.db'}"
    engine = get_or_create_engine(uri)
    try:
        assert isinstance(_data_column(engine)["type"], sa.LargeBinary)
    finally:
        engine.dispose()
        clear_engine_cache()


def test_item_payload_round_trips_and_is_framed(tmp_path: Path) -> None:
    """A large item payload round-trips and is stored as framed (compressed) bytes."""
    uri = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    engine = get_or_create_engine(uri)
    try:
        store = SqlAlchemyConversationStore(str(engine.url))
        conv = store.create_conversation()
        long_text = "the quick brown fox " * 50  # well over the compression threshold
        store.append(conv.id, [_message(long_text)])

        items = store.list_items(conv.id).data
        assert len(items) == 1
        assert items[0].data.content[0]["text"] == long_text

        # The on-disk bytes are framed and zstd-compressed (sentinel + codec).
        with engine.connect() as conn:
            raw = conn.execute(sa.text("SELECT data FROM conversation_items")).scalar_one()
        raw = raw.tobytes() if isinstance(raw, memoryview) else bytes(raw)
        assert raw[0] == 0x00 and raw[1] == 0x01  # NUL sentinel + zstd codec
    finally:
        engine.dispose()
        clear_engine_cache()


def test_downgrade_restores_text_and_plaintext(tmp_path: Path) -> None:
    """Downgrading one step restores TEXT and decompresses every value to plaintext."""
    uri = f"sqlite:///{tmp_path / 'downgrade.db'}"
    engine = get_or_create_engine(uri)
    try:
        store = SqlAlchemyConversationStore(str(engine.url))
        conv = store.create_conversation()
        long_text = "downgrade me back to plaintext " * 20
        store.append(conv.id, [_message(long_text)])

        config = _build_alembic_config(uri)
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.downgrade(config, _PRIOR_REVISION)

        # Column is text again, and the stored value is readable plaintext JSON
        # (the downgrade decompressed the framed bytes).
        assert isinstance(_data_column(engine)["type"], sa.Text)
        with engine.connect() as conn:
            stored = conn.execute(sa.text("SELECT data FROM conversation_items")).scalar_one()
        assert isinstance(stored, str)
        assert long_text in stored
    finally:
        engine.dispose()
        clear_engine_cache()
