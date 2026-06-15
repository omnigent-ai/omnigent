"""Registry tests for the Gemini harness."""

from __future__ import annotations

from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES


def test_gemini_harness_registered() -> None:
    """The runtime resolves ``"gemini"`` to the harness wrap module.

    Drift here means a spec with ``harness: gemini`` fails at runner spawn
    with a generic "unknown harness" error instead of launching.
    """
    assert _HARNESS_MODULES.get("gemini") == "omnigent.inner.gemini_harness"


def test_gemini_harness_accepted_by_spec_compat() -> None:
    """The spec-load allowlist accepts ``harness: gemini``.

    A spec with the value missing from this set fails loud at parse with a
    "must be one of […], got 'gemini'" error before it ever reaches the
    runtime — same regression that hit ``open-responses``.
    """
    assert "gemini" in OMNIGENT_HARNESSES
