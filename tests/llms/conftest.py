"""Shared fixtures for LLM adapter tests."""

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest


class _UnreadBodyStream(httpx.AsyncByteStream):
    """
    Response stream whose body is NOT buffered at construction.

    ``httpx.Response(content=...)`` eagerly reads the body into
    ``_content``, which would make ``response.text`` work even on a
    streamed response that was never read — hiding the exact bug the
    adapters guard against. Passing ``stream=`` keeps the body unread
    until ``aread()`` is called, matching a live streaming connection.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._data


@pytest.fixture
def serve_streamed_response(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[int, bytes], None]:
    """
    Patch ``httpx.AsyncClient`` so any streamed request receives a
    canned response whose body stays unread until ``aread()``.

    Returns an installer: call it with ``(status_code, body)`` before
    invoking the adapter under test.
    """
    real_async_client = httpx.AsyncClient

    def _install(status_code: int, body: bytes) -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                status_code,
                stream=_UnreadBodyStream(body),
            )
        )

        def _factory(**kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = transport
            return real_async_client(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _factory)

    return _install
