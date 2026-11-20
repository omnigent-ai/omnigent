"""Registration waits tolerate pending writes and retain useful setup failures."""

import asyncio

import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI

from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from tests.server import helpers

pytestmark = pytest.mark.asyncio


async def test_registration_waits_for_pending_work() -> None:
    app = FastAPI()
    app.state.tunnel_registry = TunnelRegistry()
    release = asyncio.Event()
    started = asyncio.Event()

    async def application(scope, receive, send):
        started.set()
        await release.wait()
        helpers.register_test_runner(app, "runner-test")
        await asyncio.Event().wait()

    communicator = ApplicationCommunicator(application, {})
    registration = asyncio.create_task(
        helpers.wait_for_tunnel_registration(
            communicator, app.state.tunnel_registry, "runner-test"
        )
    )
    try:
        await started.wait()
        assert not registration.done()
        release.set()
        await registration
        assert app.state.tunnel_registry.get("runner-test") is not None
        assert not communicator.future.done()
    finally:
        registration.cancel()
        await asyncio.gather(registration, return_exceptions=True)
        communicator.stop()


@pytest.mark.parametrize("failure", [False, True])
async def test_registration_reports_early_application_exit(failure: bool) -> None:
    async def application(scope, receive, send):
        if failure:
            raise RuntimeError("registration write failed")

    communicator = ApplicationCommunicator(application, {})
    try:
        with pytest.raises(
            RuntimeError if failure else AssertionError,
            match="registration write failed"
            if failure
            else "runner-test closed before registration",
        ):
            await helpers.wait_for_tunnel_registration(
                communicator, TunnelRegistry(), "runner-test"
            )
    finally:
        communicator.stop(exceptions=False)


async def test_registration_timeout_names_peer_and_preserves_application(monkeypatch) -> None:
    monkeypatch.setattr(helpers, "budget", lambda seconds: 0)

    async def application(scope, receive, send):
        await asyncio.Event().wait()

    communicator = ApplicationCommunicator(application, {})
    try:
        with pytest.raises(
            AssertionError, match="Tunnel runner-test did not register; ASGI task:"
        ):
            await helpers.wait_for_tunnel_registration(
                communicator, TunnelRegistry(), "runner-test"
            )
        assert not communicator.future.done()
    finally:
        communicator.stop()
