"""Adapt blocking HTTP authentication providers for asynchronous clients."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncGenerator

import httpx


class ThreadedAuth(httpx.Auth):
    """Run each synchronous auth-flow step outside the caller's event loop."""

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """Preserve HTTPX body/retry semantics while offloading credential work."""
        if self.requires_request_body:
            await request.aread()
        flow = self.auth_flow(request)
        lock = threading.Lock()

        def advance(response: httpx.Response | None) -> httpx.Request | None:
            with lock:
                try:
                    return next(flow) if response is None else flow.send(response)
                except StopIteration:
                    return None

        def close() -> None:
            # Cancellation doesn't stop an in-flight worker. Serialize cleanup
            # with that worker so we never close an executing generator.
            with lock:
                flow.close()

        try:
            next_request = await asyncio.to_thread(advance, None)
            while next_request is not None:
                response = yield next_request
                if self.requires_response_body:
                    await response.aread()
                next_request = await asyncio.to_thread(advance, response)
        finally:
            await asyncio.to_thread(close)
