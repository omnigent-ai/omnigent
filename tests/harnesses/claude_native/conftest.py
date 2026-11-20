"""Shared fixtures for the Claude-native harness tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _default_to_personal_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guidance tests assume a person's own host unless they set ``IS_SANDBOX=1``."""
    monkeypatch.delenv("IS_SANDBOX", raising=False)


@pytest.fixture(autouse=True)
def _ignore_ambient_claude_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Keep a developer's ``CLAUDE_CONFIG_DIR`` away from transcript reads and writes.

    Tests that need a Claude config dir set one themselves.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


@pytest.fixture
def claude_settings_validator(monkeypatch: pytest.MonkeyPatch):
    """Stub vendor startup in scenarios focused on settings composition or transport."""
    from unittest.mock import AsyncMock

    from omnigent.harnesses.claude_native import settings_validation

    validator = AsyncMock()
    monkeypatch.setattr(settings_validation, "validate_caller_settings", validator)
    return validator
