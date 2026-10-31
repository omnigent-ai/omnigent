"""Isolate every test under tests/host from inherited proxy settings."""

import os

import pytest

_PROXY_VARIABLES = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}


@pytest.fixture(autouse=True)
def _no_ambient_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Any capitalisation is honored by the proxy selector, so strip them all.
    for name in list(os.environ):
        if name.lower() in _PROXY_VARIABLES:
            monkeypatch.delenv(name, raising=False)
