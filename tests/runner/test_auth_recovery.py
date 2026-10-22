"""Credential recovery must survive asynchronous calls and delayed re-login."""

from __future__ import annotations

import asyncio
from itertools import pairwise
from types import SimpleNamespace

import httpx
import pytest

from omnigent.runner import _entry


@pytest.mark.parametrize("status", [401, 403, 302])
async def test_async_runner_auth_refreshes_outside_event_loop(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """HTTP callbacks and the native policy relay can use synchronous providers."""
    monkeypatch.setattr("omnigent.cli_auth.databricks_request_headers", lambda *a, **k: {})
    tokens = iter(["expired", "renewed"])
    sent: list[str] = []

    def token() -> str:
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return next(tokens)

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers["Authorization"])
        if sent[-1] == "Bearer renewed":
            return httpx.Response(200)
        return httpx.Response(status, headers={"Location": "/oidc/authorize"})

    async with httpx.AsyncClient(
        auth=_entry._RunnerDatabricksAuth(token), transport=httpx.MockTransport(respond)
    ) as client:
        response = await client.post("https://example.invalid/policies/evaluate")
    assert response.status_code == 200
    assert sent == ["Bearer expired", "Bearer renewed"]


@pytest.mark.parametrize("login_succeeds", [False, True])
def test_bootstrap_factory_retries_missing_credentials_after_long_outage(
    monkeypatch: pytest.MonkeyPatch, login_succeeds: bool
) -> None:
    """Ten minutes of missing credentials neither spin nor latch failure forever."""
    now = 0.0
    attempts: list[float] = []
    available = False

    def discover(*args: object, **kwargs: object):
        attempts.append(now)
        return (lambda: "renewed") if available else None

    monkeypatch.setattr(_entry, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(_entry, "_make_auth_token_factory", discover)
    factory = _entry._InitialAuthTokenFactory("expired", "https://example.invalid")
    assert factory.invalidate()
    for second in range(600):
        now = float(second)
        assert factory() is None
    assert 1 < len(attempts) <= 120
    assert all(b - a >= 5 for a, b in pairwise(attempts))
    available = login_succeeds
    now = 660.0
    assert factory() == ("renewed" if login_succeeds else None)
    if login_succeeds:
        count = len(attempts)
        now += 60
        assert factory() == "renewed"
        assert len(attempts) == count


def test_bootstrap_factory_invalidates_fallback_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second rejected credential refreshes the already-resolved provider."""

    class Provider:
        refreshed = False

        def __call__(self) -> str:
            return "renewed" if self.refreshed else "expired-fallback"

        def invalidate(self) -> bool:
            self.refreshed = True
            return True

    provider = Provider()
    monkeypatch.setattr(_entry, "_make_auth_token_factory", lambda *a, **k: provider)
    factory = _entry._InitialAuthTokenFactory("bootstrap", "https://example.invalid")
    factory.invalidate()
    assert factory() == "expired-fallback"
    assert factory.invalidate()
    assert factory() == "renewed"


def test_managed_mint_invalidate_discards_cached_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server-revoked minted JWT must be discarded and re-minted, not re-sent."""
    minted: list[str] = []

    def mint(*args: object, **kwargs: object) -> tuple[str, float]:
        minted.append(f"minted-{len(minted) + 1}")
        return minted[-1], 9e9

    monkeypatch.setattr(_entry, "_mint_managed_owner_token", mint)
    factory = _entry._ManagedMintTokenFactory(
        "https://example.invalid/v1/runners/r/token",
        "https://example.invalid",
        "test-binding-token",
    )
    assert factory() == "minted-1"
    assert factory() == "minted-1"  # cached until expiry
    # The server rejected minted-1 (e.g. signing-key rotation): the resolved
    # provider must drop its cache so the retry presents a fresh credential.
    assert factory.invalidate() is True
    assert factory() == "minted-2"
    assert factory.invalidate() is True
    assert factory.invalidate() is False  # nothing cached to discard
