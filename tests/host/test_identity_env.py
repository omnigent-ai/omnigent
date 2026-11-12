"""Tests for the managed-sandbox environment predicate."""

from __future__ import annotations

import pytest

from omnigent.host.identity_env import MANAGED_SANDBOX_ENV_VAR, in_managed_sandbox


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        ("", False),
        ("0", False),
        ("true", False),
        ("1", True),
        (" 1 ", True),
    ],
)
def test_in_managed_sandbox_reads_the_sandbox_marker(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool
) -> None:
    """Only ``IS_SANDBOX=1`` marks a managed sandbox; surrounding whitespace is tolerated."""
    if value is None:
        monkeypatch.delenv(MANAGED_SANDBOX_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(MANAGED_SANDBOX_ENV_VAR, value)
    assert in_managed_sandbox() is expected
