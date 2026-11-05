"""Isolation from proxy variables inherited by the test process."""

from __future__ import annotations

import os

import pytest

_PROXY_VARIABLES = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}


def clear_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Unset every capitalisation of the proxy variables the tunnel selector reads.

    :param monkeypatch: The test's monkeypatch fixture; it restores the variables.
    """
    for name in list(os.environ):
        if name.lower() in _PROXY_VARIABLES:
            monkeypatch.delenv(name, raising=False)
