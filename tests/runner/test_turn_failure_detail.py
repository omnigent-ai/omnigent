"""The runner's turn-failure messages must never drop the cause.

Messages like ``f"turn setup failed: {exc}"`` published on a ``failed``
status edge are the only reason a user (or the server's broken-turn ERROR
log) ever sees. An exception whose ``str()`` is empty -- a bare
``CancelledError`` or ``RuntimeError()`` -- used to leave nothing after the
prefix, so the published detail read ``turn setup failed: `` and the actual
cause was dropped. ``_exception_detail`` is the guard: it falls back to the
exception class name so the detail always carries something diagnosable.
"""

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
