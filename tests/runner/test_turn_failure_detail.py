"""Tests for non-empty turn-failure details."""

from __future__ import annotations

import asyncio

from omnigent.runner.app import _exception_detail


def test_exception_detail_keeps_nonempty_text() -> None:
    """A normal exception's own text passes through unchanged."""
    assert _exception_detail(RuntimeError("boom")) == "boom"


def test_exception_detail_falls_back_to_class_name_when_str_is_empty() -> None:
    """An empty ``str(exc)`` yields the class name, not an empty reason."""
    assert _exception_detail(asyncio.CancelledError()) == "CancelledError"
    assert _exception_detail(RuntimeError()) == "RuntimeError"


def test_exception_detail_treats_whitespace_only_text_as_empty() -> None:
    """Whitespace-only exception text classifies as empty too."""
    assert _exception_detail(ValueError("   ")) == "ValueError"
