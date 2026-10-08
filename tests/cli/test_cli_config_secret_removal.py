"""Tests for the remove-key status line in the setup menus."""

from __future__ import annotations

import pytest

from omnigent.cli_config import _secret_removal_status


@pytest.mark.parametrize(
    ("unresolved", "expected"),
    [
        ("", "\u2713 Removed Cursor API key"),
        (
            "the OS keychain still holds the secret",
            "\u26a0 Removed Cursor API key from config; the OS keychain still holds the secret",
        ),
    ],
    ids=["clean", "unresolved"],
)
def test_secret_removal_status(unresolved: str, expected: str) -> None:
    assert _secret_removal_status("Cursor API key", unresolved) == expected
