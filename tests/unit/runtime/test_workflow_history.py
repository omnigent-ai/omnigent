"""Tests for runtime conversation-history loading."""

from __future__ import annotations

from typing import Any

from omnigent.entities import ConversationItem, MessageData, PagedList, SlashCommandData
from omnigent.entities.pagination import paginate_in_memory
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
