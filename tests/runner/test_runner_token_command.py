"""Tests for the runner's explicit refresh-command credential source."""

from __future__ import annotations

import sys

import pytest

from omnigent.runner._entry import _make_auth_token_factory


def test_make_auth_token_factory_prefers_configured_token_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit command supplies a fresh Apps-proxy bearer per call."""
    monkeypatch.setenv(
        "OMNIGENT_DATABRICKS_TOKEN_COMMAND",
        f"{sys.executable} -c \"print('broker-token')\"",
    )
    monkeypatch.setattr(
        "omnigent.inner.databricks_executor._resolve_databricks_auth",
        lambda **_kwargs: pytest.fail("SDK auth must not shadow the explicit command"),
    )

    factory = _make_auth_token_factory(server_url="https://omnigent.example.com")

    assert factory is not None
    assert factory() == "broker-token"
