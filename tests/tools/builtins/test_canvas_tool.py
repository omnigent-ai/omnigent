"""Tests for the set_canvas builtin tool."""

from __future__ import annotations

import json

import pytest

from omnigent.stores.canvas_store.sqlalchemy_store import SqlAlchemyCanvasStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.canvas import SetCanvasTool


@pytest.fixture()
def wired(db_uri: str, monkeypatch: pytest.MonkeyPatch) -> tuple[str, SqlAlchemyCanvasStore]:
    """A real conversation id + canvas store wired into runtime.get_canvas_store."""
    store = SqlAlchemyCanvasStore(db_uri)
    monkeypatch.setattr("omnigent.runtime.get_canvas_store", lambda: store)
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(title="c").id
    return conv, store


def test_set_canvas_upserts(wired: tuple[str, SqlAlchemyCanvasStore]) -> None:
    conv, store = wired
    ctx = ToolContext(task_id="t", agent_id="a", conversation_id=conv)

    out = json.loads(
        SetCanvasTool().invoke(json.dumps({"title": "Chart", "content": "<svg/>"}), ctx)
    )
    assert out["ok"] is True
    assert out["canvas"]["content_type"] == "html"  # default

    stored = store.get_by_conversation(conv)
    assert stored is not None
    assert stored.title == "Chart"
    assert stored.content == "<svg/>"

    # Re-set with markdown overwrites.
    SetCanvasTool().invoke(
        json.dumps({"title": "Notes", "content": "# hi", "content_type": "markdown"}), ctx
    )
    again = store.get_by_conversation(conv)
    assert again is not None and again.content_type == "markdown" and again.title == "Notes"


def test_validation_and_context(wired: tuple[str, SqlAlchemyCanvasStore]) -> None:
    conv, _ = wired
    with_ctx = ToolContext(task_id="t", agent_id="a", conversation_id=conv)
    no_ctx = ToolContext(task_id="t", agent_id="a")

    assert "error" in json.loads(SetCanvasTool().invoke(json.dumps({"content": "x"}), with_ctx))
    assert "error" in json.loads(SetCanvasTool().invoke(json.dumps({"title": "t"}), with_ctx))
    bad_type = json.loads(
        SetCanvasTool().invoke(
            json.dumps({"title": "t", "content": "x", "content_type": "pdf"}), with_ctx
        )
    )
    assert "error" in bad_type
    no_conv = json.loads(
        SetCanvasTool().invoke(json.dumps({"title": "t", "content": "x"}), no_ctx)
    )
    assert "error" in no_conv and "conversation" in no_conv["error"]
