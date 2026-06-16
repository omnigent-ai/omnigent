"""Registry tests for the Antigravity CLI harness."""

from __future__ import annotations

from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES


def test_agy_harness_registered() -> None:
    assert _HARNESS_MODULES.get("agy") == "omnigent.inner.agy_harness"


def test_agy_harness_accepted_by_spec_compat() -> None:
    assert "agy" in OMNIGENT_HARNESSES
    assert "gemini" not in OMNIGENT_HARNESSES
