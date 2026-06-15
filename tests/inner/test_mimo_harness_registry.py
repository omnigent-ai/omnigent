"""Registry tests for the Mimo harness."""

from __future__ import annotations

from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES


def test_mimo_harness_registered() -> None:
    assert _HARNESS_MODULES.get("mimo") == "omnigent.inner.mimo_harness"


def test_mimo_harness_accepted_by_spec_compat() -> None:
    assert "mimo" in OMNIGENT_HARNESSES
