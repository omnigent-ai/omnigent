"""Registry tests for the Cursor harness."""

from __future__ import annotations

from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES


def test_cursor_harness_registered() -> None:
    assert _HARNESS_MODULES.get("cursor") == "omnigent.inner.cursor_harness"


def test_cursor_harness_accepted_by_spec_compat() -> None:
    assert "cursor" in OMNIGENT_HARNESSES
