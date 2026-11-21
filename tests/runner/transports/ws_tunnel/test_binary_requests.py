"""Binary request negotiation and dispatch over real loopback WebSockets."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import socket
import tracemalloc
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import serve

from omnigent.runner.transports.ws_tunnel import serve as serve_module
from omnigent.runner.transports.ws_tunnel import transport as transport_module
from omnigent.runner.transports.ws_tunnel.binary_requests import encode_binary_request
from omnigent.runner.transports.ws_tunnel.diagnostics import TunnelDiagnostics
from omnigent.runner.transports.ws_tunnel.frames import (
    HelloFrame,
    PingFrame,
    RequestCancelFrame,
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


def _pause_sender(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    resume = asyncio.Event()
    original_sender = runner_tunnel._sender_loop

    async def paused_sender(ws, session) -> None:
        await resume.wait()
        await original_sender(ws, session)

    monkeypatch.setattr(runner_tunnel, "_sender_loop", paused_sender)
    return resume


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


@pytest.mark.parametrize("capable", [False, True], ids=["old-runner", "kill-switch"])
async def test_json_fallbacks_do_not_consume_admission(
    tmp_path,
    capable: bool,
) -> None:
    if capable:
        (tmp_path / "config.yaml").write_text("runner_tunnel_binary_request_body: false\n")
    async with _tunnel([_CAPABILITY] if capable else []) as (client, ws, registry):
        session = registry.get(_RUNNER)
        assert session is not None
        tasks = [
            asyncio.create_task(
                client.patch(
                    "/upload",
                    content=b"\xff",
                    headers={"content-type": "application/octet-stream"},
                    extensions={"runner_tunnel_binary_request_body": True},
                )
            )
            for _ in range(3)
        ]
        try:
            frames = []
            for _ in tasks:
                raw = await asyncio.wait_for(ws.recv(), 2)
                assert isinstance(raw, str)
                frame = decode_frame(raw)
                assert isinstance(frame, RequestFrame)
                assert decode_body(frame.body, frame.encoding) == b"\xff"
                frames.append(frame)
            assert not session.binary_requests
            assert not session.binary_pending_sends
            assert not session.binary_waiting_response
            tasks[0].cancel()
            await asyncio.gather(tasks[0], return_exceptions=True)
            cancel = decode_frame(await asyncio.wait_for(ws.recv(), 2))
            assert isinstance(cancel, RequestCancelFrame)
            assert cancel.id == frames[0].id
            for frame in frames[1:]:
                await _reply(ws, frame.id)
            responses = await asyncio.wait_for(asyncio.gather(*tasks[1:]), 2)
            assert all(response.status_code == 204 for response in responses)
            assert registry.get(_RUNNER) is session
            assert not session.in_flight
            assert not session.binary_requests
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_json_fallback_bypasses_full_binary_admission(tmp_path) -> None:
    async with _tunnel([_CAPABILITY]) as (client, ws, registry):
        session = registry.get(_RUNNER)
        assert session is not None
        tasks = [
            asyncio.create_task(
                client.patch(
                    "/upload",
                    content=b"chunk",
                    extensions={"runner_tunnel_binary_request_body": True},
                )
            )
            for _ in range(2)
        ]
        try:
            binary_ids = []
            for _ in tasks:
                raw = await asyncio.wait_for(ws.recv(), 2)
                assert isinstance(raw, bytes)
                binary_ids.append(_envelope_header(raw)["id"])
            assert session.binary_requests == set(binary_ids)
            (tmp_path / "config.yaml").write_text("runner_tunnel_binary_request_body: false\n")
            fallback = asyncio.create_task(
                client.patch(
                    "/fallback",
                    content=b"\xff",
                    headers={"content-type": "application/octet-stream"},
                    extensions={"runner_tunnel_binary_request_body": True},
                )
            )
            tasks.append(fallback)
            raw = await asyncio.wait_for(ws.recv(), 2)
            assert isinstance(raw, str)
            frame = decode_frame(raw)
            assert isinstance(frame, RequestFrame)
            assert frame.path == "/fallback"
            assert decode_body(frame.body, frame.encoding) == b"\xff"
            assert session.binary_requests == set(binary_ids)
            fallback.cancel()
            await asyncio.gather(fallback, return_exceptions=True)
            cancel = decode_frame(await asyncio.wait_for(ws.recv(), 2))
            assert isinstance(cancel, RequestCancelFrame)
            assert cancel.id == frame.id
            assert session.binary_requests == set(binary_ids)
            for req_id in binary_ids:
                await _reply(ws, req_id)
            assert all(
                response.status_code == 204
                for response in await asyncio.wait_for(asyncio.gather(*tasks[:2]), 2)
            )
            assert not session.binary_requests
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_wire_decision_is_bound_to_connection_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _tunnel([_CAPABILITY]) as (client, ws, registry):
        old_session = registry.get(_RUNNER)
        assert old_session is not None
        async with connect(
            f"ws://127.0.0.1:{ws.remote_address[1]}/v1/runners/{_RUNNER}/tunnel"
        ) as replacement:
            await replacement.send(encode_frame(HelloFrame("old-runner", 1)))
            async with asyncio.timeout(2):
                while registry.get(_RUNNER) is old_session:
                    await asyncio.sleep(0.01)
            current = registry.get(_RUNNER)
            assert current is not None
            get_session = registry.get
            # Model a reconnect after the transport snapshots capabilities.
            monkeypatch.setattr(registry, "get", lambda runner_id: old_session)
            with pytest.raises(httpx.ConnectError, match="changed before request") as error:
                await asyncio.wait_for(
                    client.patch(
                        "/upload",
                        content=b"chunk",
                        extensions={"runner_tunnel_binary_request_body": True},
                    ),
                    0.5,
                )
            assert error.value.request.url.path == "/upload"
            monkeypatch.setattr(registry, "get", get_session)
            assert not current.in_flight
            assert not current.binary_requests
            task = asyncio.create_task(
                client.patch("/health", extensions={"runner_tunnel_binary_request_body": True})
            )
            try:
                raw = await asyncio.wait_for(replacement.recv(), 2)
                assert isinstance(raw, str)
                frame = decode_frame(raw)
                assert isinstance(frame, RequestFrame)
                assert frame.path == "/health"
                await _reply(replacement, frame.id)
                assert (await asyncio.wait_for(task, 2)).status_code == 204
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    ("capable", "enabled", "opted_in"),
    [(True, True, False), (False, True, True), (True, False, True)],
    ids=["not-opted-in", "old-runner-fallback", "kill-switch-fallback"],
)
async def test_json_request_uses_replacement_connection(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capable: bool,
    enabled: bool,
    opted_in: bool,
) -> None:
    (tmp_path / "config.yaml").write_text(
        f"runner_tunnel_binary_request_body: {str(enabled).lower()}\n"
    )
    async with _tunnel([_CAPABILITY] if capable else []) as (client, ws, registry):
        old_session = registry.get(_RUNNER)
        assert old_session is not None
        async with connect(
            f"ws://127.0.0.1:{ws.remote_address[1]}/v1/runners/{_RUNNER}/tunnel"
        ) as replacement:
            await replacement.send(encode_frame(HelloFrame("replacement", 1)))
            async with asyncio.timeout(2):
                while registry.get(_RUNNER) is old_session:
                    await asyncio.sleep(0.01)
            current = registry.get(_RUNNER)
            assert current is not None
            # Model a reconnect after the transport snapshots capabilities.
            monkeypatch.setattr(registry, "get", lambda runner_id: old_session)
            task = asyncio.create_task(
                client.patch(
                    "/upload",
                    content=b"chunk",
                    extensions={"runner_tunnel_binary_request_body": opted_in},
                )
            )
            receiver = asyncio.create_task(replacement.recv())
            try:
                done, _ = await asyncio.wait(
                    [task, receiver], timeout=2, return_when=asyncio.FIRST_COMPLETED
                )
                assert done
                if task.done():
                    await task
                raw = await asyncio.wait_for(receiver, 2)
                assert isinstance(raw, str)
                frame = decode_frame(raw)
                assert isinstance(frame, RequestFrame)
                assert frame.path == "/upload"
                assert decode_body(frame.body, frame.encoding) == b"chunk"
                assert not current.binary_requests
                await _reply(replacement, frame.id)
                assert (await asyncio.wait_for(task, 2)).status_code == 204
                assert not current.in_flight
            finally:
                task.cancel()
                receiver.cancel()
                await asyncio.gather(task, receiver, return_exceptions=True)


async def test_binary_request_replacement_before_headers_is_connect_error() -> None:
    async with _tunnel([_CAPABILITY]) as (client, ws, registry):
        session = registry.get(_RUNNER)
        assert session is not None
        task = asyncio.create_task(
            client.patch(
                "/upload",
                content=b"chunk",
                extensions={"runner_tunnel_binary_request_body": True},
            )
        )
        try:
            raw = await asyncio.wait_for(ws.recv(), 2)
            assert isinstance(raw, bytes)
            async with connect(
                f"ws://127.0.0.1:{ws.remote_address[1]}/v1/runners/{_RUNNER}/tunnel"
            ) as replacement:
                await replacement.send(encode_frame(HelloFrame("replacement", 1)))
                with pytest.raises(httpx.ConnectError, match="tunnel replaced") as error:
                    await asyncio.wait_for(task, 2)
                assert error.value.request.url.path == "/upload"
                assert not session.binary_requests
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


async def test_deeply_nested_binary_header_keeps_runner_receive_loop_alive(caplog) -> None:
    nested = b"[" * 10_000 + b"0" + b"]" * 10_000
    header = (
        b'{"kind":"request","id":"deep-header","method":"PATCH",'
        b'"path":"/malformed","query_string":"","headers":[],"stream":true,'
        b'"nested":' + nested + b"}"
    )
    wire = b"OTB\x01" + len(header).to_bytes(4, "big") + header
    malformed_handled = asyncio.Event()
    dispatch_tasks: dict[str, asyncio.Task[None]] = {}

    async def app(scope, receive, send) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async with _tunnel([_CAPABILITY]) as (client, ws, registry):
        session = registry.get(_RUNNER)
        assert session is not None

        async def runner_receive_loop() -> None:
            while True:
                raw = await ws.recv()
                await _handle_tunnel_frame(app, raw, ws.send, dispatch_tasks, {})
                if raw == wire:
                    malformed_handled.set()

        receiver = asyncio.create_task(runner_receive_loop())
        malformed_waiter = asyncio.create_task(malformed_handled.wait())
        try:
            registry.open_request(_RUNNER, "deep-header")
            await registry.send_request(session, wire, request_id="deep-header")
            done, _ = await asyncio.wait(
                {receiver, malformed_waiter}, timeout=5, return_when=asyncio.FIRST_COMPLETED
            )
            assert malformed_waiter in done and malformed_waiter.result()
            assert "runner received malformed binary request" in caplog.text
            registry.close_request(_RUNNER, "deep-header")

            response = await asyncio.wait_for(
                client.patch(
                    "/after-malformed",
                    content=b"next",
                    extensions={"runner_tunnel_binary_request_body": True},
                ),
                5,
            )
            assert response.status_code == 204
            assert not receiver.done()
        finally:
            receiver.cancel()
            malformed_waiter.cancel()
            await asyncio.gather(receiver, malformed_waiter, return_exceptions=True)
            for task in dispatch_tasks.values():
                task.cancel()
            await asyncio.gather(*dispatch_tasks.values(), return_exceptions=True)


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
    ws: ClientConnection,
    app,
    binary_sizes: list[int] | None = None,
    sent_frames: list | None = None,
    send_frame: Callable[[str], Awaitable[None]] | None = None,
) -> AsyncIterator[dict[str, asyncio.Task[None]]]:
    dispatches: dict[str, asyncio.Task[None]] = {}

    async def send(raw: str) -> None:
        if sent_frames is not None:
            sent_frames.append(decode_frame(raw))
        await (send_frame(raw) if send_frame is not None else ws.send(raw))

    async def receive() -> None:
        async for raw in ws:
            if isinstance(raw, bytes) and binary_sizes is not None:
                binary_sizes.append(len(raw))
            await _handle_tunnel_frame(app, raw, send, dispatches, {})

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
    writer_resume = _pause_sender(monkeypatch)
    uploads_started = asyncio.Event()
    uploads_finish = asyncio.Event()
    active_uploads = 0

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
    writer_resume = _pause_sender(monkeypatch)
    if not queued:
        writer_resume.set()
    uploads_started = asyncio.Event()
    uploads_finish = asyncio.Event()
    active_uploads = 0
    peak_uploads = 0
    cancellations_received = 0

    async def app(scope, receive, send) -> None:
        nonlocal active_uploads, peak_uploads, cancellations_received
        if scope["path"] == "/upload":
            await receive()
            active_uploads += 1
            peak_uploads = max(peak_uploads, active_uploads)
            if active_uploads == 2:
                uploads_started.set()
            try:
                try:
                    await uploads_finish.wait()
                except asyncio.CancelledError:
                    # Delayed runner cleanup must keep admission after a cancel is written.
                    cancellations_received += 1
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
                        assert len(session.binary_pending_sends) == 2
                    writer_resume.set()
                    await asyncio.wait_for(uploads_started.wait(), 10)
                async with asyncio.timeout(2):
                    while cancellations_received < 2:
                        await asyncio.sleep(0.01)
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


async def test_oversized_streaming_binary_body_stops_at_envelope_limit() -> None:
    chunk_size = 64 * 1024
    consumed = 0
    source_closed = asyncio.Event()

    async def body() -> AsyncGenerator[bytes, None]:
        nonlocal consumed
        try:
            for _ in range(RUNNER_TUNNEL_MAX_MESSAGE_BYTES // chunk_size + 2):
                chunk = b"x" * chunk_size
                consumed += len(chunk)
                yield chunk
        finally:
            source_closed.set()

    class BodyStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.source = body()

        def __aiter__(self) -> AsyncIterator[bytes]:
            return self.source

        async def aclose(self) -> None:
            await self.source.aclose()

    async with _tunnel([_CAPABILITY]) as (client, ws, registry):
        request = httpx.Request(
            "PATCH",
            "http://runner/upload",
            stream=BodyStream(),
            extensions={"runner_tunnel_binary_request_body": True},
        )
        header = RequestFrame(
            "0" * 32,
            request.method,
            request.url.path,
            query_string=request.url.query.decode("utf-8"),
            headers=[[key, value] for key, value in request.headers.items()],
            stream=True,
        )
        body_budget = RUNNER_TUNNEL_MAX_MESSAGE_BYTES - len(encode_binary_request(header, b""))
        with pytest.raises(
            httpx.LocalProtocolError, match=r"binary request.*message limit"
        ) as error:
            await client.send(request)
        assert error.value.request is request
        assert body_budget < consumed <= body_budget + chunk_size
        assert source_closed.is_set()

        session = registry.get(_RUNNER)
        assert session is not None
        assert not session.binary_requests
        assert not session.in_flight
        assert session.outbound_queue.empty()

        retry = asyncio.create_task(
            client.patch(
                "/upload",
                content=b"retry",
                extensions={"runner_tunnel_binary_request_body": True},
            )
        )
        try:
            raw = await asyncio.wait_for(ws.recv(), 2)
            assert isinstance(raw, bytes)
            retry_id = _envelope_header(raw)["id"]
            assert session.binary_requests == {retry_id}
            await _reply(ws, retry_id)
            assert (await asyncio.wait_for(retry, 2)).status_code == 204
            assert not session.binary_requests
        finally:
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)


async def test_binary_envelope_releases_buffer_while_waiting_for_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message_limit = 16 * 1024 * 1024
    body_size = 8 * 1024 * 1024
    chunk_size = 64 * 1024
    monkeypatch.setattr(transport_module, "RUNNER_TUNNEL_MAX_MESSAGE_BYTES", message_limit)
    sender_resume = _pause_sender(monkeypatch)
    request_enqueued = asyncio.Event()
    allow_request = asyncio.Event()

    async with _tunnel([_CAPABILITY]) as (client, ws, registry):
        original_send_request = registry.send_request

        async def hold_request(session, data, *, request_id: str) -> None:
            await original_send_request(session, data, request_id=request_id)
            request_enqueued.set()
            await allow_request.wait()

        monkeypatch.setattr(registry, "send_request", hold_request)

        async def body() -> AsyncIterator[bytes]:
            chunk = b"x" * chunk_size
            for _ in range(body_size // chunk_size):
                yield chunk

        request = client.build_request(
            "PATCH",
            "/upload",
            content=body(),
            extensions={"runner_tunnel_binary_request_body": True},
        )
        gc.collect()
        tracemalloc.start()
        baseline, _ = tracemalloc.get_traced_memory()
        task = asyncio.create_task(client.send(request, stream=True))
        try:
            await asyncio.wait_for(request_enqueued.wait(), 5)
            gc.collect()
            retained, _ = tracemalloc.get_traced_memory()
            assert retained - baseline < body_size * 3 // 2

            allow_request.set()
            sender_resume.set()
            raw = await asyncio.wait_for(ws.recv(), 5)
            assert isinstance(raw, bytes)
            request_id = _envelope_header(raw)["id"]
            await _reply(ws, request_id)
            response = await asyncio.wait_for(task, 5)
            assert response.status_code == 204
            await response.aclose()
        finally:
            allow_request.set()
            sender_resume.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            tracemalloc.stop()


async def test_binary_request_closes_custom_stream_once_after_consumption() -> None:
    class NonIdempotentStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.close_calls = 0

        def __aiter__(self) -> AsyncIterator[bytes]:
            stream = self

            class Iterator:
                def __init__(self) -> None:
                    self.sent = False

                def __aiter__(self):
                    return self

                async def __anext__(self) -> bytes:
                    if self.sent:
                        raise StopAsyncIteration
                    self.sent = True
                    return b"payload"

                async def aclose(self) -> None:
                    await stream.aclose()

            return Iterator()

        async def aclose(self) -> None:
            self.close_calls += 1
            if self.close_calls > 1:
                raise RuntimeError("stream closed twice")

    async with _tunnel([_CAPABILITY]) as (client, ws, registry):
        stream = NonIdempotentStream()
        request = httpx.Request(
            "PATCH",
            "http://runner/upload",
            stream=stream,
            extensions={"runner_tunnel_binary_request_body": True},
        )
        task = asyncio.create_task(client.send(request, stream=True))
        try:
            raw = await asyncio.wait_for(ws.recv(), 2)
            assert isinstance(raw, bytes)
            request_id = _envelope_header(raw)["id"]
            assert stream.close_calls == 1
            await _reply(ws, request_id)
            response = await asyncio.wait_for(task, 2)
            assert response.status_code == 204
            await response.aclose()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        session = registry.get(_RUNNER)
        assert session is not None
        assert not session.binary_requests


async def test_overlimit_cleanup_error_preserves_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport_module, "RUNNER_TUNNEL_MAX_MESSAGE_BYTES", 1024 * 1024)
    chunk_size = 64 * 1024
    source_closed = asyncio.Event()

    async def body() -> AsyncGenerator[bytes, None]:
        try:
            for _ in range(1024 * 1024 // chunk_size + 2):
                yield b"x" * chunk_size
        finally:
            source_closed.set()
            raise ValueError("cleanup failed")

    class BodyStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.source = body()

        def __aiter__(self) -> AsyncIterator[bytes]:
            return self.source

        async def aclose(self) -> None:
            await self.source.aclose()

    async with _tunnel([_CAPABILITY]) as (client, _, _):
        request = httpx.Request(
            "PATCH",
            "http://runner/upload",
            stream=BodyStream(),
            extensions={"runner_tunnel_binary_request_body": True},
        )
        with pytest.raises(
            httpx.LocalProtocolError, match=r"binary request.*message limit"
        ) as error:
            await client.send(request)
        assert error.value.request is request
        assert source_closed.is_set()


async def test_overlimit_closes_standard_httpx_async_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport_module, "RUNNER_TUNNEL_MAX_MESSAGE_BYTES", 1024)
    source_closed = asyncio.Event()

    async def body() -> AsyncGenerator[bytes, None]:
        try:
            yield b"x" * 2048
        finally:
            source_closed.set()

    async with _tunnel([_CAPABILITY]) as (client, _, _):
        request = client.build_request(
            "PATCH",
            "/upload",
            content=body(),
            extensions={"runner_tunnel_binary_request_body": True},
        )
        with pytest.raises(httpx.LocalProtocolError, match=r"binary request.*message limit"):
            await client.send(request)

        assert source_closed.is_set()


async def test_overlimit_closes_public_wrapper_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport_module, "RUNNER_TUNNEL_MAX_MESSAGE_BYTES", 1024)

    class InnerStream:
        def __init__(self) -> None:
            self.closed = 0
            self.sent = False

        def __aiter__(self) -> InnerStream:
            return self

        async def __anext__(self) -> bytes:
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return b"x" * 2048

        async def aclose(self) -> None:
            self.closed += 1

    class WrapperStream(httpx.AsyncByteStream):
        def __init__(self, inner: InnerStream) -> None:
            self.inner = inner
            self._stream = inner
            self.close_calls = 0

        def __aiter__(self) -> AsyncIterator[bytes]:
            return self.inner

        async def aclose(self) -> None:
            self.close_calls += 1
            await self.inner.aclose()

    async with _tunnel([_CAPABILITY]) as (client, _, registry):
        inner = InnerStream()
        stream = WrapperStream(inner)
        request = httpx.Request(
            "PATCH",
            "http://runner/upload",
            stream=stream,
            extensions={"runner_tunnel_binary_request_body": True},
        )
        with pytest.raises(httpx.LocalProtocolError, match=r"binary request.*message limit"):
            await client.send(request)

        assert stream.close_calls == 1
        assert inner.closed == 1
        session = registry.get(_RUNNER)
        assert session is not None
        assert not session.binary_requests


class _NonIdempotentAsyncIterator:
    def __init__(self, chunk: bytes) -> None:
        self.chunk = chunk
        self.sent = False
        self.close_calls = 0

    def __aiter__(self) -> _NonIdempotentAsyncIterator:
        return self

    async def __anext__(self) -> bytes:
        if self.sent:
            raise StopAsyncIteration
        self.sent = True
        return self.chunk

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.close_calls > 1:
            raise RuntimeError("source closed twice")


class _ClosingAsyncIteratorByteStream(transport_module.AsyncIteratorByteStream):
    def __init__(self, source: _NonIdempotentAsyncIterator) -> None:
        super().__init__(source)
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        await self._stream.aclose()


class _InheritingAsyncIteratorByteStream(transport_module.AsyncIteratorByteStream):
    pass


async def test_async_iterator_byte_stream_subclass_closes_once_and_queues_frame() -> None:
    async with _tunnel([_CAPABILITY]) as (client, ws, registry):
        source = _NonIdempotentAsyncIterator(b"payload")
        stream = _ClosingAsyncIteratorByteStream(source)
        request = httpx.Request(
            "PATCH",
            "http://runner/upload",
            stream=stream,
            extensions={"runner_tunnel_binary_request_body": True},
        )
        task = asyncio.create_task(client.send(request, stream=True))
        try:
            raw = await asyncio.wait_for(ws.recv(), 2)
            assert isinstance(raw, bytes)
            request_id = _envelope_header(raw)["id"]
            assert stream.close_calls == 1
            assert source.close_calls == 1
            await _reply(ws, request_id)
            response = await asyncio.wait_for(task, 2)
            assert response.status_code == 204
            await response.aclose()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        session = registry.get(_RUNNER)
        assert session is not None
        assert not session.binary_requests


async def test_async_iterator_byte_stream_subclass_closes_once_on_overlimit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport_module, "RUNNER_TUNNEL_MAX_MESSAGE_BYTES", 1024)
    async with _tunnel([_CAPABILITY]) as (client, _, registry):
        source = _NonIdempotentAsyncIterator(b"x" * 2048)
        stream = _ClosingAsyncIteratorByteStream(source)
        request = httpx.Request(
            "PATCH",
            "http://runner/upload",
            stream=stream,
            extensions={"runner_tunnel_binary_request_body": True},
        )
        with pytest.raises(httpx.LocalProtocolError, match=r"binary request.*message limit"):
            await client.send(request)

        assert stream.close_calls == 1
        assert source.close_calls == 1
        session = registry.get(_RUNNER)
        assert session is not None
        assert not session.binary_requests
        assert not session.in_flight


async def test_async_iterator_byte_stream_inheriting_noop_closes_source_and_queues_frame() -> None:
    async with _tunnel([_CAPABILITY]) as (client, ws, registry):
        source = _NonIdempotentAsyncIterator(b"payload")
        stream = _InheritingAsyncIteratorByteStream(source)
        request = httpx.Request(
            "PATCH",
            "http://runner/upload",
            stream=stream,
            extensions={"runner_tunnel_binary_request_body": True},
        )
        task = asyncio.create_task(client.send(request, stream=True))
        try:
            raw = await asyncio.wait_for(ws.recv(), 2)
            assert isinstance(raw, bytes)
            request_id = _envelope_header(raw)["id"]
            assert source.close_calls == 1
            await _reply(ws, request_id)
            response = await asyncio.wait_for(task, 2)
            assert response.status_code == 204
            await response.aclose()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        session = registry.get(_RUNNER)
        assert session is not None
        assert not session.binary_requests


async def test_async_iterator_byte_stream_inheriting_noop_closes_source_on_overlimit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport_module, "RUNNER_TUNNEL_MAX_MESSAGE_BYTES", 1024)
    async with _tunnel([_CAPABILITY]) as (client, _, registry):
        source = _NonIdempotentAsyncIterator(b"x" * 2048)
        stream = _InheritingAsyncIteratorByteStream(source)
        request = httpx.Request(
            "PATCH",
            "http://runner/upload",
            stream=stream,
            extensions={"runner_tunnel_binary_request_body": True},
        )
        with pytest.raises(httpx.LocalProtocolError, match=r"binary request.*message limit"):
            await client.send(request)

        assert source.close_calls == 1
        session = registry.get(_RUNNER)
        assert session is not None
        assert not session.binary_requests
        assert not session.in_flight


async def test_body_value_error_propagates_and_releases_slot() -> None:
    failure = ValueError("upstream body read failed")

    async def body() -> AsyncIterator[bytes]:
        yield b"partial"
        raise failure

    async with _tunnel([_CAPABILITY]) as (client, _, registry):
        request = client.build_request(
            "PATCH",
            "/upload",
            content=body(),
            extensions={"runner_tunnel_binary_request_body": True},
        )
        with pytest.raises(ValueError) as error:
            await client.send(request)

        assert error.value is failure
        session = registry.get(_RUNNER)
        assert session is not None
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


async def test_post_head_close_recovers_binary_admission() -> None:
    blocked = asyncio.Event()
    started = 0

    async def app(scope, receive, send) -> None:
        nonlocal started
        await receive()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        if scope["path"] == "/upload":
            started += 1
            await blocked.wait()
        await send({"type": "http.response.body", "body": b""})

    frames: list = []
    async with _tunnel([_CAPABILITY]) as (client, ws, registry):
        session = registry.get(_RUNNER)
        assert session is not None
        async with _runner_dispatch(ws, app, sent_frames=frames) as dispatches:
            responses = []
            try:
                for _ in range(2):
                    response = await asyncio.wait_for(
                        client.send(
                            client.build_request(
                                "PATCH",
                                "/upload",
                                content=b"chunk",
                                extensions={"runner_tunnel_binary_request_body": True},
                            ),
                            stream=True,
                        ),
                        2,
                    )
                    responses.append(response)
                assert started == 2
                with pytest.raises(httpx.PoolTimeout):
                    await client.patch(
                        "/health", extensions={"runner_tunnel_binary_request_body": True}
                    )
                for response in responses:
                    await response.aclose()
                async with asyncio.timeout(2):
                    while dispatches:
                        await asyncio.sleep(0.01)
                async with asyncio.timeout(2):
                    while session.binary_requests:
                        await asyncio.sleep(0.01)
                ends = [frame for frame in frames if isinstance(frame, ResponseEndFrame)]
                assert len(ends) == 2
                assert len({frame.id for frame in ends}) == 2
                assert all(frame.error == "runner_request_cancelled" for frame in ends)
                response = await asyncio.wait_for(
                    client.patch(
                        "/health",
                        extensions={"runner_tunnel_binary_request_body": True},
                    ),
                    2,
                )
                assert response.status_code == 204
            finally:
                blocked.set()
                for response in responses:
                    await response.aclose()


async def test_cancelled_terminal_send_is_delivered_once_and_releases_binary_admission() -> None:
    terminal_started = asyncio.Event()
    terminal_cancelled = asyncio.Event()
    release_terminal = asyncio.Event()
    terminal_delivered = asyncio.Event()
    delivered_ends: list[ResponseEndFrame] = []

    async def app(scope, receive, send) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        try:
            await send({"type": "http.response.body", "body": b""})
        except asyncio.CancelledError:
            terminal_cancelled.set()
            raise

    async with _tunnel([_CAPABILITY]) as (client, ws, registry):
        session = registry.get(_RUNNER)
        assert session is not None
        original_send = ws.send

        async def send_frame(raw: str) -> None:
            frame = decode_frame(raw)
            if isinstance(frame, ResponseEndFrame):
                terminal_started.set()
                await release_terminal.wait()
            await original_send(raw)
            if isinstance(frame, ResponseEndFrame):
                delivered_ends.append(frame)
                terminal_delivered.set()

        frames: list = []
        async with _runner_dispatch(ws, app, sent_frames=frames, send_frame=send_frame):
            response = await client.send(
                client.build_request(
                    "PATCH",
                    "/upload",
                    content=b"chunk",
                    extensions={"runner_tunnel_binary_request_body": True},
                ),
                stream=True,
            )
            try:
                await asyncio.wait_for(terminal_started.wait(), 2)
                request_id = next(iter(session.binary_requests))
                assert len(session.binary_requests) == 1

                await response.aclose()
                await asyncio.wait_for(terminal_cancelled.wait(), 2)
                release_terminal.set()
                await asyncio.wait_for(terminal_delivered.wait(), 2)
                async with asyncio.timeout(2):
                    while request_id in session.binary_requests:
                        await asyncio.sleep(0.01)

                ends = [frame for frame in frames if isinstance(frame, ResponseEndFrame)]
                assert len(ends) == 1
                assert ends[0].error is None
                assert len(delivered_ends) == 1
                assert delivered_ends[0].id == request_id
                assert not session.binary_requests
            finally:
                release_terminal.set()
                await response.aclose()


async def test_cancel_after_completed_response_emits_no_duplicate_end() -> None:
    cleanup = asyncio.Event()

    async def app(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})
        await cleanup.wait()

    frames: list = []
    async with _tunnel([_CAPABILITY]) as (client, ws, _):
        async with _runner_dispatch(ws, app, sent_frames=frames) as dispatches:
            response = await asyncio.wait_for(
                client.send(
                    client.build_request(
                        "PATCH",
                        "/upload",
                        content=b"chunk",
                        extensions={"runner_tunnel_binary_request_body": True},
                    ),
                    stream=True,
                ),
                2,
            )
            try:
                async with asyncio.timeout(2):
                    while not any(isinstance(frame, ResponseEndFrame) for frame in frames):
                        await asyncio.sleep(0.01)
                await response.aclose()
                async with asyncio.timeout(2):
                    while dispatches:
                        await asyncio.sleep(0.01)
                ends = [frame for frame in frames if isinstance(frame, ResponseEndFrame)]
                assert len(ends) == 1
                assert ends[0].error is None
            finally:
                cleanup.set()
                await response.aclose()


async def test_pre_head_relay_timeout_sends_cancel_and_recovers_admission() -> None:
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def app(scope, receive, send) -> None:
        started.set()
        await blocked.wait()

    async def relay(client) -> None:
        async with asyncio.timeout(0.1):
            await client.patch(
                "/upload", content=b"chunk", extensions={"runner_tunnel_binary_request_body": True}
            )

    frames: list = []
    async with _tunnel([_CAPABILITY]) as (client, ws, registry):
        session = registry.get(_RUNNER)
        assert session is not None
        async with _runner_dispatch(ws, app, sent_frames=frames) as dispatches:
            task = asyncio.create_task(relay(client))
            try:
                await asyncio.wait_for(started.wait(), 2)
                with pytest.raises(TimeoutError):
                    await task
                async with asyncio.timeout(2):
                    while session.binary_requests or dispatches:
                        await asyncio.sleep(0.01)
                ends = [frame for frame in frames if isinstance(frame, ResponseEndFrame)]
                assert len(ends) == 1 and ends[0].error == "runner_request_cancelled"
            finally:
                blocked.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_cancel_queued_before_dispatch_starts_emits_one_end() -> None:
    async def app(scope, receive, send) -> None:
        await asyncio.Event().wait()

    # Request and cancel are processed in the same receive-loop turn, before
    # the newly created dispatch task has had a chance to start.
    async def peer(ws) -> None:
        tasks: dict[str, asyncio.Task[None]] = {}
        raw = await ws.recv()
        request = _envelope_header(raw)
        await _handle_tunnel_frame(app, raw, ws.send, tasks, {})
        await _handle_tunnel_frame(
            app, encode_frame(RequestCancelFrame(request["id"])), ws.send, tasks, {}
        )
        try:
            await asyncio.gather(*list(tasks.values()), return_exceptions=True)
            await ws.send(encode_frame(PingFrame(345)))
        finally:
            for task in list(tasks.values()):
                task.cancel()

    async with serve(peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(
                encode_binary_request(RequestFrame("queued-cancel", "PATCH", "/upload"), b"chunk")
            )
            frame = decode_frame(await asyncio.wait_for(ws.recv(), 2))
            assert isinstance(frame, ResponseEndFrame)
            assert frame.id == "queued-cancel" and frame.error == "runner_request_cancelled"
            assert isinstance(decode_frame(await asyncio.wait_for(ws.recv(), 2)), PingFrame)


async def test_cancelled_dispatch_preserves_cancellation_on_closed_sender(caplog) -> None:
    async def app(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.Event().wait()

    async def peer(ws) -> None:
        for _ in range(3):
            assert isinstance(decode_frame(await ws.recv()), ResponseHeadFrame)
        await ws.close(code=1011, reason="connection lost")

    caplog.set_level(logging.WARNING, logger=serve_module.__name__)
    async with serve(peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}") as ws:
            dispatches: dict[str, asyncio.Task[None]] = {}
            for i in range(3):
                await _handle_tunnel_frame(
                    app,
                    encode_frame(RequestFrame(f"sse-{i}", "GET", "/stream")),
                    ws.send,
                    dispatches,
                    {},
                )
            tasks = list(dispatches.values())
            await asyncio.wait_for(ws.wait_closed(), 2)
            await serve_module._cancel_dispatch_tasks(dispatches)
            assert all(task.cancelled() for task in tasks)
            assert not [
                record for record in caplog.records if record.name == serve_module.__name__
            ]


async def test_cancelled_terminal_send_is_bounded(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    monkeypatch.setattr(serve_module, "_CANCELLED_RESPONSE_END_TIMEOUT_S", 0.05, raising=False)
    never = asyncio.Event()
    head_received = asyncio.Event()
    terminal_started = asyncio.Event()

    async def app(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await never.wait()

    async def peer(ws) -> None:
        assert isinstance(decode_frame(await ws.recv()), ResponseHeadFrame)
        head_received.set()
        await ws.wait_closed()

    caplog.set_level(logging.WARNING, logger=serve_module.__name__)
    async with serve(peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}") as ws:

            async def stalled_send(raw: str) -> None:
                if isinstance(decode_frame(raw), ResponseEndFrame):
                    terminal_started.set()
                    await never.wait()
                await ws.send(raw)

            dispatches: dict[str, asyncio.Task[None]] = {}
            await _handle_tunnel_frame(
                app,
                encode_frame(RequestFrame("stalled-end", "GET", "/stream")),
                stalled_send,
                dispatches,
                {},
            )
            task = dispatches["stalled-end"]
            try:
                await asyncio.wait_for(head_received.wait(), 2)
                task.cancel()
                await asyncio.wait_for(terminal_started.wait(), 2)
                done, _ = await asyncio.wait({task}, timeout=0.5)
                assert task in done, (
                    "cancellation terminal send must not stall dispatch cancellation"
                )
                assert task.cancelled()
                assert not [
                    record for record in caplog.records if record.name == serve_module.__name__
                ]
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("shutdown_capable", [False, True], ids=["plain", "graceful"])
async def test_closed_tunnel_skips_cancellation_terminal_send(
    monkeypatch: pytest.MonkeyPatch,
    caplog,
    shutdown_capable: bool,
) -> None:
    dispatches: list[asyncio.Task[None]] = []
    terminal_attempts: list[str] = []
    original_send = TunnelDiagnostics.send

    async def observe_send(self, sender, data, *, app_ping_ts=None) -> None:
        if isinstance(data, str) and isinstance(decode_frame(data), ResponseEndFrame):
            terminal_attempts.append(data)
        await original_send(self, sender, data, app_ping_ts=app_ping_ts)

    monkeypatch.setattr(TunnelDiagnostics, "send", observe_send)

    async def app(scope, receive, send) -> None:
        task = asyncio.current_task()
        assert task is not None
        dispatches.append(task)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.Event().wait()

    async def peer(ws) -> None:
        assert isinstance(decode_frame(await ws.recv()), HelloFrame)
        await ws.send(encode_frame(RequestFrame("closed-tunnel", "GET", "/stream")))
        assert isinstance(decode_frame(await ws.recv()), ResponseHeadFrame)
        await ws.close(code=1000)

    caplog.set_level(logging.WARNING, logger=serve_module.__name__)
    async with serve(peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        await asyncio.wait_for(
            serve_module._serve_tunnel_once(
                app,
                tunnel_url=f"ws://127.0.0.1:{port}",
                server_url=f"http://127.0.0.1:{port}",
                runner_id=_RUNNER,
                runner_version="test",
                shutdown_event=asyncio.Event() if shutdown_capable else None,
            ),
            2,
        )
    assert len(dispatches) == 1 and dispatches[0].cancelled()
    assert not terminal_attempts
    assert not [record for record in caplog.records if record.name == serve_module.__name__]


async def test_binary_dispatch_deadline_recovers_slot_with_healthy_pings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(serve_module, "BINARY_REQUEST_DISPATCH_TIMEOUT_S", 0.3, raising=False)
    blocked = asyncio.Event()
    started = asyncio.Event()

    async def app(scope, receive, send) -> None:
        if scope["path"] == "/upload":
            started.set()
            await blocked.wait()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    frames: list = []
    async with _tunnel([_CAPABILITY]) as (client, ws, registry):
        session = registry.get(_RUNNER)
        assert session is not None
        async with _runner_dispatch(ws, app, sent_frames=frames):
            task = asyncio.create_task(
                client.patch(
                    "/upload",
                    content=b"chunk",
                    extensions={"runner_tunnel_binary_request_body": True},
                )
            )
            try:
                await asyncio.wait_for(started.wait(), 2)
                await registry.send_text(session, encode_frame(PingFrame(567)), app_ping_ts=567)
                async with asyncio.timeout(2):
                    while session.diagnostics.snapshot()["last_app_pong_received_age_s"] is None:
                        await asyncio.sleep(0.01)
                response = await asyncio.wait_for(task, 2)
                assert response.status_code == 504
                assert registry.get(_RUNNER) is session and not session.binary_requests
                ends = [frame for frame in frames if isinstance(frame, ResponseEndFrame)]
                assert len(ends) == 1
                response = await client.patch(
                    "/health", extensions={"runner_tunnel_binary_request_body": True}
                )
                assert response.status_code == 204
            finally:
                blocked.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
