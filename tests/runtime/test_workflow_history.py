"""Tests for runtime conversation-history loading."""

from __future__ import annotations

from typing import Any

from omnigent.entities import (
    CompactionData,
    ConversationItem,
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    NativeToolData,
    PagedList,
    SlashCommandData,
)
from omnigent.entities.pagination import paginate_in_memory
from omnigent.runtime.prompt import history_to_input_items
from omnigent.runtime.workflow import _load_initial_history


class _ConversationStore:
    """
    Minimal conversation store for history-loader tests.

    :param items: Chronological conversation items returned by
        ``list_items``.
    """

    def __init__(self, items: list[ConversationItem]) -> None:
        self._items = items

    def list_items(
        self,
        conversation_id: str,
        *,
        type: str | None = None,
        order: str = "asc",
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        **kwargs: Any,
    ) -> PagedList[ConversationItem]:
        """
        Return an in-memory page matching ``ConversationStore.list_items``.

        :param conversation_id: Conversation id being queried.
        :param type: Optional item-type filter, e.g. ``"compaction"``.
        :param order: Sort order, ``"asc"`` or ``"desc"``.
        :param limit: Maximum items in the returned page.
        :param after: Cursor id to start after.
        :param before: Cursor id to stop before.
        :param kwargs: Additional store-specific parameters ignored by
            this fake.
        :returns: Paginated in-memory items.
        """
        del conversation_id, kwargs
        items = [item for item in self._items if type is None or item.type == type]
        return paginate_in_memory(
            items,
            lambda item: item.id,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )


def test_load_initial_history_filters_visible_slash_command_but_keeps_meta_message() -> None:
    """
    Visible command metadata is not LLM content, but hidden skill
    context is.
    """
    slash = ConversationItem(
        id="sc_1",
        type="slash_command",
        status="completed",
        response_id="turn_skill",
        created_at=1,
        data=SlashCommandData(
            agent="test-agent",
            name="grill-me",
            arguments="review this plan",
        ),
    )
    meta = ConversationItem(
        id="msg_meta",
        type="message",
        status="completed",
        response_id="turn_skill",
        created_at=2,
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": "<skill>hidden</skill>"}],
            is_meta=True,
        ),
    )
    visible = ConversationItem(
        id="msg_visible",
        type="message",
        status="completed",
        response_id="turn_user",
        created_at=3,
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": "hello"}],
        ),
    )

    loaded = _load_initial_history(
        _ConversationStore([slash, meta, visible]),  # type: ignore[arg-type]
        "conv_123",
    )

    assert [item.id for item in loaded.items] == ["msg_meta", "msg_visible"]
    assert isinstance(loaded.items[0].data, MessageData)
    assert loaded.items[0].data.is_meta is True


def test_load_initial_history_replays_typed_compaction_snapshot() -> None:
    """Compacted assistant/tool/native records survive history conversion."""
    anchor = ConversationItem(
        id="msg_anchor",
        type="message",
        status="completed",
        response_id="resp_old",
        created_at=1,
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": "old prompt"}],
        ),
    )
    compaction = ConversationItem(
        id="cmp_typed",
        type="compaction",
        status="completed",
        response_id="resp_compact",
        created_at=2,
        data=CompactionData(
            summary="prior context",
            last_item_id=anchor.id,
            model="model-from-compaction",
            token_count=12,
            compacted_messages=[
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "I will inspect the file.",
                    "interrupted": True,
                    "stream_message_id": "stream_1",
                },
                {
                    "type": "function_call",
                    "call_id": "call_read",
                    "name": "read_file",
                    "arguments": '{"path":"a.txt"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_read",
                    "output": "file contents",
                },
                {
                    "type": "web_search_call",
                    "id": "search_1",
                    "status": "completed",
                },
            ],
        ),
    )

    loaded = _load_initial_history(_ConversationStore([anchor, compaction]), "conv_typed")

    assert [item.type for item in loaded.items] == [
        "message",
        "function_call",
        "function_call_output",
        "native_tool",
    ]
    assert isinstance(loaded.items[0].data, MessageData)
    assert loaded.items[0].data.agent == "model-from-compaction"
    assert loaded.items[0].data.interrupted is True
    assert loaded.items[0].data.stream_message_id == "stream_1"
    assert isinstance(loaded.items[1].data, FunctionCallData)
    assert loaded.items[1].data.call_id == "call_read"
    assert isinstance(loaded.items[2].data, FunctionCallOutputData)
    assert loaded.items[2].data.output == "file contents"
    assert isinstance(loaded.items[3].data, NativeToolData)
    assert loaded.items[3].data.item["id"] == "search_1"

    assert history_to_input_items(loaded.items) == [
        {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "I will inspect the file."}],
        },
        {
            "type": "function_call",
            "call_id": "call_read",
            "name": "read_file",
            "arguments": '{"path":"a.txt"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_read",
            "output": "file contents",
        },
        {"type": "web_search_call", "id": "search_1", "status": "completed"},
    ]


def test_load_initial_history_keeps_summary_only_and_missing_model_compatible() -> None:
    """Older summary rows and model-less assistant snapshots still load."""
    anchor = ConversationItem(
        id="msg_summary_anchor",
        type="message",
        status="completed",
        response_id="resp_old",
        created_at=1,
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": "old prompt"}],
        ),
    )
    summary_only = ConversationItem(
        id="cmp_summary_only",
        type="compaction",
        status="completed",
        response_id="resp_compact",
        created_at=2,
        data=CompactionData(
            summary="the earlier work",
            last_item_id=anchor.id,
            model=None,
            token_count=7,
            compacted_messages=[
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "continued"}],
                }
            ],
        ),
    )
    loaded_snapshot = _load_initial_history(
        _ConversationStore([anchor, summary_only]), "conv_missing_model"
    )
    assert isinstance(loaded_snapshot.items[0].data, MessageData)
    assert loaded_snapshot.items[0].data.agent == "unknown"

    summary_only.data = summary_only.data.model_copy(update={"compacted_messages": None})
    loaded_summary = _load_initial_history(
        _ConversationStore([anchor, summary_only]), "conv_summary_only"
    )
    assert [
        item.data.role for item in loaded_summary.items if isinstance(item.data, MessageData)
    ] == [
        "user",
        "assistant",
    ]
    assert history_to_input_items(loaded_summary.items)[-1]["content"][0]["text"] == (
        "the earlier work"
    )


def test_load_initial_history_preserves_unsupported_snapshot_shapes_opaque() -> None:
    """Malformed IDs/content and non-chat roles are not silently rewritten."""
    anchor = ConversationItem(
        id="msg_opaque_anchor",
        type="message",
        status="completed",
        response_id="resp_old",
        created_at=1,
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": "old prompt"}],
        ),
    )
    snapshots = [
        {"type": "message", "role": "assistant", "content": {"raw": "value"}},
        {"type": "message", "role": "system", "content": "system context"},
        {
            "type": "function_call",
            "id": "response_item_id_not_call_id",
            "name": "read_file",
            "arguments": "{}",
        },
        {"type": "function_call_output", "call_id": 42, "output": "result"},
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "ok"}, 7],
        },
    ]
    compaction = ConversationItem(
        id="cmp_opaque",
        type="compaction",
        status="completed",
        response_id="resp_compact",
        created_at=2,
        data=CompactionData(
            summary="prior context",
            last_item_id=anchor.id,
            model="model-x",
            token_count=12,
            compacted_messages=snapshots,
        ),
    )

    loaded = _load_initial_history(_ConversationStore([anchor, compaction]), "conv_opaque")

    assert all(item.type == "native_tool" for item in loaded.items)
    assert [
        item.data.item for item in loaded.items if isinstance(item.data, NativeToolData)
    ] == snapshots
    assert history_to_input_items(loaded.items) == snapshots
