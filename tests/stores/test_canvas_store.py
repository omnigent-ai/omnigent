"""Tests for SqlAlchemyCanvasStore (one canvas per conversation)."""

from __future__ import annotations

from omnigent.stores.canvas_store.sqlalchemy_store import SqlAlchemyCanvasStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


def test_upsert_creates_then_overwrites(
    canvas_store: SqlAlchemyCanvasStore,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation(title="c").id
    assert canvas_store.get_by_conversation(conv) is None

    created = canvas_store.upsert("cnv_1", conv, "Report", "<h1>Hi</h1>", "html")
    assert created.id == "cnv_1"
    assert created.content_type == "html"
    assert created.updated_at is None

    # Re-upsert overwrites in place (same row), stamping updated_at.
    updated = canvas_store.upsert("cnv_ignored", conv, "Report v2", "<h1>Bye</h1>", "markdown")
    assert updated.id == "cnv_1"  # original id kept
    assert updated.title == "Report v2"
    assert updated.content == "<h1>Bye</h1>"
    assert updated.content_type == "markdown"
    assert updated.updated_at is not None

    fetched = canvas_store.get_by_conversation(conv)
    assert fetched is not None
    assert fetched.id == "cnv_1"
    assert fetched.title == "Report v2"


def test_delete_is_idempotent(
    canvas_store: SqlAlchemyCanvasStore,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation().id
    canvas_store.upsert("cnv_d", conv, "t", "<p>x</p>", "html")
    assert canvas_store.delete(conv) is True
    assert canvas_store.get_by_conversation(conv) is None
    assert canvas_store.delete(conv) is False
