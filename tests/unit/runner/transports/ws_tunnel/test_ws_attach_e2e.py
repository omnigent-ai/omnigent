"""End-to-end test for tunneled WebSocket attach over the runner tunnel.

Wires up: server-side ``_TunneledWSConn`` factory + tunnel registry +
``_handle_tunnel_frame`` on the runner side + a runner-side FastAPI app
whose ``@app.websocket(...)`` route echoes frames. Verifies that bytes
and text round-trip in both directions and that a runner-side close is
surfaced as :class:`websockets.exceptions.ConnectionClosed` with the
peer-supplied code+reason on ``.rcvd`` (the shape the
terminal-attach shuttle expects).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib

import pytest
from fastapi import FastAPI, WebSocket
from websockets.exceptions import ConnectionClosed

from omnigent.runner.transports.ws_tunnel.frames import (
    HelloFrame,
    WSCloseFrame,
    WSFrame,
    WSOpenFrame,
    decode_frame,
    encode_frame,
)
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.runner.transports.ws_tunnel.serve import (
    _cancel_ws_channels,
    _handle_tunnel_frame,
    _RunnerWSChannel,
)
from omnigent.server._runner_ws_tunnel import _TunneledWSConn


class _FakeWS:
    """Bidirectional in-memory WebSocket pair half."""

    def __init__(self) -> None:
        self.recv_queue: asyncio.Queue[str] = asyncio.Queue()
        self._peer: _FakeWS | None = None

    def link(self, peer: _FakeWS) -> None:
        self._peer = peer
        peer._peer = self

    async def send_text(self, data: str) -> None:
        assert self._peer is not None
        await self._peer.recv_queue.put(data)

    async def receive_text(self) -> str:
        return await self.recv_queue.get()


def _make_pair() -> tuple[_FakeWS, _FakeWS]:
    a, b = _FakeWS(), _FakeWS()
    a.link(b)
    return a, b


async def _drain_session_outbound(registry: TunnelRegistry, runner_id: str) -> None:
    session = registry.get(runner_id)
    assert session is not None
    while True:
        data = await session.outbound_queue.get()
        if data is None:
            return
        await session.ws.send_text(data)


def _build_echo_app() -> FastAPI:
    app = FastAPI()

    @app.websocket("/v1/sessions/{session_id}/resources/terminals/{terminal_id}/attach")
    async def attach(websocket: WebSocket, session_id: str, terminal_id: str) -> None:
        await websocket.accept()
        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("text") == "close-me":
                    # Surface a custom close code+reason so the test can
                    # assert ConnectionClosed.rcvd round-trips.
                    await websocket.close(code=4242, reason="goodbye")
                    return
                if msg.get("text") is not None:
                    await websocket.send_text(f"echo:{msg['text']}")
                elif msg.get("bytes") is not None:
                    await websocket.send_bytes(b"binary-echo:" + msg["bytes"])
        except Exception:
            return

    return app


@pytest.mark.asyncio
async def test_text_and_binary_round_trip_over_tunneled_ws_attach() -> None:
    """Text and binary frames round-trip in both directions."""
    runner_app = _build_echo_app()
    server_ws, runner_ws = _make_pair()

    registry = TunnelRegistry()
    hello = HelloFrame(
        runner_version="0.1.0-test",
        frame_protocol_version=1,
        harnesses=["test"],
        envs=["test"],
    )
    runner_id = "runner-attach-1"
    session = registry.register(runner_id, server_ws, hello)

    # Server-side sender loop drains the session outbound queue onto
    # the fake socket — same pattern that the real runner_tunnel route
    # uses in production.
    sender_task = asyncio.create_task(_drain_session_outbound(registry, runner_id))

    # Server-side receive loop routes ws.* frames into the channel
    # inbound queue.
    async def server_receive_loop() -> None:
        while True:
            text = await server_ws.receive_text()
            frame = decode_frame(text)
            if isinstance(frame, (WSFrame, WSCloseFrame)):
                registry.route_ws_inbound(runner_id, frame, session=session)

    server_recv_task = asyncio.create_task(server_receive_loop())

    # Runner-side: invoke _handle_tunnel_frame for each inbound text frame.
    ws_channels: dict[str, _RunnerWSChannel] = {}
    dispatch_tasks: dict[str, asyncio.Task[None]] = {}

    async def runner_receive_loop() -> None:
        while True:
            text = await runner_ws.receive_text()
            await _handle_tunnel_frame(
                runner_app, text, runner_ws.send_text, dispatch_tasks, ws_channels
            )

    runner_recv_task = asyncio.create_task(runner_receive_loop())

    try:
        # Server side: open a tunneled WS attach via the factory's
        # connection class directly.
        runner_path = (
            "/v1/sessions/conv_x/resources/terminals/terminal_bash_s1/attach?read_only=false"
        )
        async with _TunneledWSConn(
            registry=registry,
            session=session,
            runner_path=runner_path,
        ) as conn:
            # Wait until the runner dispatch task has accepted, so the
            # echo route's receive loop is in place before we send.
            for _ in range(20):
                if ws_channels and any(ch.accepted for ch in ws_channels.values()):
                    break
                await asyncio.sleep(0.01)

            # Text → text round-trip.
            await conn.send("hello")
            reply = await asyncio.wait_for(conn.recv(), timeout=2.0)
            assert reply == "echo:hello"

            # Binary → binary round-trip.
            await conn.send(b"\x00\x01\x02ABC")
            reply2 = await asyncio.wait_for(conn.recv(), timeout=2.0)
            assert reply2 == b"binary-echo:\x00\x01\x02ABC"
    finally:
        for task in (sender_task, server_recv_task, runner_recv_task):
            task.cancel()
        await asyncio.gather(
            sender_task, server_recv_task, runner_recv_task, return_exceptions=True
        )
        await _cancel_ws_channels(ws_channels)


@pytest.mark.asyncio
async def test_runner_side_close_surfaces_as_connection_closed_with_code() -> None:
    """A runner-initiated WS close arrives on the server as ConnectionClosed."""
    runner_app = _build_echo_app()
    server_ws, runner_ws = _make_pair()
    registry = TunnelRegistry()
    hello = HelloFrame(
        runner_version="0.1.0-test",
        frame_protocol_version=1,
        harnesses=["test"],
        envs=["test"],
    )
    runner_id = "runner-attach-2"
    session = registry.register(runner_id, server_ws, hello)
    sender_task = asyncio.create_task(_drain_session_outbound(registry, runner_id))

    async def server_receive_loop() -> None:
        while True:
            text = await server_ws.receive_text()
            frame = decode_frame(text)
            if isinstance(frame, (WSFrame, WSCloseFrame)):
                registry.route_ws_inbound(runner_id, frame, session=session)

    server_recv_task = asyncio.create_task(server_receive_loop())

    ws_channels: dict[str, _RunnerWSChannel] = {}
    dispatch_tasks: dict[str, asyncio.Task[None]] = {}

    async def runner_receive_loop() -> None:
        while True:
            text = await runner_ws.receive_text()
            await _handle_tunnel_frame(
                runner_app, text, runner_ws.send_text, dispatch_tasks, ws_channels
            )

    runner_recv_task = asyncio.create_task(runner_receive_loop())

    try:
        runner_path = (
            "/v1/sessions/conv_y/resources/terminals/terminal_bash_s1/attach?read_only=false"
        )
        async with _TunneledWSConn(
            registry=registry,
            session=session,
            runner_path=runner_path,
        ) as conn:
            for _ in range(20):
                if ws_channels and any(ch.accepted for ch in ws_channels.values()):
                    break
                await asyncio.sleep(0.01)

            await conn.send("close-me")
            with pytest.raises(ConnectionClosed) as exc_info:
                await asyncio.wait_for(conn.recv(), timeout=2.0)
            assert exc_info.value.rcvd is not None
            assert exc_info.value.rcvd.code == 4242
            assert exc_info.value.rcvd.reason == "goodbye"
    finally:
        for task in (sender_task, server_recv_task, runner_recv_task):
            task.cancel()
        await asyncio.gather(
            sender_task, server_recv_task, runner_recv_task, return_exceptions=True
        )
        await _cancel_ws_channels(ws_channels)


@pytest.mark.asyncio
async def test_frame_encode_decode_round_trip() -> None:
    """The three new frame kinds survive encode/decode."""
    for frame in (
        WSOpenFrame(ch_id="ab12", path="/v1/x", query_string="a=b"),
        WSFrame(ch_id="ab12", data="hello", encoding="utf-8"),
        WSFrame(
            ch_id="ab12",
            data=base64.b64encode(b"\x00\xff").decode("ascii"),
            encoding="base64",
        ),
        WSCloseFrame(ch_id="ab12", code=4242, reason="bye"),
    ):
        round_tripped = decode_frame(encode_frame(frame))
        assert round_tripped == frame


@pytest.mark.asyncio
async def test_open_ws_channel_session_guard_rejects_stale_session() -> None:
    """open_ws_channel raises KeyError when its session has been replaced."""
    registry = TunnelRegistry()
    ws_a, _ = _make_pair()
    hello = HelloFrame(
        runner_version="0.1.0",
        frame_protocol_version=1,
        harnesses=["t"],
        envs=["t"],
    )
    session_old = registry.register("runner-replace", ws_a, hello)
    # New session replaces the old one.
    ws_b, _ = _make_pair()
    registry.register("runner-replace", ws_b, hello)
    with pytest.raises(KeyError):
        registry.open_ws_channel("runner-replace", "stale01", session=session_old)
    with contextlib.suppress(KeyError):
        registry.deregister("runner-replace")
