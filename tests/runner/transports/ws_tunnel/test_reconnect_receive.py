"""Reconnect recovery must leave the tunnel available for requests and keepalives."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from omnigent.runner.transports.ws_tunnel.event_delivery import RunnerEventDispatcher
from omnigent.runner.transports.ws_tunnel.frames import (
    HelloFrame,
    PingFrame,
    PongFrame,
    RequestFrame,
    ResponseBodyFrame,
    ResponseEndFrame,
    ResponseHeadFrame,
    decode_frame,
    encode_frame,
)
from omnigent.runner.transports.ws_tunnel.serve import _serve_tunnel_once


@pytest.mark.parametrize("tunnel_events", [False, True])
@pytest.mark.parametrize("shutdown_signal", [False, True])
async def test_recovery_waiting_on_peer_keeps_receiving(
    tunnel_events: bool, shutdown_signal: bool
) -> None:
    recovery_started = asyncio.Event()
    recovery_complete = asyncio.Event()
    request_complete = asyncio.Event()
    received: list[Any] = []

    async def recover() -> None:
        recovery_started.set()
        await request_complete.wait()
        recovery_complete.set()

    async def app(_scope: Any, _receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"recovered"})

    async def peer(ws: ServerConnection) -> None:
        assert isinstance(decode_frame(await ws.recv()), HelloFrame)
        await recovery_started.wait()
        await ws.send(encode_frame(PingFrame(ts=123)))
        received.append(decode_frame(await ws.recv()))
        await ws.send(encode_frame(RequestFrame(id="recovery", method="GET", path="/health")))
        while not received or not isinstance(received[-1], ResponseEndFrame):
            received.append(decode_frame(await ws.recv()))
        request_complete.set()
        await recovery_complete.wait()
        await ws.close()

    async with serve(peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        await asyncio.wait_for(
            _serve_tunnel_once(
                app,
                tunnel_url=f"ws://127.0.0.1:{port}/tunnel",
                server_url=f"http://127.0.0.1:{port}",
                runner_id="runner-recovery",
                runner_version="0.1.0",
                on_ready=recover,
                event_dispatcher=RunnerEventDispatcher() if tunnel_events else None,
                shutdown_event=asyncio.Event() if shutdown_signal else None,
            ),
            timeout=2,
        )

    assert received == [
        PongFrame(ts=123),
        ResponseHeadFrame(id="recovery", status=200, headers=[]),
        ResponseBodyFrame(id="recovery", encoding="base64", body="cmVjb3ZlcmVk"),
        ResponseEndFrame(id="recovery"),
    ]
    assert recovery_complete.is_set()


@pytest.mark.parametrize("shutdown_signal", [False, True])
async def test_peer_close_cancels_pending_recovery(shutdown_signal: bool) -> None:
    recovery_started = asyncio.Event()
    recovery_cancelled = asyncio.Event()

    async def recover() -> None:
        recovery_started.set()
        try:
            await asyncio.Future()
        finally:
            recovery_cancelled.set()

    async def app(_scope: Any, _receive: Any, _send: Any) -> None:
        raise AssertionError("No application request is expected")

    async def peer(ws: ServerConnection) -> None:
        assert isinstance(decode_frame(await ws.recv()), HelloFrame)
        await recovery_started.wait()
        await ws.close()

    async with serve(peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        await asyncio.wait_for(
            _serve_tunnel_once(
                app,
                tunnel_url=f"ws://127.0.0.1:{port}/tunnel",
                server_url=f"http://127.0.0.1:{port}",
                runner_id="runner-recovery",
                runner_version="0.1.0",
                on_ready=recover,
                shutdown_event=asyncio.Event() if shutdown_signal else None,
            ),
            timeout=2,
        )
    assert recovery_cancelled.is_set()
