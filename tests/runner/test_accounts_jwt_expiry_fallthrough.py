"""Expired accounts JWTs must not fall through to Databricks auth.

Bug: when a runner is started in ``accounts`` auth mode (no Databricks),
the stored accounts JWT expires while the runner is still alive.  The
runner's auth factory calls ``load_token`` (which returns ``None`` on
expiry), then falls through to the Databricks SDK path.  Because no
Databricks credential is configured, the SDK returns nothing and
``_RunnerDatabricksAuth.auth_flow`` raises:

    httpx.RequestError: Databricks token refresh returned no token

That error is both misleading (Databricks is not involved) and fatal for
every runner->server callback until the host is restarted.

The correct behaviour is:
- ``_make_auth_token_factory`` returns ``None`` (no valid credential) when
  the accounts JWT has expired *and* no other credential path is available.
- ``_RunnerDatabricksAuth.auth_flow`` still fails closed on a
  ``None``-returning factory, but the surfaced error must not name
  Databricks in a pure-accounts deployment -- it should say the Omnigent
  login expired (and how to renew it).  Captured here so the fix has a
  precise fail->pass target.

Journey reproduced
------------------
1. Server is configured with ``OMNIGENT_AUTH_PROVIDER=accounts``.
2. User runs ``omni login <server-url>``, storing a session JWT in
   ``<data-dir>/auth_tokens.json``.
3. A persistent host is started with ``omni host --server <server-url>``.
4. The stored accounts JWT expires (``expires_at`` is now in the past).
5. A new runner callback fires (any: policy, event forwarding, etc.).

Expected: the runner surfaces a clear "accounts login expired" error, or
at minimum does NOT route through the Databricks token path.
Actual: ``httpx.RequestError: Databricks token refresh returned no token``.
"""

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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _no_databricks_creds(*args: Any, **kwargs: Any) -> tuple[Any, str]:
    """Stand-in for _resolve_databricks_auth in a no-Databricks deployment."""
    from omnigent.inner.databricks_executor import DatabricksAuthError

    raise DatabricksAuthError("no Databricks credentials configured")


@pytest.fixture()
def _accounts_only_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> str:
    """Isolate auth state and remove every non-accounts credential path.

    Mirrors the reporter's environment: accounts auth only, no
    ``~/.databrickscfg``, no ``DATABRICKS_*`` env, no host-delegated or
    initial bearer, no managed-mint binding token.

    :param monkeypatch: Pytest environment patch fixture.
    :param tmp_path: Temporary directory used as an isolated data dir.
    :returns: The server URL the stored accounts JWT is keyed by.
    """
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_factory_returns_none_when_accounts_jwt_expires(
    _accounts_only_env: str,
) -> None:
    """``_make_auth_token_factory`` returns ``None`` after the accounts JWT expires.

    A factory that returns ``None`` signals "no credential available".
    The runner must not reach the Databricks SDK path when the only
    stored record is an accounts JWT that has now expired.

    :param _accounts_only_env: Isolated accounts-only environment fixture.
    :returns: None.
    """
    from omnigent.cli_auth import store_token

    server_url = _accounts_only_env

    # Write an already-expired accounts JWT (``expires_at`` in the past).
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
    """After a valid JWT expires, the *existing* factory returns ``None``.

    The factory is built once at runner startup (when the JWT is still
    valid).  The JWT then expires while the runner is alive.  Subsequent
    ``factory()`` calls must return ``None``, not raise.

    This captures the core mechanism: the ``_factory`` closure inside
    ``_make_auth_token_factory`` calls ``load_token`` on every invocation,
    so a live expiry is visible without restarting the runner.

    :param _accounts_only_env: Isolated accounts-only environment fixture.
    :returns: None.
    """
    from omnigent.cli_auth import store_token

    server_url = _accounts_only_env

    # Write a still-valid accounts JWT so the factory creation probe passes.
    store_token(
        server_url,
        token="valid-accounts-jwt",
        user_id="alice@example.com",
        expires_at=time.time() + 3600,
    )

    factory = _make_auth_token_factory(server_url=server_url)
    assert factory is not None, "factory must be created while JWT is valid"
    assert factory() == "valid-accounts-jwt"

    # Now expire the JWT (overwrite with a past timestamp).
    store_token(
        server_url,
        token="valid-accounts-jwt",
        user_id="alice@example.com",
        expires_at=time.time() - 1,
    )

    # After expiry the factory must return None (no token), not raise.
    result = factory()
    assert result is None, (
        "factory() must return None after the accounts JWT expires; "
        "it must not fall through to Databricks SDK and raise an error."
    )


def test_auth_flow_error_names_accounts_expiry_not_databricks(
    _accounts_only_env: str,
) -> None:
    """``_RunnerDatabricksAuth`` must not surface a Databricks error on accounts expiry.

    After the accounts JWT expires in a no-Databricks deployment the
    runner raises ``httpx.RequestError: Databricks token refresh returned
    no token`` on every runner->server callback.  That message is actively
    misleading -- Databricks is neither configured nor involved -- and it
    hides the real remedy (re-run ``omnigent login``).

    This test pins the failure symptom visible to operators: whatever
    error the auth flow raises after an accounts-JWT expiry must NOT
    blame Databricks.  The fix may change the message, raise a dedicated
    error type, or mark the factory declined; any of those satisfies
    this assertion.

    :param _accounts_only_env: Isolated accounts-only environment fixture.
    :returns: None.
    """
    from omnigent.cli_auth import store_token

    server_url = _accounts_only_env

    # Build the factory while the JWT is still valid (runner startup).
    store_token(
        server_url,
        token="valid-accounts-jwt",
        user_id="alice@example.com",
        expires_at=time.time() + 3600,
    )
    factory = _make_auth_token_factory(server_url=server_url)
    assert factory is not None

    # The JWT expires while the runner is alive.
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
        # No exception (e.g. the fix passes the request through and lets
        # the server 401, or marks the factory declined) is acceptable --
        # the test does not mandate a raise, only that any raised error
        # does not blame Databricks.
        return
    except httpx.RequestError as exc:
        # The unfixed build raises "Databricks token refresh returned no
        # token" here even though the deployment is pure accounts auth
        # with no Databricks at all.
        assert "Databricks" not in str(exc), (
            "expired accounts JWT must not surface a Databricks error in an "
            f"accounts-only deployment; got: {exc!r}"
        )
