"""Server-side copies of one in-flight upload chunk, relayed over an uncompressed tunnel."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tracemalloc
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request

from omnigent.entities import DEFAULT_ENVIRONMENT_ID
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.runner.transports.ws_tunnel.transport import WSTunnelTransport
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.routes import routes_uploads as uploads
from omnigent.server.routes.runner_tunnel import create_runner_tunnel_router
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent
from tests.server.routes.test_workspace_uploads import ALICE, HEADERS, UploadScenario

CHUNK = 8 * 1024**2
MAX_COPIES = 3
# Headers, frames and other per-request objects are far smaller than one chunk.
SLACK = 512 * 1024
# Multipart reads keep one chunk buffer plus small receive parts.
MAX_READ_PEAK = 1.2


@pytest.mark.parametrize(
    ("size", "part"),
    [(CHUNK, 64 * 1024), (1024**2, 16)],
    ids=["8MiB-in-64KiB-parts", "1MiB-in-16B-parts"],
)
async def test_multi_part_read_stays_near_one_copy(size: int, part: int) -> None:
    payload = os.urandom(size)
    offsets = iter(range(0, size, part))

    async def receive() -> dict:
        start = next(offsets, size)
        # Slicing allocates a fresh part per message, as uvicorn does for each receive().
        body = payload[start : start + part]
        return {"type": "http.request", "body": body, "more_body": start + part < size}

    request = Request(
        {
            "type": "http",
            "method": "PATCH",
            "headers": [(b"content-length", str(size).encode())],
        },
        receive,
    )
    tracemalloc.start()
    try:
        baseline = tracemalloc.get_traced_memory()[0]
        body = await uploads._read_capped(request, size)
        peak = tracemalloc.get_traced_memory()[1] - baseline
    finally:
        tracemalloc.stop()
    assert body == payload
    print(f"{size}B in {part}B parts: read peak={peak} bytes ({peak / size:.2f}x)")
    assert peak <= MAX_READ_PEAK * size, f"read peak {peak / size:.2f}x payload"


async def test_declared_length_preallocates_exact_buffer_for_multipart_body(monkeypatch) -> None:
    payload = os.urandom(CHUNK)
    offsets = iter(range(0, len(payload), 64 * 1024))
    preallocated_sizes: list[int] = []
    original_bytes_io = uploads.io.BytesIO

    class TrackingBytesIO(original_bytes_io):
        def write(self, data: bytes) -> int:
            written = super().write(data)
            if data == b"\0":
                preallocated_sizes.append(self.tell())
            return written

    monkeypatch.setattr(uploads.io, "BytesIO", TrackingBytesIO)

    async def receive() -> dict:
        start = next(offsets, len(payload))
        body = payload[start : start + 64 * 1024]
        return {
            "type": "http.request",
            "body": body,
            "more_body": start + 64 * 1024 < len(payload),
        }

    request = Request(
        {
            "type": "http",
            "method": "PATCH",
            "headers": [(b"content-length", str(len(payload)).encode())],
        },
        receive,
    )
    body = await uploads._read_capped(request, len(payload))
    assert body == payload
    assert preallocated_sizes == [len(payload)]


async def test_unknown_length_preallocates_one_capped_buffer(monkeypatch) -> None:
    payload = os.urandom(CHUNK)
    offsets = iter(range(0, len(payload), 64 * 1024))
    preallocated_sizes: list[int] = []
    original_bytes_io = uploads.io.BytesIO

    class TrackingBytesIO(original_bytes_io):
        def write(self, data: bytes) -> int:
            written = super().write(data)
            if data == b"\0":
                preallocated_sizes.append(self.tell())
            return written

    monkeypatch.setattr(uploads.io, "BytesIO", TrackingBytesIO)

    async def receive() -> dict:
        start = next(offsets, len(payload))
        body = payload[start : start + 64 * 1024]
        return {
            "type": "http.request",
            "body": body,
            "more_body": start + 64 * 1024 < len(payload),
        }

    request = Request({"type": "http", "method": "PATCH", "headers": []}, receive)
    body = await uploads._read_capped(request, len(payload))
    assert body == payload
    assert preallocated_sizes == [len(payload)]


async def test_declared_body_longer_than_content_length_is_rejected() -> None:
    async def receive() -> dict:
        return {"type": "http.request", "body": b"four", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "PATCH",
            "headers": [(b"content-length", b"3")],
        },
        receive,
    )
    with pytest.raises(uploads.UploadError) as error:
        await uploads._read_capped(request, 8)
    assert error.value.code == uploads.ErrorCode.TOO_LARGE


async def test_declared_length_over_cap_is_rejected_before_reading() -> None:
    calls = 0

    async def receive() -> dict:
        nonlocal calls
        calls += 1
        return {"type": "http.request", "body": b"x", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "PATCH",
            "headers": [(b"content-length", b"9")],
        },
        receive,
    )
    with pytest.raises(uploads.UploadError) as error:
        await uploads._read_capped(request, 8)
    assert error.value.code == uploads.ErrorCode.TOO_LARGE
    assert calls == 0


async def test_declared_body_shorter_than_content_length_is_rejected() -> None:
    async def receive() -> dict:
        return {"type": "http.request", "body": b"four", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "PATCH",
            "headers": [(b"content-length", b"5")],
        },
        receive,
    )
    with pytest.raises(uploads.UploadError) as error:
        await uploads._read_capped(request, 8)
    assert error.value.code == uploads.ErrorCode.UPLOAD_RELAY_FAILED


_RSS_PROBE = r"""
import asyncio
import gc
import json
import os

import psutil
from fastapi import Request

from omnigent.server.routes import routes_uploads as uploads

chunk_size = 8 * 1024**2
payload = os.urandom(chunk_size)


async def read_once():
    offsets = iter(range(0, len(payload), 64 * 1024))

    async def receive():
        start = next(offsets, len(payload))
        end = start + 64 * 1024
        return {
            "type": "http.request",
            "body": payload[start:end],
            "more_body": end < len(payload),
        }

    request = Request({"type": "http", "method": "PATCH", "headers": []}, receive)
    return await uploads._read_capped(request, len(payload))


async def main():
    try:
        process = psutil.Process()
        baseline = process.memory_info().rss
    except (OSError, psutil.Error) as exc:
        print(json.dumps({"unavailable": str(exc)}))
        return

    for _ in range(25):
        body = await read_once()
        assert body == payload
        del body
        gc.collect()

    try:
        growth = process.memory_info().rss - baseline
    except (OSError, psutil.Error) as exc:
        print(json.dumps({"unavailable": str(exc)}))
        return
    print(json.dumps({"growth": growth}))


asyncio.run(main())
"""


def test_repeated_unknown_length_reads_bound_rss_growth() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _RSS_PROBE],
        check=True,
        capture_output=True,
        cwd=os.getcwd(),
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        text=True,
        timeout=90,
    )
    measurement = json.loads(result.stdout.strip().splitlines()[-1])
    if "unavailable" in measurement:
        pytest.skip(f"process RSS is unavailable: {measurement['unavailable']}")
    growth = measurement["growth"]
    print(f"25 repeated 8 MiB unknown-length reads: RSS growth={growth / 1024**2:.2f} MiB")
    assert growth < 64 * 1024**2, f"RSS grew {growth / 1024**2:.2f} MiB"


# The runner lives in a child process so tracemalloc sees only server-side allocations.
_RUNNER = r"""
import asyncio, sys, time, uuid
from fastapi import FastAPI, Request, Response
from websockets.asyncio.client import connect
from omnigent.runner.transports.ws_tunnel.frames import HelloFrame, encode_frame
from omnigent.runner.transports.ws_tunnel.serve import _handle_tunnel_frame
from omnigent.runner.workspace_upload import UploadLimits
from omnigent.util.tunnel_limits import RUNNER_TUNNEL_MAX_MESSAGE_BYTES

app = FastAPI()
base = "/v1/sessions/{sid}/resources/environments/{eid}/uploads"


@app.get(base)
async def probe(sid: str, eid: str):
    return {"supported": True, "limits": UploadLimits().public()}


@app.post(base, status_code=201)
async def create(sid: str, eid: str):
    return {
        "upload_id": uuid.uuid4().hex,
        "offset": 0,
        "chunk_bytes": 1024**2,
        "expires_at": time.time() + 3600,
    }


@app.patch(base + "/{uid}")
async def append(request: Request, sid: str, eid: str, uid: str):
    end = int(request.headers["upload-offset"]) + len(await request.body())
    return Response(status_code=204, headers={"Upload-Offset": str(end)})


async def main():
    async with connect(
        sys.argv[1], max_size=RUNNER_TUNNEL_MAX_MESSAGE_BYTES, compression=None
    ) as ws:
        capabilities = ["workspace_upload_v1", "binary_request_body_v1"]
        await ws.send(encode_frame(HelloFrame("copies", 1, capabilities=capabilities)))
        dispatches = {}
        async for raw in ws:
            await _handle_tunnel_frame(app, raw, ws.send, dispatches, {})


asyncio.run(main())
"""


@asynccontextmanager
async def _subprocess_tunnel(runner_id: str) -> AsyncIterator[TunnelRegistry]:
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
        runner = None
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            runner = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _RUNNER,
                f"ws://127.0.0.1:{port}/v1/runners/{runner_id}/tunnel",
                env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            )
            assert await registry.wait_for_runner(runner_id, timeout_s=30) is not None
            yield registry
        finally:
            if runner is not None and runner.returncode is None:
                runner.kill()
                await runner.wait()
            server.should_exit = True
            await asyncio.wait_for(server_task, 5)


async def test_in_flight_chunk_has_at_most_three_server_copies(
    auth_app, auth_client, db_uri, tmp_path, monkeypatch
):
    config = tmp_path / "upload-config.yaml"
    config.write_text("runner_tunnel_binary_request_body: true\n")
    monkeypatch.setenv("OMNIGENT_CONFIG", str(config))
    auth_app.state.feature_flags = FeatureFlags(frozenset({Feature.WORKSPACE_UPLOAD}))
    auth_client.headers["X-Forwarded-Email"] = ALICE
    agent = await create_test_agent(auth_client)
    created = await auth_client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    store = SqlAlchemyConversationStore(db_uri)
    store.set_runner_id(session_id, "copies")
    scenario = UploadScenario(
        auth_app,
        auth_client,
        f"/v1/sessions/{session_id}/resources/environments/{DEFAULT_ENVIRONMENT_ID}/uploads",
        session_id,
    )
    async with (
        _subprocess_tunnel("copies") as registry,
        httpx.AsyncClient(
            transport=WSTunnelTransport(registry, "copies"), base_url="http://runner"
        ) as runner,
    ):
        routing = scenario.bind(registry, store, monkeypatch)

        async def wake(**kwargs):
            return runner, kwargs["conv"]

        monkeypatch.setattr(uploads, "ensure_runner_connected", wake)
        try:
            url = await scenario.url(size=2 * CHUNK)
            # The first chunk warms lazy imports and per-connection buffers.
            warm = await auth_client.patch(url, headers=HEADERS, content=os.urandom(CHUNK))
            assert warm.status_code == 204, warm.text
            tracemalloc.start()
            try:
                baseline = tracemalloc.get_traced_memory()[0]
                # In process, the ASGI body is this object, so it counts as the received copy.
                chunk = os.urandom(CHUNK)
                response = await auth_client.patch(
                    url, headers={**HEADERS, "Upload-Offset": str(CHUNK)}, content=chunk
                )
                peak = tracemalloc.get_traced_memory()[1] - baseline
            finally:
                tracemalloc.stop()
            assert response.status_code == 204, response.text
            assert response.headers["Upload-Offset"] == str(2 * CHUNK)
            copies = peak / CHUNK
            print(f"in-flight chunk peak={peak} bytes copies={copies:.2f}")
            assert peak <= MAX_COPIES * CHUNK + SLACK, f"{copies:.2f} live copies"
        finally:
            await routing.aclose()
