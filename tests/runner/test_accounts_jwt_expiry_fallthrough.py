"""Verify that expired accounts JWTs do not select Databricks auth."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner._entry import (
    _make_auth_token_factory,
    _RunnerDatabricksAuth,
)


def _no_databricks_creds(*args: Any, **kwargs: Any) -> tuple[Any, str]:
    """Stand-in for _resolve_databricks_auth in a no-Databricks deployment."""
    from omnigent.inner.databricks_executor import DatabricksAuthError

    raise DatabricksAuthError("no Databricks credentials configured")


@pytest.fixture()
def _accounts_only_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> str:
    """Isolate auth state and remove non-accounts credential paths."""
    server_url = "https://omnigent.example.com"
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RUNNER_SERVER_URL", server_url)
    monkeypatch.delenv("OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN", raising=False)
    monkeypatch.delenv("OMNIGENT_RUNNER_DELEGATED_AUTH", raising=False)
    monkeypatch.delenv("OMNIGENT_RUNNER_INITIAL_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("RUNNER_INITIAL_AUTH_TOKEN", raising=False)
    for var in list(__import__("os").environ):
        if var.startswith("DATABRICKS_"):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        "omnigent.inner.databricks_executor._resolve_databricks_auth",
        _no_databricks_creds,
    )
    monkeypatch.setattr("omnigent.runner._entry._runner_auth_factory", None)
    return server_url


def test_factory_returns_none_when_accounts_jwt_expires(
    _accounts_only_env: str,
) -> None:
    """Return no factory for an already-expired accounts JWT."""
    from omnigent.cli_auth import store_token

    server_url = _accounts_only_env

    store_token(
        server_url,
        token="expired-accounts-jwt",
        user_id="alice@example.com",
        expires_at=time.time() - 1,
    )

    factory = _make_auth_token_factory(server_url=server_url)

    assert factory is None, (
        "_make_auth_token_factory must return None when the accounts JWT has "
        "expired and no other credential (Databricks, managed-mint) is available."
    )


def test_factory_callable_returns_none_when_accounts_jwt_expires_mid_run(
    _accounts_only_env: str,
) -> None:
    """Return no token when an existing factory's accounts JWT expires."""
    from omnigent.cli_auth import store_token

    server_url = _accounts_only_env

    store_token(
        server_url,
        token="valid-accounts-jwt",
        user_id="alice@example.com",
        expires_at=time.time() + 3600,
    )

    factory = _make_auth_token_factory(server_url=server_url)
    assert factory is not None, "factory must be created while JWT is valid"
    assert factory() == "valid-accounts-jwt"

    store_token(
        server_url,
        token="valid-accounts-jwt",
        user_id="alice@example.com",
        expires_at=time.time() - 1,
    )

    result = factory()
    assert result is None, (
        "factory() must return None after the accounts JWT expires; "
        "it must not fall through to Databricks SDK and raise an error."
    )


def test_auth_flow_error_names_accounts_expiry_not_databricks(
    _accounts_only_env: str,
) -> None:
    """Do not blame Databricks when accounts auth expires."""
    from omnigent.cli_auth import store_token

    server_url = _accounts_only_env

    store_token(
        server_url,
        token="valid-accounts-jwt",
        user_id="alice@example.com",
        expires_at=time.time() + 3600,
    )
    factory = _make_auth_token_factory(server_url=server_url)
    assert factory is not None

    store_token(
        server_url,
        token="valid-accounts-jwt",
        user_id="alice@example.com",
        expires_at=time.time() - 1,
    )

    auth = _RunnerDatabricksAuth(factory, server_url=server_url)
    request = httpx.Request("GET", server_url + "/v1/health")
    flow = auth.auth_flow(request)

    try:
        next(flow)
        # A bare request is also valid; the server can reject it honestly.
        return
    except httpx.RequestError as exc:
        assert "Databricks" not in str(exc), (
            "expired accounts JWT must not surface a Databricks error in an "
            f"accounts-only deployment; got: {exc!r}"
        )
