"""Tests for pagination entity and paginate_in_memory helper."""

from __future__ import annotations

from omnigent.entities.pagination import PagedList, paginate_in_memory

# ── PagedList ─────────────────────────────────────────


def test_paged_list_defaults() -> None:
    page = PagedList()
    assert page.data == []
    assert page.first_id is None
    assert page.last_id is None
    assert page.has_more is False


def test_paged_list_independent_defaults() -> None:
    """Each PagedList gets its own data list (no mutable-default footgun)."""
    a = PagedList()
    b = PagedList()
    a.data.append("x")
    assert b.data == []


# ── paginate_in_memory ────────────────────────────────


_ITEMS = [
    {"id": "1", "name": "first"},
    {"id": "2", "name": "second"},
    {"id": "3", "name": "third"},
    {"id": "4", "name": "fourth"},
    {"id": "5", "name": "fifth"},
]


def _id_fn(item: dict) -> str:
    return item["id"]


def test_paginate_asc_no_cursor() -> None:
    result = paginate_in_memory(_ITEMS, _id_fn, limit=3, order="asc")
    assert len(result.data) == 3
    assert result.first_id == "1"
    assert result.last_id == "3"
    assert result.has_more is True


def test_paginate_asc_all_fit() -> None:
    result = paginate_in_memory(_ITEMS, _id_fn, limit=10, order="asc")
    assert len(result.data) == 5
    assert result.has_more is False
    assert result.first_id == "1"
    assert result.last_id == "5"


def test_paginate_desc_no_cursor() -> None:
    result = paginate_in_memory(_ITEMS, _id_fn, limit=3, order="desc")
    assert len(result.data) == 3
    assert result.first_id == "5"
    assert result.last_id == "3"
    assert result.has_more is True


def test_paginate_after_cursor_asc() -> None:
    result = paginate_in_memory(_ITEMS, _id_fn, limit=2, after="2", order="asc")
    assert [_id_fn(i) for i in result.data] == ["3", "4"]
    assert result.has_more is True


def test_paginate_after_cursor_exhausts() -> None:
    result = paginate_in_memory(_ITEMS, _id_fn, limit=10, after="3", order="asc")
    assert [_id_fn(i) for i in result.data] == ["4", "5"]
    assert result.has_more is False


def test_paginate_before_cursor_asc() -> None:
    result = paginate_in_memory(_ITEMS, _id_fn, limit=10, before="4", order="asc")
    assert [_id_fn(i) for i in result.data] == ["1", "2", "3"]
    assert result.has_more is False


def test_paginate_after_and_before() -> None:
    """Both cursors narrow the window."""
    result = paginate_in_memory(_ITEMS, _id_fn, limit=10, after="1", before="5", order="asc")
    assert [_id_fn(i) for i in result.data] == ["2", "3", "4"]


def test_paginate_empty_list() -> None:
    result = paginate_in_memory([], _id_fn, limit=10, order="asc")
    assert result.data == []
    assert result.first_id is None
    assert result.last_id is None
    assert result.has_more is False


def test_paginate_cursor_not_found() -> None:
    """Unknown cursor id is silently ignored (no items skipped)."""
    result = paginate_in_memory(_ITEMS, _id_fn, limit=10, after="999", order="asc")
    assert len(result.data) == 5


def test_paginate_limit_one() -> None:
    result = paginate_in_memory(_ITEMS, _id_fn, limit=1, order="asc")
    assert len(result.data) == 1
    assert result.first_id == "1"
    assert result.last_id == "1"
    assert result.has_more is True


def test_paginate_desc_after_cursor() -> None:
    """After cursor in desc order — items appear reversed, cursor still works."""
    result = paginate_in_memory(_ITEMS, _id_fn, limit=2, after="4", order="desc")
    # desc reverses to [5,4,3,2,1]; after "4" gives [3,2,1]; limit 2 = [3,2]
    assert [_id_fn(i) for i in result.data] == ["3", "2"]
    assert result.has_more is True


def test_paginate_desc_before_cursor() -> None:
    result = paginate_in_memory(_ITEMS, _id_fn, limit=10, before="3", order="desc")
    # desc reverses to [5,4,3,2,1]; before "3" gives [5,4]
    assert [_id_fn(i) for i in result.data] == ["5", "4"]
