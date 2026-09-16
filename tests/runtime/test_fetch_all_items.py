"""Tests for ``fetch_all_items`` in ``omnigent/runtime/workflow.py``.

``fetch_all_items`` drains a conversation by paginating ``list_items`` until
``has_more`` is False, advancing the cursor to each page's ``last_id``. That
cursor-advancement invariant is position-ordering logic that a full workflow
integration test exercises only incidentally, so it gets a focused unit test
here with a store stub that hands back controlled pages.
"""

from __future__ import annotations

import pytest

from omnigent.entities.conversation import CompactionData, ConversationItem, MessageData
from omnigent.entities.pagination import PagedList
from omnigent.errors import _STALE_CURSOR_ATTEMPTS, StaleCursorError
from omnigent.runtime.workflow import _load_initial_history, fetch_all_items


def _item(item_id: str) -> ConversationItem:
    """Build a minimal persisted message item with the given id."""
    return ConversationItem(
        id=item_id,
        type="message",
        status="completed",
        response_id="resp_1",
        created_at=0,
        data=MessageData(role="user", content=[{"type": "input_text", "text": "hi"}]),
    )


class _PagedStore:
    """A ConversationStore stub that returns pre-queued pages from ``list_items``.

    Records the ``after`` cursor of each call so the test can assert the loop
    advances the cursor to the prior page's ``last_id`` rather than re-querying
    from the same position.
    """

    def __init__(self, pages: list[PagedList[ConversationItem]]) -> None:
        self._pages = pages
        self.after_calls: list[str | None] = []

    def list_items(
        self,
        conversation_id: str,
        limit: int = 100,
        after: str | None = None,
        before: str | None = None,
        order: str = "asc",
        type: str | None = None,
    ) -> PagedList[ConversationItem]:
        self.after_calls.append(after)
        return self._pages.pop(0)


def test_fetches_a_single_page_without_advancing() -> None:
    """A lone page with ``has_more=False`` returns its items and stops after one call."""
    store = _PagedStore([PagedList(data=[_item("a"), _item("b")], last_id="b", has_more=False)])
    result = fetch_all_items(store, "conv_1")
    assert [i.id for i in result] == ["a", "b"], "Single page items should pass through in order."
    assert store.after_calls == [None], (
        "One page means exactly one list_items call, starting at None."
    )


def test_paginates_until_has_more_is_false() -> None:
    """Items from every page are concatenated in order and the cursor chases ``last_id``."""
    store = _PagedStore(
        [
            PagedList(data=[_item("a"), _item("b")], last_id="b", has_more=True),
            PagedList(data=[_item("c"), _item("d")], last_id="d", has_more=True),
            PagedList(data=[_item("e")], last_id="e", has_more=False),
        ]
    )
    result = fetch_all_items(store, "conv_1")
    assert [i.id for i in result] == ["a", "b", "c", "d", "e"], (
        "All pages should be drained into one chronological list."
    )
    # Each successive call advances to the previous page's last_id.
    assert store.after_calls == [None, "b", "d"], (
        f"Cursor must chase each page's last_id, got {store.after_calls!r}."
    )


def test_honors_initial_after_cursor() -> None:
    """The starting ``after`` cursor is passed through to the first query."""
    store = _PagedStore([PagedList(data=[_item("z")], last_id="z", has_more=False)])
    fetch_all_items(store, "conv_1", after="msg_start")
    assert store.after_calls[0] == "msg_start", (
        "The initial cursor must reach the first list_items call."
    )


def test_empty_conversation_returns_empty_list() -> None:
    """An empty first page yields no items and a single query."""
    store = _PagedStore([PagedList(data=[], last_id=None, has_more=False)])
    assert fetch_all_items(store, "conv_1") == [], "An empty conversation should drain to []."
    assert store.after_calls == [None]


def test_stale_initial_cursor_propagates_after_futile_restarts() -> None:
    """A caller-supplied cursor that is already gone cannot be recovered by
    restarting — every attempt re-issues it — so it must surface for the
    caller to handle."""
    calls: list[str | None] = []

    class _DeadCursorStore:
        def list_items(
            self, conversation_id: str, **kwargs: object
        ) -> PagedList[ConversationItem]:
            calls.append(kwargs.get("after"))  # type: ignore[arg-type]
            raise StaleCursorError(conversation_id)

    with pytest.raises(StaleCursorError):
        fetch_all_items(_DeadCursorStore(), "conv_1", after="msg_gone")
    assert calls == ["msg_gone"] * _STALE_CURSOR_ATTEMPTS, (
        f"Every attempt re-issues the dead seed unchanged, got {calls!r}."
    )


class _CompactionStore:
    """Store stub whose compaction anchor no longer resolves.

    ``list_items`` answers the ``type="compaction"`` probe with one compaction
    item, raises :class:`StaleCursorError` for the anchored read, and drains
    the whole conversation when asked without a cursor.
    """

    def __init__(self, anchor: str) -> None:
        self._anchor = anchor
        self.cursors: list[str | None] = []

    def list_items(
        self,
        conversation_id: str,
        limit: int = 100,
        after: str | None = None,
        before: str | None = None,
        order: str = "asc",
        type: str | None = None,
    ) -> PagedList[ConversationItem]:
        if type == "compaction":
            item = ConversationItem(
                id="cmp_1",
                type="compaction",
                status="completed",
                response_id="resp_1",
                created_at=1,
                data=CompactionData(
                    summary="earlier turns",
                    last_item_id=self._anchor,
                    token_count=10,
                ),
            )
            return PagedList(data=[item], last_id=item.id, has_more=False)
        self.cursors.append(after)
        if after is not None:
            raise StaleCursorError(conversation_id)
        return PagedList(data=[_item("a"), _item("b")], last_id="b", has_more=False)


def test_deleted_compaction_anchor_falls_back_to_full_history() -> None:
    """A deleted anchor makes the "everything after it" slice unrecoverable,
    so the loader reloads the whole conversation instead of raising."""
    store = _CompactionStore(anchor="msg_gone")
    loaded = _load_initial_history(store, "conv_1")
    assert [i.id for i in loaded.items] == ["a", "b"], (
        "The full conversation should stand in for the unrecoverable slice."
    )
    assert loaded.last_compaction_created_at is None, (
        "The summary was not used, so it must not be reported as the boundary."
    )
    assert store.cursors == ["msg_gone"] * _STALE_CURSOR_ATTEMPTS + [None], (
        "Expected the anchored read (restarted futilely) then one full "
        f"reload, got {store.cursors!r}."
    )
