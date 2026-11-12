"""Failure reporting of ``sent_to_host``, the host route tests' frame wait.

The route tests that use it cover the frame arriving; these pin what it says
when the route never sends one.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.host.frames import HostHelloFrame
from omnigent.server.host_registry import HostConnection, HostRegistry
from tests.server.helpers import sent_to_host

_HOST_ID = "a828988dc0b441fb8d04dad3761773b9"


def _connection() -> HostConnection:
    return HostRegistry().register(
        _HOST_ID,
        AsyncMock(),
        HostHelloFrame(version="test", frame_protocol_version=1, name="laptop", capabilities=[]),
        owner="owner",
    )


async def _never_answers() -> httpx.Response:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


async def test_says_what_the_route_answered_when_it_never_asks_the_host() -> None:
    conn = _connection()

    async def answers_404() -> httpx.Response:
        return httpx.Response(404, text='{"detail":"host not found"}')

    request = asyncio.create_task(answers_404())
    answered = r"answered 404 without asking the host: .*host not found"
    with pytest.raises(AssertionError, match=answered):
        await sent_to_host(conn, request, timeout=5)


async def test_fails_when_the_connection_closes_instead_of_sending() -> None:
    conn = _connection()
    request = asyncio.create_task(_never_answers())
    conn.outbound_queue.put_nowait(None)
    try:
        with pytest.raises(AssertionError, match="connection closed instead of sending"):
            await sent_to_host(conn, request, timeout=5)
    finally:
        request.cancel()


async def test_times_out_when_neither_the_frame_nor_the_answer_comes() -> None:
    conn = _connection()
    request = asyncio.create_task(_never_answers())
    try:
        with pytest.raises(AssertionError, match=r"didn't ask the host within 0\.05s"):
            await sent_to_host(conn, request, timeout=0.05)
    finally:
        request.cancel()
    assert conn.outbound_queue.empty()
