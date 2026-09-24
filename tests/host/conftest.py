"""Shared fixtures for the host daemon tests."""

from __future__ import annotations

import pytest

_PROXY_ENV_VARS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")


@pytest.fixture(autouse=True)
def _no_ambient_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip ambient proxy env so host tunnel connects dial exactly as asserted.

    The host tunnel honors ``http_proxy``/``https_proxy``/``all_proxy`` like
    every other client; a developer machine or CI runner with those set would
    otherwise reroute the test tunnels. Tests that exercise the proxy path
    set the variables explicitly.
    """
    for name in _PROXY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
