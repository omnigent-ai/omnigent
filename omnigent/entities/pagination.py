"""Cursor-based pagination container."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class PagedList(Generic[T]):
    """
    A page of results matching the OpenAI list pagination shape.

    :param data: The items in this page.
    :param first_id: ID of the first item in the page, or ``None``
        if empty.
    :param last_id: ID of the last item in the page, or ``None``
        if empty.
    :param has_more: ``True`` if more pages exist after this one.
    """

    data: list[T] = field(default_factory=list)
    first_id: str | None = None
    last_id: str | None = None
    has_more: bool = False


def paginate_in_memory(
    items: list[T],
    id_fn: Callable[[T], str],
    *,
    limit: int = 20,
    after: str | None = None,
    before: str | None = None,
    order: str = "desc",
) -> PagedList[T]:
    """Apply cursor-based pagination to an in-memory list.

    Items should already be in a stable order. The function slices
    using ``after``/``before`` cursors keyed by item id and returns
    at most ``limit`` items.

    Forward pagination (``after``) returns up to ``limit`` items
    immediately after the cursor. Backward pagination (``before``)
    returns up to ``limit`` items immediately *before* the cursor,
    anchored to the end of the range — not the first page. ``has_more``
    reports whether more items remain in the pagination direction:
    after the page for ``after``, before the page for ``before``. An
    unknown cursor id is ignored.

    :param items: Pre-sorted items to paginate.
    :param id_fn: Callable that extracts the id from an item.
    :param limit: Maximum items to return, default 20.
    :param after: Cursor id — return items after this one.
    :param before: Cursor id — return items before this one.
    :param order: ``"desc"`` reverses the list, ``"asc"`` keeps it.
    :returns: A paginated :class:`PagedList`.
    """
    working = list(items)
    if order == "desc":
        working = list(reversed(working))

    # Resolve the cursors to a ``[start, end)`` window over ``working``.
    # An unknown cursor leaves its bound untouched (it is ignored).
    start = 0
    end = len(working)

    if after is not None:
        idx = next(
            (i for i, item in enumerate(working) if id_fn(item) == after),
            None,
        )
        if idx is not None:
            start = idx + 1

    before_found = False
    if before is not None:
        idx = next(
            (i for i, item in enumerate(working) if id_fn(item) == before),
            None,
        )
        if idx is not None:
            end = idx
            before_found = True

    if before_found:
        # Backward: the ``limit`` items immediately preceding the cursor,
        # anchored to the end of the window (not the first page).
        page_start = max(start, end - limit)
        page = working[page_start:end]
        has_more = page_start > start
    else:
        # Forward, or no/unknown cursor: the first ``limit`` items.
        page = working[start:end][:limit]
        has_more = end - start > limit

    return PagedList(
        data=page,
        first_id=id_fn(page[0]) if page else None,
        last_id=id_fn(page[-1]) if page else None,
        has_more=has_more,
    )
