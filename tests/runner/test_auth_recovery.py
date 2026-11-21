"""Credential recovery must survive asynchronous calls and delayed re-login."""

from __future__ import annotations

import asyncio
import contextlib
from itertools import pairwise
from types import SimpleNamespace

import httpx
import pytest

from omnigent.runner import _entry, credential_rejections


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


@pytest.mark.parametrize("failure", ["rejected_after_refresh", "refresh_raises", "mint_declined"])
async def test_persistently_refused_credentials_raise_the_sign_in_exit(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A runner whose every server call is refused for the window exits with the sign-in error.

    One accepted call restarts the window, so a brief refusal never stops the runner. A
    declined mint sends the call without a token, and its outcome counts the same way.
    """
    monkeypatch.setattr("omnigent.cli_auth.databricks_request_headers", lambda *a, **k: {})
    monkeypatch.setattr(
        _entry, "credential_rejections", credential_rejections.CredentialRejections()
    )
    clock = [0.0]
    monkeypatch.setattr(credential_rejections, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    accept = [False]

    class _DeclinedMint:
        declined_without_proxy_bearer = True

        def __call__(self) -> str | None:
            return None

        def call_with_declined(self) -> tuple[str | None, bool]:
            return None, True

        def reset_decline(self) -> None:
            pass

    def token() -> str:
        if failure == "refresh_raises" and not accept[0]:
            raise RuntimeError("the refresh token is invalid")
        return "token"

    factory = _DeclinedMint() if failure == "mint_declined" else token

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200 if accept[0] else 401)

    exit_task = asyncio.create_task(
        _entry._raise_when_credentials_rejected("https://server.example.com")
    )
    await asyncio.sleep(0)
    with httpx.Client(
        auth=_entry._RunnerDatabricksAuth(factory), transport=httpx.MockTransport(respond)
    ) as client:

        def call() -> None:
            with contextlib.suppress(RuntimeError):
                client.get("https://server.example.com/v1/sessions/s/events")

        for elapsed in (0.0, 100.0):
            clock[0] = elapsed
            call()
        accept[0] = True
        call()
        accept[0] = False
        for elapsed in (200.0, 450.0):
            clock[0] = elapsed
            call()
        await asyncio.sleep(0)
        assert not exit_task.done()

        clock[0] = 500.0
        call()
        with pytest.raises(_entry.RunnerTunnelAuthRejectedError, match=r" login https://"):
            await asyncio.wait_for(exit_task, timeout=1.0)


@pytest.mark.parametrize(
    ("response", "refused"),
    [
        (httpx.Response(401), True),
        (httpx.Response(302, headers={"Location": "/oidc/oauth2/v2.0/authorize"}), True),
        (httpx.Response(403), False),
        (httpx.Response(302, headers={"Location": "/sessions"}), False),
        (httpx.Response(200), False),
    ],
)
def test_only_an_unauthenticated_response_counts_as_refused_credentials(
    response: httpx.Response, refused: bool
) -> None:
    """A 403 can be a permission denial for a valid user, so it never stops the runner."""
    assert credential_rejections.refuses_credentials(response) is refused


def test_runner_entry_run_as_main_shares_the_credential_tracker() -> None:
    """A direct-spawned runner runs ``_entry`` as ``__main__``, beside the imported module."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_runner_entry_as_main", _entry.__file__)
    assert spec is not None and spec.loader is not None
    as_main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(as_main)

    assert as_main.credential_rejections is _entry.credential_rejections
