"""Credential recovery does not require an unrelated executor to import."""

from __future__ import annotations

import sys
import time

import pytest

from omnigent import cli_auth
from omnigent.runner import _entry
from omnigent.runner.identity import RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR


@pytest.mark.parametrize("credential", ["stored_oidc", "refreshed_oidc", "delegated", "managed"])
def test_rejected_bootstrap_recovers_when_executor_import_fails(
    monkeypatch: pytest.MonkeyPatch, credential: str
) -> None:
    """An unavailable SDK dependency cannot block another configured credential."""
    monkeypatch.setattr(_entry, "_runner_auth_factory", None)
    monkeypatch.setenv("RUNNER_SERVER_URL", "https://server.example.invalid")
    monkeypatch.setenv(RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR, "bootstrap-token")
    monkeypatch.delenv("OMNIGENT_RUNNER_DELEGATED_AUTH", raising=False)
    monkeypatch.delenv("OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN", raising=False)

    # Isolate credential storage and the remote mint service from developer auth.
    monkeypatch.setattr(
        cli_auth,
        "load_token",
        lambda *_args, **_kwargs: "renewed-token" if credential == "stored_oidc" else None,
    )
    monkeypatch.setattr(
        cli_auth,
        "refresh_stored_token",
        lambda *_args, **_kwargs: "renewed-token" if credential == "refreshed_oidc" else None,
    )
    if credential in {"delegated", "managed"}:
        monkeypatch.setenv("OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN", "binding-token")
        monkeypatch.setattr(
            _entry,
            "_mint_managed_owner_token",
            lambda *_args, **_kwargs: ("renewed-token", time.time() + 3600),
        )
    if credential == "delegated":
        monkeypatch.setenv("OMNIGENT_RUNNER_DELEGATED_AUTH", "1")

    factory = _entry._make_auth_token_factory()
    assert isinstance(factory, _entry._InitialAuthTokenFactory)
    assert factory() == "bootstrap-token"

    # A long-lived runner may first import this module during token renewal.
    monkeypatch.setitem(sys.modules, "omnigent.inner.databricks_executor", None)
    assert factory.invalidate()
    assert factory() == "renewed-token"
    assert factory() == "renewed-token"


def test_unavailable_sdk_without_an_alternative_does_not_produce_a_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_entry, "_runner_auth_factory", None)
    monkeypatch.setenv("RUNNER_SERVER_URL", "https://server.example.invalid")
    monkeypatch.delenv(RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv("OMNIGENT_RUNNER_DELEGATED_AUTH", raising=False)
    monkeypatch.delenv("OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN", raising=False)
    monkeypatch.setattr(cli_auth, "load_token", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_auth, "refresh_stored_token", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(sys.modules, "omnigent.inner.databricks_executor", None)

    assert _entry._make_auth_token_factory() is None
