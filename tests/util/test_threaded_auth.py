"""Async transport and cancellation behavior of blocking auth providers."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

import httpx

from omnigent.util.threaded_auth import ThreadedAuth
from tests.budgets import budget


async def test_async_streaming_bodies_remain_available_to_auth() -> None:
    """Body-dependent auth sees buffered request and response streams on retry."""

    class BodyAuth(ThreadedAuth):
        requires_request_body = True
        requires_response_body = True

        def auth_flow(
            self, request: httpx.Request
        ) -> Generator[httpx.Request, httpx.Response, None]:
            assert request.content == b"request"
            response = yield request
            assert response.content == b"response"

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"response"

    async def body() -> AsyncIterator[bytes]:
        yield b"request"

    async with httpx.AsyncClient(
        auth=BodyAuth(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Body())),
    ) as client:
        response = await client.post("https://example.invalid", content=body())
    assert response.status_code == 200


async def test_cancellation_waits_for_provider_cleanup_without_blocking_loop() -> None:
    """Cancellation cannot close a generator while its credential lookup runs."""
    started = asyncio.Event()
    release = threading.Event()
    closed = threading.Event()
    loop = asyncio.get_running_loop()

    class BlockingAuth(ThreadedAuth):
        def auth_flow(
            self, request: httpx.Request
        ) -> Generator[httpx.Request, httpx.Response, None]:
            try:
                loop.call_soon_threadsafe(started.set)
                assert release.wait(timeout=5)
                yield request
            finally:
                closed.set()

    flow = BlockingAuth().async_auth_flow(httpx.Request("GET", "https://example.invalid"))
    task = asyncio.create_task(anext(flow))
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        task.cancel()
        await asyncio.sleep(0)
        assert not closed.is_set()
    finally:
        release.set()
    with suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=3)
    assert closed.is_set()


async def test_credential_steps_run_while_the_default_executor_is_full() -> None:
    """Authenticated requests don't queue behind blocking jobs on the default pool."""
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    release = threading.Event()
    default_job = asyncio.ensure_future(asyncio.to_thread(release.wait, budget(10)))

    class TokenAuth(ThreadedAuth):
        def auth_flow(
            self, request: httpx.Request
        ) -> Generator[httpx.Request, httpx.Response, None]:
            request.headers["Authorization"] = "Bearer token"
            yield request

    try:
        async with httpx.AsyncClient(
            auth=TokenAuth(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=request.headers["Authorization"])
            ),
        ) as client:
            response = await asyncio.wait_for(
                client.get("https://example.invalid"), timeout=budget(2)
            )
        assert response.text == "Bearer token"
    finally:
        release.set()
        await default_job
