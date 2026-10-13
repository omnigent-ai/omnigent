"""Binary request negotiation and dispatch over real loopback WebSockets."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import serve

from omnigent.runner.transports.ws_tunnel.binary_requests import encode_binary_request
from omnigent.runner.transports.ws_tunnel.frames import (
    HelloFrame,
    PingFrame,
    RequestFrame,
    ResponseEndFrame,
    ResponseHeadFrame,
    decode_body,
    decode_frame,
    encode_frame,
)
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.runner.transports.ws_tunnel.serve import _handle_tunnel_frame, _send_hello
from omnigent.runner.transports.ws_tunnel.transport import WSTunnelTransport
from omnigent.server.routes import runner_tunnel
from omnigent.server.routes.runner_tunnel import create_runner_tunnel_router
from omnigent.util.tunnel_limits import RUNNER_TUNNEL_MAX_MESSAGE_BYTES

_CAPABILITY = "binary_request_body_v1"
_RUNNER = "binary-test"


@pytest.fixture(autouse=True)
def _isolated_server_config(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("{}\n")
    monkeypatch.setenv("OMNIGENT_CONFIG", str(config))


@asynccontextmanager
async def _tunnel(
    capabilities: list[str],
) -> AsyncIterator[tuple[httpx.AsyncClient, ClientConnection, TunnelRegistry]]:
    registry = TunnelRegistry()
    app = FastAPI()
    app.include_router(create_runner_tunnel_router(registry), prefix="/v1")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="error", lifespan="off", ws="websockets")
        )
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            async with connect(
                f"ws://127.0.0.1:{port}/v1/runners/{_RUNNER}/tunnel",
                max_size=RUNNER_TUNNEL_MAX_MESSAGE_BYTES,
                compression=None,
            ) as ws:
                await ws.send(encode_frame(HelloFrame("test", 1, capabilities=capabilities)))
                assert await registry.wait_for_runner(_RUNNER, timeout_s=5) is not None
                async with httpx.AsyncClient(
                    transport=WSTunnelTransport(registry, _RUNNER), base_url="http://runner"
                ) as client:
                    yield client, ws, registry
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, 5)


def _envelope_header(raw: bytes) -> dict:
    return json.loads(raw[8 : 8 + int.from_bytes(raw[4:8], "big")])


async def _reply(ws: ClientConnection, req_id: str) -> None:
    await ws.send(encode_frame(ResponseHeadFrame(req_id, 204)))
    await ws.send(encode_frame(ResponseEndFrame(req_id)))


@pytest.mark.parametrize(
    ("capable", "enabled", "opted_in", "binary"),
    [
        (False, True, True, False),
        (True, True, True, True),
        (True, False, True, False),
        (True, True, False, False),
    ],
    ids=["old-runner", "binary", "kill-switch", "default-json"],
)
async def test_wire_decision(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capable: bool,
    enabled: bool,
    opted_in: bool,
    binary: bool,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(f"runner_tunnel_binary_request_body: {str(enabled).lower()}\n")
    monkeypatch.setenv("OMNIGENT_CONFIG", str(config))
    payload = bytes(range(256)) * 4096
    async with _tunnel([_CAPABILITY] if capable else []) as (client, ws, registry):
        task = asyncio.create_task(
            client.patch(
                "/upload",
                content=payload,
                headers={"content-type": "application/octet-stream"},
                extensions={"runner_tunnel_binary_request_body": opted_in},
            )
        )
        try:
            raw = await asyncio.wait_for(ws.recv(), 5)
            assert isinstance(raw, bytes) is binary
            if isinstance(raw, bytes):
                header_len = int.from_bytes(raw[4:8], "big")
                header = json.loads(raw[8 : 8 + header_len])
                assert raw[:4] == b"OTB\x01"
                assert "body" not in header
                assert raw[8 + header_len :] == payload
                assert len(raw) < len(payload) + 1024
                req_id = header["id"]
            else:
                frame = decode_frame(raw)
                assert isinstance(frame, RequestFrame)
                assert frame.encoding == "base64"
                assert decode_body(frame.body, frame.encoding) == payload
                req_id = frame.id
            await _reply(ws, req_id)
            assert (await asyncio.wait_for(task, 5)).status_code == 204
            session = registry.get(_RUNNER)
            assert session is not None
            assert session.diagnostics.snapshot()["last_send_outcome"] == "completed"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_hello_advertises_binary_without_version_bump() -> None:
    hello_received: asyncio.Future[HelloFrame] = asyncio.get_running_loop().create_future()

    async def receive_hello(ws) -> None:
        frame = decode_frame(await ws.recv())
        assert isinstance(frame, HelloFrame)
        hello_received.set_result(frame)

    async with serve(receive_hello, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}") as ws:
            await _send_hello(ws.send, "test")
            hello = await asyncio.wait_for(hello_received, 5)
            assert _CAPABILITY in hello.capabilities
            assert hello.frame_protocol_version == 1


@pytest.mark.parametrize(
    "size", [0, RUNNER_TUNNEL_MAX_MESSAGE_BYTES - 512], ids=["empty", "maximum"]
)
async def test_envelope_dispatch_round_trip(size: int) -> None:
    frame = RequestFrame("round-trip", "PATCH", "/upload", headers=[["x-test", "value"]])
    overhead = len(encode_binary_request(frame, b""))
    payload = b"\xff" * (RUNNER_TUNNEL_MAX_MESSAGE_BYTES - overhead if size else 0)
    wire = encode_binary_request(frame, payload)
    assert len(wire) == (RUNNER_TUNNEL_MAX_MESSAGE_BYTES if size else overhead)
    received: list[dict[str, object]] = []

    async def app(scope, receive, send) -> None:
        event = await receive()
        received.append(event)
        assert scope["method"] == "PATCH"
        assert scope["path"] == "/upload"
        assert scope["headers"] == [(b"x-test", b"value")]
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async with _tunnel([_CAPABILITY]) as (_, ws, registry):
        session = registry.get(_RUNNER)
        assert session is not None
        state = registry.open_request(_RUNNER, frame.id)
        await registry.send_request(session, wire, request_id=frame.id)
        tasks: dict[str, asyncio.Task[None]] = {}
        await _handle_tunnel_frame(app, await asyncio.wait_for(ws.recv(), 10), ws.send, tasks, {})
        await asyncio.wait_for(asyncio.gather(*tasks.values()), 10)
        assert (await asyncio.wait_for(state.head_future, 5)).status == 204
        assert received == [{"type": "http.request", "body": payload, "more_body": False}]
        registry.close_request(_RUNNER, frame.id)


@pytest.mark.parametrize(
    "fault",
    ["magic", "prefix", "truncated-header", "overflow", "json", "utf8", "body", "fields", "kind"],
)
async def test_malformed_envelope_is_diagnosed(fault: str, caplog) -> None:
    wire = encode_binary_request(RequestFrame("bad-request", "PATCH", "/upload"), b"payload")
    header = _envelope_header(wire)
    recoverable = fault in {"magic", "body", "fields", "kind"}
    if fault == "magic":
        wire = b"BAD!" + wire[4:]
    elif fault == "prefix":
        wire = wire[:6]
    elif fault == "truncated-header":
        wire = wire[:15]
    elif fault == "overflow":
        wire = wire[:4] + b"\xff\xff\xff\xff" + wire[8:]
    elif fault in {"json", "utf8"}:
        raw_header = b"{" if fault == "json" else b"\xff"
        wire = wire[:4] + len(raw_header).to_bytes(4, "big") + raw_header
    else:
        if fault == "body":
            header["body"] = "ambiguous"
        elif fault == "fields":
            header["headers"] = 123
        else:
            header["kind"] = "ping"
        raw_header = json.dumps(header).encode()
        wire = wire[:4] + len(raw_header).to_bytes(4, "big") + raw_header

    async def app(scope, receive, send) -> None:
        pytest.fail("malformed envelope reached ASGI")

    async with _tunnel([_CAPABILITY]) as (_, ws, registry):
        session = registry.get(_RUNNER)
        assert session is not None
        state = registry.open_request(_RUNNER, "bad-request")
        await registry.send_request(session, wire, request_id="bad-request")
        await _handle_tunnel_frame(app, await asyncio.wait_for(ws.recv(), 5), ws.send, {}, {})
        assert "malformed binary request" in caplog.text
        if recoverable:
            assert (await asyncio.wait_for(state.head_future, 5)).status == 400
            await asyncio.wait_for(state.end_event.wait(), 5)
        else:
            assert not state.head_future.done()
        registry.close_request(_RUNNER, "bad-request")


def test_encoder_rejects_over_limit() -> None:
    frame = RequestFrame("limit", "PATCH", "/")
    overhead = len(encode_binary_request(frame, b""))
    with pytest.raises(ValueError, match="message limit"):
        encode_binary_request(frame, b"x" * (RUNNER_TUNNEL_MAX_MESSAGE_BYTES - overhead + 1))


async def test_decoder_rejects_over_limit(caplog) -> None:
    wire = encode_binary_request(RequestFrame("limit", "PATCH", "/"), b"")
    wire += b"x" * (RUNNER_TUNNEL_MAX_MESSAGE_BYTES - len(wire) + 1)

    async def app(scope, receive, send) -> None:
        pytest.fail("oversized request reached ASGI")

    async def receiver(ws) -> None:
        await _handle_tunnel_frame(app, await ws.recv(), ws.send, {}, {})

    async with serve(receiver, "127.0.0.1", 0, max_size=len(wire), compression=None) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}", compression=None) as ws:
            await ws.send(wire)
            head = decode_frame(await asyncio.wait_for(ws.recv(), 10))
            assert isinstance(head, ResponseHeadFrame)
            assert head.id == "limit" and head.status == 400
            assert isinstance(decode_frame(await ws.recv()), ResponseEndFrame)
    assert "message limit" in caplog.text


@asynccontextmanager
async def _runner_dispatch(
    ws: ClientConnection, app, binary_sizes: list[int] | None = None
) -> AsyncIterator[dict[str, asyncio.Task[None]]]:
    dispatches: dict[str, asyncio.Task[None]] = {}

    async def receive() -> None:
        async for raw in ws:
            if isinstance(raw, bytes) and binary_sizes is not None:
                binary_sizes.append(len(raw))
            await _handle_tunnel_frame(app, raw, ws.send, dispatches, {})

    receiver = asyncio.create_task(receive())
    try:
        yield dispatches
    finally:
        receiver.cancel()
        tasks = list(dispatches.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(receiver, *tasks, return_exceptions=True)


async def test_binary_admission_bounds_buffering_and_keeps_pings_flowing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer_resume = asyncio.Event()
    uploads_started = asyncio.Event()
    uploads_finish = asyncio.Event()
    active_uploads = 0
    original_sender = runner_tunnel._sender_loop

    async def paused_sender(ws, session) -> None:
        await writer_resume.wait()
        await original_sender(ws, session)

    async def app(scope, receive, send) -> None:
        nonlocal active_uploads
        if scope["path"] == "/upload":
            event = await receive()
            assert len(event["body"]) == len(payload)
            active_uploads += 1
            if active_uploads == 2:
                uploads_started.set()
            try:
                await uploads_finish.wait()
            finally:
                active_uploads -= 1
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    monkeypatch.setattr(runner_tunnel, "_sender_loop", paused_sender)
    async with _tunnel([_CAPABILITY]) as (client, ws, registry):
        # Content-Length has the same number of digits at this boundary.
        probe = client.build_request(
            "PATCH", "/upload", headers={"content-length": str(RUNNER_TUNNEL_MAX_MESSAGE_BYTES)}
        )
        frame = RequestFrame(
            "0" * 32,
            "PATCH",
            "/upload",
            headers=list(map(list, probe.headers.items())),
            stream=True,
        )
        payload = b"\xff" * (
            RUNNER_TUNNEL_MAX_MESSAGE_BYTES - len(encode_binary_request(frame, b""))
        )
        session = registry.get(_RUNNER)
        assert session is not None
        binary_sizes: list[int] = []
        async with _runner_dispatch(ws, app, binary_sizes):
            tasks = [
                asyncio.create_task(
                    client.patch(
                        "/upload",
                        content=payload,
                        extensions={"runner_tunnel_binary_request_body": True},
                    )
                )
                for _ in range(2)
            ]
            try:
                async with asyncio.timeout(5):
                    while session.outbound_queue.qsize() < 2:
                        await asyncio.sleep(0.01)
                # Queue control and ordinary traffic before either envelope is sent.
                await registry.send_text(session, encode_frame(PingFrame(123)), app_ping_ts=123)
                health = asyncio.create_task(client.get("/health"))
                tasks.append(health)
                async with asyncio.timeout(5):
                    while session.outbound_queue.qsize() < 4:
                        await asyncio.sleep(0.01)
                for _ in range(4):
                    with pytest.raises(httpx.PoolTimeout, match="binary"):
                        await asyncio.wait_for(
                            client.patch(
                                "/upload",
                                content=payload,
                                extensions={"runner_tunnel_binary_request_body": True},
                            ),
                            1,
                        )
                    assert session.outbound_queue.qsize() == 4
                writer_resume.set()
                await asyncio.wait_for(uploads_started.wait(), 10)
                assert (await asyncio.wait_for(health, 10)).status_code == 204
                async with asyncio.timeout(5):
                    while session.diagnostics.snapshot()["last_app_pong_received_age_s"] is None:
                        await asyncio.sleep(0.01)
                assert binary_sizes == [RUNNER_TUNNEL_MAX_MESSAGE_BYTES] * 2
                assert active_uploads == 2
                assert not uploads_finish.is_set()
                assert all(not task.done() for task in tasks[:2])
                uploads_finish.set()
                responses = await asyncio.wait_for(asyncio.gather(*tasks), 10)
                assert all(response.status_code == 204 for response in responses)
            finally:
                writer_resume.set()
                uploads_finish.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("queued", [False, True], ids=["sent", "queued"])
@pytest.mark.parametrize("terminal_error", [False, True], ids=["end", "error"])
async def test_cancelled_requests_keep_admission_until_runner_finishes(
    monkeypatch: pytest.MonkeyPatch,
    queued: bool,
    terminal_error: bool,
) -> None:
    writer_resume = asyncio.Event()
    if not queued:
        writer_resume.set()
    uploads_started = asyncio.Event()
    uploads_finish = asyncio.Event()
    active_uploads = 0
    peak_uploads = 0
    original_sender = runner_tunnel._sender_loop

    async def paused_sender(ws, session) -> None:
        await writer_resume.wait()
        await original_sender(ws, session)

    async def app(scope, receive, send) -> None:
        nonlocal active_uploads, peak_uploads
        if scope["path"] == "/upload":
            await receive()
            active_uploads += 1
            peak_uploads = max(peak_uploads, active_uploads)
            if active_uploads == 2:
                uploads_started.set()
            try:
                await uploads_finish.wait()
                await send({"type": "http.response.start", "status": 204, "headers": []})
                if terminal_error:
                    raise RuntimeError("blocked upload failed")
                await send({"type": "http.response.body", "body": b""})
            finally:
                active_uploads -= 1
        else:
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    monkeypatch.setattr(runner_tunnel, "_sender_loop", paused_sender)
    payload = b"\xff" * (RUNNER_TUNNEL_MAX_MESSAGE_BYTES - 1024)
    async with _tunnel([_CAPABILITY]) as (client, ws, registry):
        session = registry.get(_RUNNER)
        assert session is not None
        async with _runner_dispatch(ws, app) as dispatches:
            tasks = [
                asyncio.create_task(
                    client.patch(
                        "/upload",
                        content=payload,
                        extensions={"runner_tunnel_binary_request_body": True},
                    )
                )
                for _ in range(2)
            ]
            try:
                if queued:
                    async with asyncio.timeout(5):
                        while session.outbound_queue.qsize() < 2:
                            await asyncio.sleep(0.01)
                else:
                    await asyncio.wait_for(uploads_started.wait(), 10)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                assert not session.in_flight
                if queued:
                    for _ in range(4):
                        with pytest.raises(httpx.PoolTimeout):
                            await asyncio.wait_for(
                                client.patch(
                                    "/upload",
                                    content=payload,
                                    extensions={"runner_tunnel_binary_request_body": True},
                                ),
                                1,
                            )
                        assert session.outbound_queue.qsize() == 2
                    writer_resume.set()
                    await asyncio.wait_for(uploads_started.wait(), 10)
                # Cancellation has removed both consumers, but ASGI work is still blocked.
                assert active_uploads == 2
                for _ in range(3):
                    with pytest.raises(httpx.PoolTimeout):
                        await asyncio.wait_for(
                            client.patch(
                                "/upload",
                                content=payload,
                                extensions={"runner_tunnel_binary_request_body": True},
                            ),
                            1,
                        )
                assert peak_uploads == 2
                uploads_finish.set()
                results = await asyncio.wait_for(
                    asyncio.gather(*list(dispatches.values()), return_exceptions=True), 5
                )
                if terminal_error:
                    assert len(results) == 2
                    assert all(isinstance(result, RuntimeError) for result in results)
                else:
                    assert results == [None, None]
                async with asyncio.timeout(5):
                    while session.binary_requests:
                        await asyncio.sleep(0.01)
                assert active_uploads == 0
                assert not session.binary_pending_sends
                response = await asyncio.wait_for(
                    client.patch(
                        "/health",
                        content=b"retry",
                        extensions={"runner_tunnel_binary_request_body": True},
                    ),
                    5,
                )
                assert response.status_code == 204
                assert not session.binary_requests
            finally:
                writer_resume.set()
                uploads_finish.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


async def test_transport_reports_oversized_binary_request() -> None:
    async with _tunnel([_CAPABILITY]) as (client, _, registry):
        payload = b"x" * RUNNER_TUNNEL_MAX_MESSAGE_BYTES
        with pytest.raises(
            httpx.LocalProtocolError, match=r"binary request.*message limit"
        ) as error:
            await client.patch(
                "/upload",
                content=payload,
                extensions={"runner_tunnel_binary_request_body": True},
            )
        assert error.value.request.url.path == "/upload"
        session = registry.get(_RUNNER)
        assert session is not None
        assert session.outbound_queue.empty()
        assert not session.in_flight
        assert not session.binary_requests


async def test_binary_request_across_consumer_loop() -> None:
    def consume() -> int:
        async def send() -> int:
            async with httpx.AsyncClient(
                transport=WSTunnelTransport(registry, _RUNNER), base_url="http://runner"
            ) as client:
                response = await client.patch(
                    "/upload",
                    content=b"\xff",
                    extensions={"runner_tunnel_binary_request_body": True},
                )
                return response.status_code

        return asyncio.run(send())

    async with _tunnel([_CAPABILITY]) as (_, ws, registry):
        task = asyncio.create_task(asyncio.to_thread(consume))
        try:
            raw = await asyncio.wait_for(ws.recv(), 5)
            assert isinstance(raw, bytes)
            req_id = _envelope_header(raw)["id"]
            await _reply(ws, req_id)
            assert await asyncio.wait_for(task, 5) == 204
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
