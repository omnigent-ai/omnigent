"""Directory fixtures for the claude-native adapter tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _default_to_personal_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guidance tests assume a person's own host unless they set ``IS_SANDBOX=1``."""
    monkeypatch.delenv("IS_SANDBOX", raising=False)
