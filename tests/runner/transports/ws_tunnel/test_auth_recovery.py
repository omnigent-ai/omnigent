"""A connected tunnel survives a long credential outage until re-login."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus, InvalidURI
from websockets.http11 import Response

from omnigent.runner import _entry
from omnigent.runner.transports.ws_tunnel import serve as serve_module


@pytest.mark.parametrize("status", [401, 403, 302])
@pytest.mark.parametrize("login_succeeds", [False, True])
async def test_connected_tunnel_survives_delayed_or_failed_relogin(
    monkeypatch: pytest.MonkeyPatch, status: int, login_succeeds: bool
) -> None:
    """Simulate ten minutes of rejections without killing a connected runner."""
    now = 0.0
    attempts = 0
    recovered = False
    sleeps: list[float] = []
    shutdown = asyncio.Event()

    def discover(*args: object, **kwargs: object):
        return (lambda: "renewed") if login_succeeds and now >= 600 else None

    async def serve_once(app: Any, **kwargs: Any) -> None:
        nonlocal attempts, recovered
        attempts += 1
        if attempts == 1:
            kwargs["on_connected"]()
            return
        if kwargs["auth_token"] == "renewed":
            kwargs["on_connected"]()
            recovered = True
            shutdown.set()
            return
        if status == 302:
            raise InvalidURI("https://example.invalid/oidc/authorize", "scheme isn't ws or wss")
        raise InvalidStatus(Response(status, "Unauthorized", Headers(), b""))

    async def sleep(delay: float) -> None:
        nonlocal now
        sleeps.append(delay)
        now += delay
        if now >= 660:
            shutdown.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(_entry, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(_entry, "_make_auth_token_factory", discover)
    monkeypatch.setattr(serve_module, "_serve_tunnel_once", serve_once)
    monkeypatch.setattr(
        serve_module, "asyncio", SimpleNamespace(**(vars(asyncio) | {"sleep": sleep}))
    )
    monkeypatch.setattr(serve_module.random, "uniform", lambda *args: 0.0)
    factory = _entry._InitialAuthTokenFactory("bootstrap", "https://example.invalid")
    await serve_module.serve_tunnel(
        serve_once,
        server_url="https://example.invalid",
        runner_id="synthetic-runner",
        runner_version="0.1.0",
        auth_token="bootstrap",
        auth_token_factory=factory,
        shutdown_event=shutdown,
    )
    assert recovered is login_succeeds
    assert now >= 600
    assert attempts > 3
    assert max(sleeps) <= 10


@pytest.mark.parametrize("status", [401, 403, 302])
@pytest.mark.parametrize("refresh_fails", [False, True])
async def test_rejection_invalidates_off_loop_and_tolerates_refresh_failure(
    monkeypatch: pytest.MonkeyPatch, status: int, refresh_fails: bool
) -> None:
    shutdown = asyncio.Event()
    attempts = 0

    class Provider:
        invalidations = 0

        def __call__(self) -> str:
            return "renewed" if self.invalidations else "expired"

        def invalidate(self) -> bool:
            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
            self.invalidations += 1
            if refresh_fails:
                raise OSError("temporary credential provider failure")
            return True

    provider = Provider()

    async def serve_once(app: Any, **kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        if kwargs["auth_token"] == "renewed":
            shutdown.set()
            return
        if status == 302:
            raise InvalidURI("https://example.invalid/oidc/authorize", "scheme isn't ws or wss")
        raise InvalidStatus(Response(status, "Unauthorized", Headers(), b""))

    async def sleep(delay: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(serve_module, "_serve_tunnel_once", serve_once)
    monkeypatch.setattr(
        serve_module, "asyncio", SimpleNamespace(**(vars(asyncio) | {"sleep": sleep}))
    )
    await asyncio.wait_for(
        serve_module.serve_tunnel(
            serve_once,
            server_url="https://example.invalid",
            runner_id="synthetic-runner",
            runner_version="0.1.0",
            auth_token_factory=provider,
            shutdown_event=shutdown,
        ),
        timeout=5,
    )
    assert attempts == 2
    assert provider.invalidations == 1
