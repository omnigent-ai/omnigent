"""Permission modes a created session may request."""

from __future__ import annotations

import pytest

from omnigent.native.permission_modes import (
    APPROVAL_SKIPPING_PERMISSION_MODES,
    PERMISSION_LAUNCH_ARGS,
    PERMISSION_MODE_CONCEPTS,
    created_session_launch_args,
    inherited_permission_mode,
)

_GATED = [
    (harness, mode)
    for harness, modes in PERMISSION_LAUNCH_ARGS.items()
    for mode in modes
    if mode not in APPROVAL_SKIPPING_PERMISSION_MODES
]
_SKIPPING = [
    (harness, mode)
    for harness, modes in PERMISSION_LAUNCH_ARGS.items()
    for mode in modes
    if mode in APPROVAL_SKIPPING_PERMISSION_MODES
]


@pytest.mark.parametrize(("harness", "mode"), _GATED)
def test_modes_that_keep_approval_use_the_automation_launch_args(harness: str, mode: str) -> None:
    assert created_session_launch_args(harness, mode) == (
        list(PERMISSION_LAUNCH_ARGS[harness][mode]) or None
    )


@pytest.mark.parametrize(("harness", "mode"), _SKIPPING)
def test_modes_that_skip_approval_are_refused(harness: str, mode: str) -> None:
    # Every harness has one, so the refusal can't be bypassed by picking another harness.
    with pytest.raises(ValueError, match="skips approval"):
        created_session_launch_args(harness, mode)


def test_refusal_names_the_harness_choices() -> None:
    with pytest.raises(ValueError) as error:
        created_session_launch_args("codex-native", "auto")
    message = str(error.value)
    assert "is not a mode of 'codex-native'" in message
    assert "'approve-for-me'" in message
    assert "'full-access'" not in message


@pytest.mark.parametrize("harness", ["claude-sdk", None])
def test_harness_without_modes_is_refused(harness: str | None) -> None:
    with pytest.raises(ValueError, match="omit it"):
        created_session_launch_args(harness, "default")


@pytest.mark.parametrize(
    ("parent_harness", "parent_mode", "child_harness", "expected"),
    [
        ("claude-native", "auto", "claude-native", "auto"),
        ("claude-native", "auto", "codex-native", "approve-for-me"),
        ("claude-native", "auto", "cursor-native", "auto-review"),
        ("codex-native", "approve-for-me", "claude-native", "auto"),
        ("claude-native", "acceptEdits", "claude-native", "acceptEdits"),
        ("codex-native", "ask-for-approval", "codex-native", "ask-for-approval"),
        # No mode of the child's harness has the same meaning.
        ("claude-native", "acceptEdits", "codex-native", None),
        ("codex-native", "ask-for-approval", "claude-native", None),
        ("claude-native", "auto", "antigravity-native", None),
        ("claude-native", "auto", "claude-sdk", None),
        ("claude-native", "auto", None, None),
        # A child starts at its own default rather than a parent's.
        ("claude-native", "default", "claude-native", None),
        ("codex-native", "default", "claude-native", None),
        # A planning child would stop to ask before its first edit.
        ("claude-native", "plan", "codex-native", None),
        ("codex-native", "read-only", "codex-native", None),
        ("claude-native", "not-a-mode", "claude-native", None),
    ],
)
def test_inherited_permission_mode(
    parent_harness: str, parent_mode: str, child_harness: str | None, expected: str | None
) -> None:
    assert inherited_permission_mode(parent_harness, parent_mode, child_harness) == expected


@pytest.mark.parametrize(
    ("harness", "mode"),
    [
        (harness, mode)
        for harness, modes in PERMISSION_MODE_CONCEPTS.items()
        for mode in modes
        if mode in APPROVAL_SKIPPING_PERMISSION_MODES
    ],
)
@pytest.mark.parametrize("child_harness", ["claude-native", "codex-native", "cursor-native"])
def test_a_mode_that_skips_approval_is_not_inherited(
    harness: str, mode: str, child_harness: str
) -> None:
    assert inherited_permission_mode(harness, mode, child_harness) is None
