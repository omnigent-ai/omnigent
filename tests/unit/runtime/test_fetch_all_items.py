"""Tests for ``fetch_all_items`` in ``omnigent/runtime/workflow.py``.

``fetch_all_items`` drains a conversation by paginating ``list_items`` until
``has_more`` is False, advancing the cursor to each page's ``last_id``. That
cursor-advancement invariant is position-ordering logic that a full workflow
integration test exercises only incidentally, so it gets a focused unit test
here with a store stub that hands back controlled pages.
"""

from __future__ import annotations

from omnigent.entities.conversation import ConversationItem, MessageData
from omnigent.entities.pagination import PagedList
from omnigent.runtime.workflow import fetch_all_items


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
