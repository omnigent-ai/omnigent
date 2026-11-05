"""Isolate every test under tests/host from inherited proxy settings."""

import pytest

from tests._helpers.proxy_env import clear_proxy_env


@pytest.fixture(autouse=True)
def _no_ambient_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_proxy_env(monkeypatch)
