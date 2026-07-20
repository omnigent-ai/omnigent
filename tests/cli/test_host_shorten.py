"""Tests for omnigent.cli._host_shorten — terminal display truncation."""

from __future__ import annotations

import pytest

from omnigent.cli import _host_shorten


@pytest.mark.parametrize("max_chars", [0, 1, 2, 3, 4, 5, 10, 24])
def test_host_shorten_never_exceeds_max_chars(max_chars: int) -> None:
    """The result must never overflow the requested display width.

    ``max_chars == 2`` used to overflow to 3 chars (head + ellipsis + tail),
    breaking the function's own contract for that one width.
    """
    result = _host_shorten("abcdefghij", max_chars=max_chars)
    assert len(result) <= max_chars


def test_host_shorten_width_two_uses_plain_slice() -> None:
    """A two-character budget uses a plain slice without an ellipsis."""
    assert _host_shorten("abcdefghij", max_chars=2) == "ab"


def test_host_shorten_fits_under_max_returns_unchanged() -> None:
    """A value already within budget is returned as-is."""
    assert _host_shorten("short", max_chars=24) == "short"


def test_host_shorten_truncates_with_ellipsis_when_room_allows() -> None:
    """Widths of 3+ get a head...tail split around an ellipsis."""
    assert _host_shorten("abcdefghij", max_chars=5) == "ab…ij"
