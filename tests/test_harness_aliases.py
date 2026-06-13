"""Tests for omnigent.harness_aliases."""

from __future__ import annotations

import pytest

from omnigent.harness_aliases import canonicalize_harness, is_native_harness


@pytest.mark.parametrize(
    "alias,canonical",
    [
        ("claude", "claude-sdk"),
        ("native-pi", "pi-native"),
        # Docs / runtime-dispatch spelling of the openai-agents harness;
        # specs and OMNIGENT_HARNESSES use "openai-agents".
        ("openai-agents-sdk", "openai-agents"),
        # Canonical names pass through unchanged.
        ("openai-agents", "openai-agents"),
        ("pi", "pi"),
        # Canonical cursor id passes through unchanged (no alias).
        ("cursor", "cursor"),
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
        ("pi-native", True),
        ("native-pi", True),
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
        # cursor is a headless ACP harness, not a native CLI bridge.
        ("cursor", False),
        ("some-unknown-harness", False),
        (None, False),
    ],
)
def test_is_native_harness(harness: str | None, expected: bool) -> None:
    """``is_native_harness`` flags only the native CLI harnesses.

    The runner gates terminal-owned turn sequencing and history replay on
    this. Misclassifying either way makes native TUI sessions behave like
    in-process SDK turns, or vice versa.
    """
    assert is_native_harness(harness) is expected
