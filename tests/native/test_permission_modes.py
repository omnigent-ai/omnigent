"""Permission modes a created session may request."""

from __future__ import annotations

import pytest

from omnigent.native.permission_modes import (
    APPROVAL_SKIPPING_PERMISSION_MODES,
    PERMISSION_LAUNCH_ARGS,
    created_session_launch_args,
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
