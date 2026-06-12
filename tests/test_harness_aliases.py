"""Tests for omnigent.harness_aliases."""

from __future__ import annotations

import pytest

from omnigent.harness_aliases import canonicalize_harness, is_native_harness


@pytest.mark.parametrize(
    "alias,canonical",
    [
        ("claude", "claude-sdk"),
        # Docs / runtime-dispatch spelling of the openai-agents harness;
        # specs and OMNIGENT_HARNESSES use "openai-agents".
        ("openai-agents-sdk", "openai-agents"),
        # Canonical names pass through unchanged.
        ("openai-agents", "openai-agents"),
        ("pi", "pi"),
        # Unknown names return unchanged so callers keep their own errors.
        ("bogus", "bogus"),
        (None, None),
    ],
)
def test_canonicalize_harness(alias: str | None, canonical: str | None) -> None:
    """Alias spellings map to canonical ids; everything else passes through.

    A missing ``openai-agents-sdk`` mapping breaks the documented
    ``omnigent run ... --harness openai-agents-sdk`` invocation at
    ``_validate_harness``.
    """
    assert canonicalize_harness(alias) == canonical


@pytest.mark.parametrize(
    "harness,expected",
    [
        # Canonical native spellings and their reversed forms.
        ("claude-native", True),
        ("codex-native", True),
        ("native-claude", True),
        ("native-codex", True),
        # SDK harnesses are NOT native — they replay the Omnigent
        # transcript and don't own an on-disk runtime transcript. A
        # regression that classified these as native would wrongly route a
        # fork into the native-rebuild path.
        ("claude-sdk", False),
        ("claude_sdk", False),
        ("openai-agents", False),
        ("agents_sdk", False),
        ("codex", False),
        # The "claude" shorthand canonicalizes to claude-sdk (not native).
        ("claude", False),
        ("some-unknown-harness", False),
        (None, False),
    ],
)
def test_is_native_harness(harness: str | None, expected: bool) -> None:
    """``is_native_harness`` flags only the native CLI harnesses.

    The fork agent-switch gates the native transcript-rebuild path on this:
    only native targets need a rebuild (SDK targets carry history as
    context on their own). Misclassifying either way breaks history
    carry-over on a switch.
    """
    assert is_native_harness(harness) is expected
