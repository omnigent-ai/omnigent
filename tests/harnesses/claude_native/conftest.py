"""Shared fixtures for the Claude-native harness tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _ignore_ambient_claude_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Keep a developer's ``CLAUDE_CONFIG_DIR`` away from transcript reads and writes.

    Tests that need a Claude config dir set one themselves.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
