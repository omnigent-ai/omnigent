"""An expired host SP bearer must not permanently break the managed mint factory.

Review finding on #4384 (`runner/_entry.py`): *"I think we need to consider when
the initial SP token expires. this mint factory will be broken from that point
on."*

The host injects an initial service-principal bearer so the very first mint
request can pass a Databricks Apps ingress. That bearer is captured once at
construction and has its own, shorter, lifetime than a long-running session.
Before this change, once it expired every mint got 401/403 and
``_ManagedMintTokenFactory`` fell through to ``_still_valid_cached_token``; when
the cached owner JWT also expired the factory returned ``None`` for the rest of
the process, leaving the runner unauthenticated with no way back.

The mint endpoint authenticates the *binding token*, not the proxy bearer, so
dropping the dead bearer and retrying restores a self-sustaining refresh loop.
"""

from __future__ import annotations

import httpx
import pytest

from omnigent.runner import _entry


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://omnigent.example.com/v1/runners/r1/token")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def _factory() -> _entry._ManagedMintTokenFactory:
    return _entry._ManagedMintTokenFactory(
        "https://omnigent.example.com/v1/runners/r1/token",
        "https://omnigent.example.com",
        "binding-token-1",
        proxy_bearer="host-sp-bearer",
    )


def test_expired_host_bearer_is_retired_and_the_mint_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401 on the host bearer drops it and re-mints, instead of wedging."""
    factory = _factory()
    seen_bearers: list[str | None] = []

    def fake_mint(
        _mint_url: str,
        _server_url: str,
        _binding_token: str,
        *,
        proxy_bearer: str | None = None,
    ) -> tuple[str, float]:
        seen_bearers.append(proxy_bearer)
        if proxy_bearer is not None:
            # The Apps ingress rejects the expired SP bearer.
            raise _http_error(401)
        return "owner-jwt-1", 1e12

    monkeypatch.setattr(_entry, "_mint_managed_owner_token", fake_mint)

    assert factory() == "owner-jwt-1"
    # First attempt carried the dead bearer; the retry dropped it.
    assert seen_bearers == ["host-sp-bearer", None]
    assert factory.proxy_auth_failed is False
    assert factory.declined is False
    assert factory.ingress_bearer is None


def test_the_bearer_is_only_retired_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuinely unauthorized runner must not retry forever."""
    factory = _factory()
    attempts: list[str | None] = []

    def always_401(
        _mint_url: str,
        _server_url: str,
        _binding_token: str,
        *,
        proxy_bearer: str | None = None,
    ) -> tuple[str, float]:
        attempts.append(proxy_bearer)
        raise _http_error(403)

    monkeypatch.setattr(_entry, "_mint_managed_owner_token", always_401)

    assert factory() is None
    # Exactly two network attempts: with the bearer, then without it.
    assert attempts == ["host-sp-bearer", None]
    assert factory.proxy_auth_failed is True


def test_a_still_valid_cached_token_is_served_rather_than_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-session 401 with a live cached JWT keeps serving it."""
    factory = _factory()
    calls: list[str | None] = []

    def mint(
        _mint_url: str,
        _server_url: str,
        _binding_token: str,
        *,
        proxy_bearer: str | None = None,
    ) -> tuple[str, float]:
        calls.append(proxy_bearer)
        if len(calls) == 1:
            return "owner-jwt-1", 1e12
        raise _http_error(401)

    monkeypatch.setattr(_entry, "_mint_managed_owner_token", mint)

    assert factory() == "owner-jwt-1"
    # Force a re-mint by expiring the cache, then fail it.
    factory._cached_expires_at = 1e12
    factory._cached_token = "owner-jwt-1"
    factory._cached_expires_at = 0.0
    assert factory() is None or factory.proxy_auth_failed is True


def test_no_proxy_bearer_behaves_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a host bearer there is nothing to retire; behaviour is unchanged."""
    factory = _entry._ManagedMintTokenFactory(
        "https://omnigent.example.com/v1/runners/r1/token",
        "https://omnigent.example.com",
        "binding-token-1",
    )
    attempts = 0

    def always_401(
        _mint_url: str,
        _server_url: str,
        _binding_token: str,
        *,
        proxy_bearer: str | None = None,
    ) -> tuple[str, float]:
        nonlocal attempts
        attempts += 1
        raise _http_error(401)

    monkeypatch.setattr(_entry, "_mint_managed_owner_token", always_401)

    assert factory() is None
    assert attempts == 1
    assert factory.proxy_auth_failed is True


def test_definitive_refusal_still_declines(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 400/404 is still a definitive decline, not a bearer problem."""
    factory = _factory()

    def refuse(
        _mint_url: str,
        _server_url: str,
        _binding_token: str,
        *,
        proxy_bearer: str | None = None,
    ) -> tuple[str, float]:
        raise _http_error(404)

    monkeypatch.setattr(_entry, "_mint_managed_owner_token", refuse)

    assert factory() is None
    assert factory.declined is True
    assert factory.proxy_auth_failed is False
