"""Public uploads through the real server app and the runner tunnel."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from omnigent.entities import DEFAULT_ENVIRONMENT_ID
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import create_os_environment
from omnigent.runner.routing import RunnerRouter
from omnigent.runner.transports.ws_tunnel.frames import HelloFrame, RequestFrame, decode_frame
from omnigent.runner.transports.ws_tunnel.serve import _handle_tunnel_frame
from omnigent.runner.workspace_upload import UploadLimits, register_upload_routes
from omnigent.runtime import _globals
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.routes import routes_uploads as uploads
from omnigent.server.routes._sessions import common as session_common
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.runner.transports.ws_tunnel.test_binary_requests import _tunnel
from tests.server.helpers import create_test_agent, start_session_stream_collector

HEADERS = {"Content-Type": "application/offset+octet-stream", "Upload-Offset": "0"}
ALICE = "alice@example.test"
BOB = "bob@example.test"


@dataclass
class UploadScenario:
    app: FastAPI
    client: httpx.AsyncClient
    base: str
    session_id: str
    requests: list[httpx.Request] = field(default_factory=list)
    failure: httpx.Response | Exception | None = None
    blocked: asyncio.Event | None = None
    offset: int = 0
    size: int = 3
    name: str = "payload.bin"
    wake_calls: int = 0
    hard: dict = field(default_factory=lambda: UploadLimits().public())
    runner: httpx.AsyncClient | None = None
    routing: RunnerRouter | None = None

    def bind(self, registry, store, monkeypatch, client=None, **kwargs):
        self.routing = self.app.state.runner_router
        monkeypatch.setattr(self.routing, "_registry", registry)
        monkeypatch.setattr(self.routing, "_conversation_store", store)
        for name, value in kwargs.items():
            monkeypatch.setattr(self.routing, "_" + name, value)
        self.routing._clients.clear()
        if client is not None:
            self.routing._clients["binary-test"] = client
        self.app.state.tunnel_registry = registry
        monkeypatch.setattr(_globals, "_runner_router", self.routing)
        monkeypatch.setattr(session_common, "_server_runner_router", self.routing)
        return self.routing

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method != "GET":
            if self.blocked is not None:
                await self.blocked.wait()
            if isinstance(self.failure, Exception):
                raise self.failure
            if self.failure is not None:
                return self.failure
        if request.method == "GET":
            return httpx.Response(200, json={"supported": True, "limits": self.hard})
        if request.method == "POST" and not request.url.path.endswith("/complete"):
            declaration = json.loads(request.content)
            self.size, self.name = declaration["size"], declaration["name"]
            return httpx.Response(
                201,
                json={
                    "upload_id": uuid.uuid4().hex,
                    "offset": 0,
                    "chunk_bytes": 1024**2,
                    "expires_at": time.time() + 3600,
                },
            )
        if request.method == "PATCH":
            self.offset += len(request.content)
            return httpx.Response(204, headers={"Upload-Offset": str(self.offset)})
        if request.method == "HEAD":
            return httpx.Response(
                200, headers={"Upload-Offset": str(self.offset), "Upload-Length": str(self.size)}
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(
            200,
            json={
                "path": self.name,
                "size": self.size,
                "sha256": "0" * 64,
                "outcome": "created",
                "final_name": self.name,
            },
        )

    async def create(self, **fields: object) -> httpx.Response:
        return await self.client.post(
            self.base, json={"dir": "", "name": "payload.bin", "size": 3, **fields}
        )

    async def url(self, **fields: object) -> str:
        response = await self.create(**fields)
        assert response.status_code == 201, response.text
        return self.base + "/" + response.json()["upload_id"]


@pytest.fixture
async def scenario(auth_app, auth_client, db_uri, tmp_path, monkeypatch):
    config = tmp_path / "upload-config.yaml"
    config.write_text("{}\n")
    monkeypatch.setenv("OMNIGENT_CONFIG", str(config))
    auth_app.state.feature_flags = FeatureFlags(frozenset({Feature.WORKSPACE_UPLOAD}))
    auth_client.headers["X-Forwarded-Email"] = ALICE
    agent = await create_test_agent(auth_client)
    response = await auth_client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    SqlAlchemyConversationStore(db_uri).set_runner_id(session_id, "binary-test")
    test = UploadScenario(
        auth_app,
        auth_client,
        f"/v1/sessions/{session_id}/resources/environments/{DEFAULT_ENVIRONMENT_ID}/uploads",
        session_id,
    )
    tunnel = SimpleNamespace(hello=HelloFrame("test", 1, capabilities=["workspace_upload_v1"]))
    auth_app.state.tunnel_registry = SimpleNamespace(get=lambda runner_id: tunnel)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(test.handle), base_url="http://runner"
    ) as runner:
        test.runner = runner

        test.bind(
            auth_app.state.tunnel_registry,
            SqlAlchemyConversationStore(db_uri),
            monkeypatch,
            runner,
        )

        async def wake(**kwargs):
            test.wake_calls += 1
            assert kwargs["raise_host_refusal"] is True
            assert kwargs["session_id"] == kwargs["conv"].id
            return test.runner, kwargs["conv"]

        monkeypatch.setattr(uploads, "ensure_runner_connected", wake)
        yield test


@pytest.mark.parametrize("enabled", [True, False], ids=["binary", "json-fallback"])
async def test_tunnel_upload_and_commit(scenario, tmp_path, monkeypatch, db_uri, enabled):
    config = tmp_path / "tunnel-config.yaml"
    config.write_text(f"runner_tunnel_binary_request_body: {str(enabled).lower()}\n")
    monkeypatch.setenv("OMNIGENT_CONFIG", str(config))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = create_os_environment(
        OSEnvSpec(cwd=str(workspace), sandbox=OSEnvSandboxSpec(type="none"))
    )
    runner_app = FastAPI()

    async def environment(sid, eid):
        return env

    async def registry_for(sid):
        return None

    manager = register_upload_routes(
        runner_app,
        resolve_environment=environment,
        resolve_registry=registry_for,
        state_path=tmp_path / "state" / "index.json",
    )
    wires: list[str | bytes] = []
    dispatches = {}
    collector = await start_session_stream_collector(scenario.session_id)
    try:
        async with _tunnel(["workspace_upload_v1", "binary_request_body_v1"]) as (
            runner,
            ws,
            registry,
        ):
            scenario.runner = runner
            routing = scenario.bind(registry, SqlAlchemyConversationStore(db_uri), monkeypatch)

            async def dispatch():
                async for raw in ws:
                    wires.append(raw)
                    await _handle_tunnel_frame(runner_app, raw, ws.send, dispatches, {})

            task = asyncio.create_task(dispatch())
            try:
                payload = bytes(range(256)) * 1024 + b"\x00\xff"
                url = await scenario.url(
                    size=len(payload), sha256=hashlib.sha256(payload).hexdigest()
                )
                assert scenario.wake_calls == 1
                for start, chunk in [(0, payload[:131072]), (131072, payload[131072:])]:
                    response = await scenario.client.patch(
                        url, headers={**HEADERS, "Upload-Offset": str(start)}, content=chunk
                    )
                    assert response.status_code == 204, response.text
                    assert response.headers["Upload-Offset"] == str(start + len(chunk))
                await collector.assert_no_event(0.01)
                head = await scenario.client.head(url)
                assert head.status_code == 200
                assert head.headers["Upload-Offset"] == str(len(payload))
                assert head.headers["Upload-Length"] == str(len(payload))
                assert head.headers["Cache-Control"] == "no-store"
                mismatch = await scenario.client.patch(url, headers=HEADERS, content=b"")
                assert mismatch.status_code == 409
                assert mismatch.json()["error"]["code"] == "offset_mismatch"
                assert mismatch.headers["Upload-Offset"] == str(len(payload))
                past_end = await scenario.client.patch(
                    url,
                    headers={**HEADERS, "Upload-Offset": str(len(payload) + 1)},
                    content=b"",
                )
                assert past_end.status_code == 409
                assert past_end.headers["Upload-Offset"] == str(len(payload))
                overflow = await scenario.client.patch(
                    url,
                    headers={**HEADERS, "Upload-Offset": str(len(payload))},
                    content=b"x",
                )
                assert overflow.status_code == 413
                complete = await scenario.client.post(url + "/complete")
                assert complete.status_code == 200, complete.text
                assert complete.json()["sha256"] == hashlib.sha256(payload).hexdigest()
                assert (workspace / "payload.bin").read_bytes() == payload
                event = await collector.next_event()
                assert event["type"] == "session.changed_files.invalidated"
                assert event["environment_id"] == DEFAULT_ENVIRONMENT_ID
                assert (await scenario.client.post(url + "/complete")).json() == complete.json()
                await collector.assert_no_event(0.02)
                assert (await scenario.client.delete(url)).json() == complete.json()
                assert (workspace / "payload.bin").read_bytes() == payload
                if enabled:
                    binary = [raw for raw in wires if isinstance(raw, bytes)]
                    assert len(binary) == 5
                    assert binary[0].startswith(b"OTB\x01")
                    header_length = int.from_bytes(binary[0][4:8], "big")
                    assert binary[0][8 + header_length :] == payload[:131072]
                else:
                    assert all(isinstance(raw, str) for raw in wires)
                    chunks = [decode_frame(raw) for raw in wires if isinstance(raw, str)]
                    patches = [
                        frame
                        for frame in chunks
                        if isinstance(frame, RequestFrame) and frame.method == "PATCH"
                    ]
                    assert len(patches) == 5
                    assert patches[0].encoding == "base64"
                aborted = await scenario.url(name="abort.bin", size=3)
                assert (await scenario.client.delete(aborted)).status_code == 204
                assert (await scenario.client.head(aborted)).status_code == 410
                assert not list(workspace.glob("*.part"))
                probes = [decode_frame(raw) for raw in wires if isinstance(raw, str)]
                assert (
                    sum(
                        isinstance(frame, RequestFrame) and frame.method == "GET"
                        for frame in probes
                    )
                    == 1
                )
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.gather(*dispatches.values(), return_exceptions=True)
                await routing.aclose()
    finally:
        await collector.stop()
        await manager.stop()
        env.close()


@pytest.mark.parametrize(
    "method,suffix",
    [
        ("GET", ""),
        ("POST", ""),
        ("PATCH", "/unknown"),
        ("HEAD", "/unknown"),
        ("POST", "/unknown/complete"),
        ("DELETE", "/unknown"),
    ],
)
async def test_disabled_routes(scenario, method, suffix):
    url = await scenario.url()
    scenario.requests.clear()
    scenario.wake_calls = 0
    scenario.app.state.feature_flags = FeatureFlags()
    target = (
        url + ("/complete" if suffix.endswith("complete") else "") if suffix else scenario.base
    )
    response = await scenario.client.request(
        method,
        target,
        headers=HEADERS,
        json={"dir": "", "name": "another", "size": 1}
        if method == "POST" and not suffix
        else None,
        content=b"x" if method == "PATCH" else None,
    )
    assert response.status_code == 404
    assert scenario.requests == []
    assert scenario.wake_calls == 0


@pytest.mark.parametrize(
    "level,method,suffix",
    [
        (1, "GET", ""),
        (1, "POST", ""),
        *[
            (level, method, suffix)
            for level in (1, 2)
            for method, suffix in [
                ("PATCH", "/owned"),
                ("HEAD", "/owned"),
                ("POST", "/owned/complete"),
                ("DELETE", "/owned"),
            ]
        ],
    ],
)
async def test_auth_and_server_ownership(scenario, db_uri, level, method, suffix):
    store = SqlAlchemyPermissionStore(db_uri)
    if level == 1:
        # A caller's own id still requires EDIT after their grant is downgraded.
        store.grant(BOB, scenario.session_id, 2)
        scenario.client.headers["X-Forwarded-Email"] = BOB
    url = await scenario.url()
    scenario.client.headers["X-Forwarded-Email"] = ALICE
    granted = await scenario.client.put(
        f"/v1/sessions/{scenario.session_id}/permissions",
        json={"user_id": BOB, "level": level},
    )
    assert granted.status_code == 200, granted.text
    scenario.client.headers["X-Forwarded-Email"] = BOB
    scenario.requests.clear()
    target = (
        url + ("/complete" if suffix.endswith("complete") else "") if suffix else scenario.base
    )
    response = await scenario.client.request(
        method,
        target,
        headers=HEADERS if suffix else {},
        content=b"x" if method == "PATCH" else None,
        json={"dir": "", "name": "b", "size": 1} if method == "POST" and not suffix else None,
    )
    assert response.status_code == 403, response.text
    if method != "HEAD":
        assert response.json()["error"]["code"] == (
            "forbidden" if level == 1 else "upload_owner_mismatch"
        )
    assert scenario.requests == []


@pytest.mark.parametrize(
    "method,suffix",
    [("POST", ""), ("PATCH", "/owned"), ("POST", "/owned/complete"), ("DELETE", "/owned")],
)
async def test_untrusted_origin(scenario, monkeypatch, method, suffix):
    url = await scenario.url()
    monkeypatch.setenv("OMNIGENT_WS_ALLOWED_ORIGINS", "https://trusted.example.test")
    scenario.requests.clear()
    response = await scenario.client.request(
        method,
        url + ("/complete" if suffix.endswith("complete") else "") if suffix else scenario.base,
        headers={**HEADERS, "Origin": "https://untrusted.example.test"},
        content=b"x" if method == "PATCH" else None,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert scenario.requests == []


async def test_missing_capability_after_auth_and_wake(scenario, db_uri):
    scenario.app.state.tunnel_registry = SimpleNamespace(
        get=lambda runner_id: SimpleNamespace(hello=HelloFrame("old", 1))
    )
    SqlAlchemyPermissionStore(db_uri).grant(BOB, scenario.session_id, 1)
    scenario.client.headers["X-Forwarded-Email"] = BOB
    assert (await scenario.create()).status_code == 403
    assert scenario.wake_calls == 0
    scenario.client.headers["X-Forwarded-Email"] = ALICE
    before = time.monotonic()
    response = await scenario.create()
    assert time.monotonic() - before < 1
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "runner_capability_mismatch"
    assert scenario.wake_calls == 1
    assert scenario.requests == []
    cap = await scenario.client.get(scenario.base)
    assert cap.json()["supported"] is False


@pytest.mark.parametrize("length", [None, "1"], ids=["absent", "lying"])
async def test_chunk_cap_without_content_length(scenario, monkeypatch, length):
    monkeypatch.setattr(uploads.config, "workspace_upload_chunk_max_bytes", lambda: 256 * 1024)
    url = await scenario.url(size=300000)
    scenario.requests.clear()

    async def stream():
        yield b"a" * 200000
        yield b"b" * 62145
        raise AssertionError("Server kept consuming an oversized body")

    headers = {**HEADERS, **({"Content-Length": length} if length is not None else {})}
    response = await scenario.client.patch(url, headers=headers, content=stream())
    assert response.status_code == 413
    assert scenario.requests == []
    assert uploads._chunk_admission.used == 0


@pytest.mark.parametrize(
    "headers,status",
    [
        ({**HEADERS, "Content-Type": "text/plain"}, 415),
        ({**HEADERS, "Upload-Offset": "-1"}, 400),
        ({**HEADERS, "Upload-Offset": "1.5"}, 400),
        ({"Content-Type": HEADERS["Content-Type"]}, 400),
    ],
)
async def test_chunk_headers(scenario, headers, status):
    url = await scenario.url()
    scenario.requests.clear()
    assert (await scenario.client.patch(url, headers=headers, content=b"x")).status_code == status
    assert scenario.requests == []


@pytest.mark.parametrize(
    "fields,code",
    [
        ({"dir": "../"}, "invalid_path"),
        ({"dir": "/tmp"}, "invalid_path"),
        ({"dir": "C:\\temp"}, "invalid_path"),
        ({"dir": "C:temp"}, "invalid_path"),
        ({"name": "../x"}, "invalid_name"),
        ({"name": "."}, "invalid_name"),
        ({"name": "a\x00"}, "invalid_name"),
        ({"created_by": "bob"}, "invalid_request"),
    ],
)
async def test_declaration_validation(scenario, db_uri, fields, code):
    SqlAlchemyPermissionStore(db_uri).grant(ALICE, scenario.session_id, LEVEL_OWNER)
    scenario.client.headers["X-Forwarded-Email"] = ALICE
    response = await scenario.create(**fields)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == code
    assert scenario.requests == []


async def test_create_body_cap(scenario):
    response = await scenario.client.post(
        scenario.base,
        content=b" " * (uploads.CREATE_BODY_MAX_BYTES + 1),
        headers={"Content-Type": "application/json", "Content-Length": "1"},
    )
    assert response.status_code == 413
    assert scenario.requests == []


async def test_admission_and_release(scenario, monkeypatch):
    url = await scenario.url()
    monkeypatch.setattr(uploads, "INFLIGHT_CHUNK_MAX_BYTES", 1)
    scenario.requests.clear()
    response = await scenario.client.patch(url, headers=HEADERS, content=b"x")
    assert response.status_code == 429
    assert 1 <= int(response.headers["Retry-After"]) <= 5
    assert scenario.requests == []
    assert uploads._chunk_admission.used == 0


@pytest.mark.parametrize(
    "error,status",
    [
        (httpx.PoolTimeout("full"), 429),
        (httpx.LocalProtocolError("oversize"), 413),
        (httpx.ConnectError("/private/path"), 502),
    ],
)
async def test_transport_failures(scenario, error, status):
    url = await scenario.url()
    scenario.failure = error
    response = await scenario.client.patch(url, headers=HEADERS, content=b"x")
    assert response.status_code == status
    assert "/private" not in response.text
    if status == 429:
        assert 1 <= int(response.headers["Retry-After"]) <= 5
    assert uploads._chunk_admission.used == 0


async def test_chunk_deadline(scenario, monkeypatch):
    url = await scenario.url()
    monkeypatch.setattr(uploads.config, "workspace_upload_chunk_timeout_s", lambda: 0.01)
    scenario.blocked = asyncio.Event()
    async with asyncio.timeout(0.2):
        response = await scenario.client.patch(url, headers=HEADERS, content=b"x")
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "timeout"
    assert uploads._chunk_admission.used == 0
    scenario.blocked = None
    assert (await scenario.client.head(url)).headers["Upload-Offset"] == "0"


@pytest.mark.parametrize(
    "code,status",
    [
        ("invalid_name", 400),
        ("invalid_path", 400),
        ("file_exists", 409),
        ("is_directory", 409),
        ("offset_mismatch", 409),
        ("incomplete", 409),
        ("not_found", 404),
        ("gone", 410),
        ("permission_denied", 403),
        ("busy", 429),
        ("upload_limit", 429),
        ("no_space", 507),
        ("runner_capability_mismatch", 503),
        ("runner_unavailable", 503),
        ("io_error", 500),
    ],
)
async def test_runner_error_mapping(scenario, code, status):
    url = await scenario.url()
    scenario.failure = httpx.Response(
        status,
        json={"error": {"code": code, "message": "at /private/workspace/path"}},
        headers={"Upload-Offset": "2", "Retry-After": "60"},
    )
    response = await scenario.client.patch(url, headers=HEADERS, content=b"x")
    assert response.status_code == (502 if status == 500 else status)
    assert response.json()["error"]["code"] == ("upload_relay_failed" if status == 500 else code)
    assert "/private" not in response.text
    if code == "offset_mismatch":
        assert response.headers["Upload-Offset"] == "2"
    if status == 429:
        assert response.headers["Retry-After"] == "60"


async def test_limits_frozen_and_session_quota(scenario, monkeypatch):
    monkeypatch.setattr(uploads.config, "workspace_upload_max_open_per_session", lambda: 1)
    monkeypatch.setattr(uploads.config, "workspace_upload_chunk_max_bytes", lambda: 256 * 1024)
    monkeypatch.setattr(uploads.config, "workspace_upload_max_file_bytes", lambda: 2)
    capability = (await scenario.client.get(scenario.base)).json()
    assert capability["limits"]["max_file_bytes"] == 2
    assert capability["limits"]["chunk"]["max"] == 256 * 1024
    assert (await scenario.create()).status_code == 413
    monkeypatch.setattr(uploads.config, "workspace_upload_max_file_bytes", lambda: 1000000)
    url = await scenario.url(size=300000)
    response = await scenario.create(name="second", size=1)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    monkeypatch.setattr(uploads.config, "workspace_upload_chunk_max_bytes", lambda: 8 * 1024**2)
    scenario.requests.clear()
    assert (
        await scenario.client.patch(url, headers=HEADERS, content=b"x" * 262145)
    ).status_code == 413
    assert scenario.requests == []
    assert (await scenario.client.delete(url)).status_code == 204
    assert (await scenario.client.head(url)).status_code == 410
    assert (await scenario.create(size=1)).status_code == 201


async def test_complete_deadline_scales_with_size(scenario, monkeypatch):
    url = await scenario.url(size=1024**3)
    original_relay = uploads._relay
    deadlines = []

    async def relay(*args, **kwargs):
        deadlines.append(kwargs["timeout"])
        return await original_relay(*args, **kwargs)

    monkeypatch.setattr(uploads, "_relay", relay)
    monkeypatch.setattr(uploads.config, "workspace_upload_finalize_timeout_s", lambda: 1)
    response = await scenario.client.post(url + "/complete")
    assert response.status_code == 200
    assert deadlines == [425]


async def test_complete_timeout_can_replay(scenario, monkeypatch):
    from omnigent.inner import os_env_upload

    url = await scenario.url()
    monkeypatch.setattr(os_env_upload, "COMPLETE_TIMEOUT_FLOOR_S", 0.01)
    monkeypatch.setattr(os_env_upload, "COMPLETE_TIMEOUT_PER_GIB_S", 0)
    monkeypatch.setattr(uploads, "FINALIZE_RELAY_MARGIN_S", 0)
    monkeypatch.setattr(uploads.config, "workspace_upload_finalize_timeout_s", lambda: 0.01)
    scenario.blocked = asyncio.Event()
    async with asyncio.timeout(0.2):
        response = await scenario.client.post(url + "/complete")
    assert response.status_code == 504
    scenario.blocked = None
    response = await scenario.client.post(url + "/complete")
    assert response.status_code == 200
    assert (await scenario.client.post(url + "/complete")).json() == response.json()


async def test_capability_limits_and_unavailable(scenario, monkeypatch):
    for name in ("max_file_bytes", "max_open_bytes_per_session", "max_batch_bytes"):
        monkeypatch.setattr(uploads.config, "workspace_upload_" + name, lambda: 10 * 1024**3)
    for name in ("max_open_per_session", "parallel_files", "max_files_per_batch"):
        monkeypatch.setattr(uploads.config, "workspace_upload_" + name, lambda: 1000)
    scenario.hard = {
        "max_file_bytes": 500000,
        "chunk": {"min": 262144, "initial": 262144, "max": 524288},
        "max_open_bytes": 600000,
        "max_open_uploads": 3,
        "max_files_per_batch": 9,
        "max_batch_bytes": 700000,
        "parallel_files": 1,
    }
    response = await scenario.client.get(scenario.base)
    assert response.json()["limits"] == scenario.hard
    for key in ("max_files_per_batch", "max_batch_bytes", "parallel_files"):
        del scenario.hard[key]
    previous = scenario.app.state.tunnel_registry.get("binary-test")
    fresh = SimpleNamespace(hello=previous.hello)
    scenario.app.state.tunnel_registry.get = lambda rid: fresh
    response = await scenario.client.get(scenario.base)
    assert response.json()["limits"]["max_files_per_batch"] == 1000
    assert response.json()["limits"]["max_batch_bytes"] == 10 * 1024**3
    assert response.json()["limits"]["parallel_files"] == 1000
    scenario.app.state.tunnel_registry = SimpleNamespace(get=lambda rid: None)
    response = await scenario.client.get(scenario.base)
    assert response.json()["supported"] is False
    assert response.json()["reason"] == "runner_unavailable"
    assert (await scenario.create()).json()["error"]["code"] == "runner_unavailable"


@pytest.mark.parametrize("setting", ["staging_idle_ttl_s", "staging_max_age_s"])
async def test_server_expiry(scenario, monkeypatch, setting):
    monkeypatch.setattr(uploads.config, "workspace_upload_" + setting, lambda: 0.01)
    url = await scenario.url()
    scenario.requests.clear()
    await asyncio.sleep(0.02)
    assert (await scenario.client.head(url)).status_code == 410
    assert scenario.requests == []


async def test_stalled_body_releases_cross_session_admission(scenario, db_uri, monkeypatch):
    url = await scenario.url()
    agent = await create_test_agent(scenario.client)
    response = await scenario.client.post("/v1/sessions", json={"agent_id": agent["id"]})
    other = response.json()["id"]
    SqlAlchemyConversationStore(db_uri).set_runner_id(other, "binary-test")
    response = await scenario.client.post(
        scenario.base.replace(scenario.session_id, other),
        json={"dir": "", "name": "other.bin", "size": 3},
    )
    assert response.status_code == 201
    fresh = scenario.base.replace(scenario.session_id, other) + "/" + response.json()["upload_id"]
    monkeypatch.setattr(uploads.config, "workspace_upload_chunk_timeout_s", lambda: 0.02)
    scenario.requests.clear()

    async def stalled():
        yield b"x"
        await asyncio.Event().wait()

    response = await asyncio.wait_for(
        scenario.client.patch(url, headers=HEADERS, content=stalled()), 0.5
    )
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "timeout"
    assert uploads._chunk_admission.used == 0
    assert scenario.requests == []
    assert (await scenario.client.patch(fresh, headers=HEADERS, content=b"x")).status_code == 204
    assert uploads._chunk_admission.used == 0


@pytest.mark.parametrize(
    "method,suffix",
    [
        ("GET", ""),
        ("POST", ""),
        ("PATCH", "/unknown"),
        ("HEAD", "/unknown"),
        ("POST", "/unknown/complete"),
        ("DELETE", "/unknown"),
    ],
)
async def test_remote_runner_readdresses_before_local_state(
    scenario, db_uri, monkeypatch, method, suffix
):
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(
        scenario.session_id, "00000000-0000-0000-0000-000000000123", workspace="/private/workspace"
    )
    scenario.bind(
        SimpleNamespace(get=lambda rid: None),
        store,
        monkeypatch,
        host_registry=SimpleNamespace(get=lambda hid: None),
        host_store=SimpleNamespace(is_online=lambda hid: True),
    )
    response = await scenario.client.request(
        method,
        scenario.base + suffix,
        headers=HEADERS if method == "PATCH" else {},
        json={"dir": "", "name": "x", "size": 1} if method == "POST" and not suffix else None,
    )
    assert response.status_code == 400
    if method != "HEAD":
        assert response.json()["error"]["code"] == "wrong_replica"
        assert "/private" not in response.text
    assert scenario.wake_calls == 0
    assert scenario.requests == []


@pytest.mark.parametrize(
    "code,status",
    [
        (ErrorCode.WORKSPACE_MISSING, 410),
        (ErrorCode.RUNNER_UNAVAILABLE, 503),
        (ErrorCode.WRONG_REPLICA, 400),
    ],
)
async def test_wake_refusals_are_sanitized(scenario, monkeypatch, code, status):
    async def refused(**kwargs):
        raise OmnigentError(
            "workspace path does not exist: /private/workspace/credentials", code=code
        )

    monkeypatch.setattr(uploads, "ensure_runner_connected", refused)
    response = await scenario.create()
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "/private" not in response.text
    assert scenario.requests == []


async def test_capability_and_relay_require_the_same_routed_client(scenario, monkeypatch):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(scenario.handle), base_url="http://elsewhere"
    ) as other:

        async def different(*args, **kwargs):
            return other

        monkeypatch.setattr(uploads, "_get_runner_client_for_resource_access", different)
        response = await scenario.create()
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "runner_capability_mismatch"
        assert scenario.requests == []


async def test_capability_probe_coalesces_and_reconnect_invalidates(scenario, monkeypatch):
    original = scenario.handle
    blocked = asyncio.Event()
    entered = asyncio.Event()
    probes = 0

    async def handle(request):
        nonlocal probes
        if request.method == "GET":
            probes += 1
            entered.set()
            await blocked.wait()
        return await original(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://runner"
    ) as client:
        scenario.routing._clients["binary-test"] = client
        tasks = [asyncio.create_task(scenario.client.get(scenario.base)) for _ in range(3)]
        try:
            await asyncio.wait_for(entered.wait(), 0.5)
            tunnel = scenario.app.state.tunnel_registry.get("binary-test")
            probe = tunnel.workspace_upload_capabilities[
                (scenario.session_id, DEFAULT_ENVIRONMENT_ID)
            ]
            # Release the probe only once both other requests await the shared future.
            async with asyncio.timeout(0.5):
                while len(probe._done_callbacks) < 2:
                    await asyncio.sleep(0)
            blocked.set()
            results = await asyncio.gather(*tasks)
            assert all(r.json()["supported"] for r in results)
            assert probes == 1
            assert (await scenario.create()).status_code == 201
            assert probes == 1
            fresh = SimpleNamespace(
                hello=HelloFrame("new", 1, capabilities=["workspace_upload_v1"])
            )
            scenario.app.state.tunnel_registry.get = lambda rid: fresh
            assert (await scenario.client.get(scenario.base)).json()["supported"]
            assert probes == 2
        finally:
            blocked.set()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("concurrent", [False, True], ids=["sequential", "concurrent"])
async def test_completion_retries_publish_once(scenario, monkeypatch, concurrent):
    url = await scenario.url()
    events = []
    monkeypatch.setattr(
        uploads, "_publish_changed_files_invalidated", lambda *args: events.append(args)
    )
    if concurrent:
        scenario.blocked = asyncio.Event()
        tasks = [asyncio.create_task(scenario.client.post(url + "/complete")) for _ in range(2)]
        try:
            async with asyncio.timeout(0.5):
                while sum(r.url.path.endswith("/complete") for r in scenario.requests) != 2:
                    await asyncio.sleep(0)
            scenario.blocked.set()
            results = await asyncio.gather(*tasks)
        finally:
            scenario.blocked.set()
            await asyncio.gather(*tasks, return_exceptions=True)
    else:
        results = [await scenario.client.post(url + "/complete") for _ in range(2)]
    assert all(r.status_code == 200 for r in results)
    assert results[0].json() == results[1].json()
    assert events == [(scenario.session_id, DEFAULT_ENVIRONMENT_ID)]
    assert (await scenario.create()).status_code == 201


@pytest.mark.parametrize("ordering", ["delete-first", "complete-first", "aborted"])
async def test_commit_receipt_publishes_once_from_either_route(scenario, monkeypatch, ordering):
    url = await scenario.url()
    events = []
    monkeypatch.setattr(
        uploads, "_publish_changed_files_invalidated", lambda *args: events.append(args)
    )
    if ordering == "aborted":
        assert (await scenario.client.delete(url)).status_code == 204
        assert events == []
    else:
        scenario.failure = httpx.Response(
            200,
            json={
                "path": scenario.name,
                "size": scenario.size,
                "sha256": "0" * 64,
                "outcome": "created",
                "final_name": scenario.name,
            },
        )
        operations = [
            lambda: scenario.client.delete(url),
            lambda: scenario.client.post(url + "/complete"),
        ]
        if ordering == "complete-first":
            operations.reverse()
        results = []
        for operation in operations:
            results.append(await operation())
            assert results[-1].status_code == 200
            assert events == [(scenario.session_id, DEFAULT_ENVIRONMENT_ID)]
        assert results[0].json() == results[1].json()
        scenario.failure = None
    assert (await scenario.create()).status_code == 201


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("unexpected transport failure"),
        AssertionError("unexpected transport failure"),
        httpx.ReadTimeout("probe deadline exceeded"),
        asyncio.CancelledError(),
    ],
    ids=["runtime-error", "assertion-error", "typed-timeout", "cancellation"],
)
async def test_unexpected_capability_error_does_not_poison_connection(scenario, failure):
    probes = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handle(request):
        nonlocal probes
        probes += 1
        if probes == 1:
            entered.set()
            await release.wait()
            raise failure
        return await scenario.handle(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://runner"
    ) as client:
        scenario.routing._clients["binary-test"] = client
        tasks = [asyncio.create_task(scenario.client.get(scenario.base))]
        try:
            await asyncio.wait_for(entered.wait(), 0.5)
            tasks.append(asyncio.create_task(scenario.client.get(scenario.base)))
            tunnel = scenario.app.state.tunnel_registry.get("binary-test")
            probe = tunnel.workspace_upload_capabilities[
                (scenario.session_id, DEFAULT_ENVIRONMENT_ID)
            ]
            # Fail the probe only once the second request is awaiting the shared future.
            async with asyncio.timeout(0.5):
                while not probe._done_callbacks:
                    await asyncio.sleep(0)
            release.set()
            cancelled = isinstance(failure, asyncio.CancelledError)
            expected_status = 504 if isinstance(failure, httpx.ReadTimeout) else 502
            expected_code = "timeout" if expected_status == 504 else "upload_relay_failed"
            leader = await asyncio.wait_for(tasks[0], 0.5)
            if cancelled:
                # The app books a cancellation escaping the route as an empty 499.
                assert leader.status_code == 499
                assert leader.content == b""
            else:
                assert leader.status_code == expected_status
                assert leader.json()["error"]["code"] == expected_code
                assert "unexpected transport failure" not in leader.text
            waiter = await asyncio.wait_for(tasks[1], 0.5)
            assert waiter.status_code == expected_status
            assert waiter.json()["error"]["code"] == expected_code
            if not cancelled:
                assert leader.json() == waiter.json()
            assert probes == 1
            response = await asyncio.wait_for(scenario.client.get(scenario.base), 0.5)
            assert response.status_code == 200
            assert response.json()["supported"] is True
            assert probes == 2
            assert (await scenario.client.get(scenario.base)).json()["supported"] is True
            assert probes == 2
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize(
    "failure", [httpx.ReadTimeout("lost acknowledgement"), httpx.Response(502)]
)
async def test_ambiguous_create_does_not_hold_session_quota(scenario, monkeypatch, failure):
    monkeypatch.setattr(uploads.config, "workspace_upload_max_open_per_session", lambda: 1)
    scenario.failure = failure
    for _ in range(2):
        response = await scenario.create()
        assert response.status_code in (502, 504)
    scenario.failure = None
    assert (await scenario.create()).status_code == 201


async def test_cross_site_simple_complete_is_rejected(scenario, monkeypatch):
    url = await scenario.url()
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "0")
    monkeypatch.delenv("OMNIGENT_WS_ALLOWED_ORIGINS", raising=False)
    scenario.requests.clear()
    response = await scenario.client.post(
        url + "/complete",
        headers={
            "Origin": "https://attacker.example.test",
            "Sec-Fetch-Site": "cross-site",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        content="submit=1",
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
    assert scenario.requests == []
    assert (
        await scenario.client.post(url + "/complete", headers={"Sec-Fetch-Site": "same-origin"})
    ).status_code == 200


@pytest.mark.parametrize("expired", [False, True], ids=["completed-history", "expiry-backlog"])
def test_index_work_is_bounded_across_sessions(expired):
    class NoScan(dict):
        reads = 0

        def get(self, *args):
            self.reads += 1
            return super().get(*args)

        def items(self):
            raise AssertionError("Upload admission scanned unrelated history")

        def values(self):
            raise AssertionError("Upload admission scanned unrelated history")

        def __iter__(self):
            raise AssertionError("Upload admission scanned unrelated history")

    index = uploads.UploadIndex()
    now = time.monotonic()
    limits = uploads._limits()
    for i in range(2000):
        key = (f"session-{i}", "os", str(i))
        record = uploads.UploadRecord(
            "owner",
            1,
            limits,
            now - (100 if expired else 0),
            now - (100 if expired else 0),
            10,
            20,
            "runner",
        )
        with index.lock:
            index.add(key, record)
            index.finish(key, record)
    overdue_key = ("current", "os", "expired")
    overdue = uploads.UploadRecord("owner", 1, limits, now - 50, now - 50, 10, 20, "runner")
    with index.lock:
        index.add(overdue_key, overdue)
    index.records = NoScan(index.records)
    index.prune("current")
    assert index.records.reads <= uploads.EXPIRY_CLEANUP_LIMIT + 1
    assert len(index.records) >= 2000 - uploads.EXPIRY_CLEANUP_LIMIT
    assert not index.sessions
    record = uploads.UploadRecord("owner", 7, uploads._limits(), now, now, 10, 20, "runner")
    key = ("current", "os", "new")
    with index.lock:
        index.add(key, record)
        assert len(index.sessions["current"].keys) == 1
        assert index.sessions["current"].size == 7
        assert index.finish(key, record)
        assert not index.finish(key, record)
        assert not index.sessions


@pytest.mark.parametrize("lost_leg", ["browser", "runner"])
async def test_create_key_recovers_lost_response_without_double_admission(scenario, lost_leg: str):
    scenario.hard["max_open_uploads"] = 1
    key = "a" * 32
    ids: dict[str, str] = {}
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.method == "GET":
            return await scenario.handle(request)
        scenario.requests.append(request)
        assert request.headers.get("Idempotency-Key") == key
        calls += 1
        uid = ids.setdefault(key, uuid.uuid4().hex)
        if lost_leg == "runner" and calls == 1:
            raise httpx.ReadTimeout("runner acknowledgement lost")
        return httpx.Response(
            201,
            json={
                "upload_id": uid,
                "offset": 2 if calls == 2 else 0,
                "chunk_bytes": 1024**2,
                "expires_at": time.time() + 3600,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://runner"
    ) as runner:
        scenario.routing._clients["binary-test"] = runner
        first = await scenario.client.post(
            scenario.base,
            json={"dir": "", "name": "payload.bin", "size": 3},
            headers={"Idempotency-Key": key},
        )
        assert first.status_code == (504 if lost_leg == "runner" else 201)
        replay = await scenario.client.post(
            scenario.base,
            json={"dir": "", "name": "payload.bin", "size": 3},
            headers={"Idempotency-Key": key},
        )
        assert replay.status_code == 201, replay.text
        assert replay.json()["upload_id"] == ids[key]
        assert replay.json()["offset"] == 2
        assert len(ids) == 1
        assert calls == 2
        assert (
            await scenario.client.post(
                scenario.base,
                json={"dir": "", "name": "other.bin", "size": 3},
                headers={"Idempotency-Key": "b" * 32},
            )
        ).status_code == 429
        assert calls == 2


async def test_create_key_mismatch_is_typed_and_stops_before_runner(scenario):
    key = "a" * 32
    first = await scenario.client.post(
        scenario.base,
        json={"dir": "", "name": "payload.bin", "size": 3},
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 201
    sent = len(scenario.requests)
    changed = await scenario.client.post(
        scenario.base,
        json={"dir": "", "name": "different.bin", "size": 3},
        headers={"Idempotency-Key": key},
    )
    assert changed.status_code == 422
    assert changed.json()["error"]["code"] == "idempotency_key_conflict"
    assert len(scenario.requests) == sent


async def test_runner_create_key_conflict_passes_through_typed(scenario):
    scenario.failure = httpx.Response(
        422, json={"error": {"code": "idempotency_key_conflict", "message": "Key changed"}}
    )
    response = await scenario.client.post(
        scenario.base,
        json={"dir": "", "name": "payload.bin", "size": 3},
        headers={"Idempotency-Key": "a" * 32},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_conflict"


@pytest.mark.parametrize("key", ["A" * 32, "a" * 31, "g" * 32])
async def test_invalid_create_key_is_rejected_before_runner(scenario, key: str):
    response = await scenario.client.post(
        scenario.base,
        json={"dir": "", "name": "payload.bin", "size": 3},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert not any(request.method == "POST" for request in scenario.requests)


async def test_pending_create_key_waits_without_another_admission(scenario, monkeypatch):
    scenario.hard["max_open_uploads"] = 1
    key = "a" * 32
    uid = uuid.uuid4().hex
    entered = asyncio.Event()
    duplicate_ready = asyncio.Event()
    release = asyncio.Event()
    posts = 0
    capability_calls = 0
    limits = uploads._limits

    def tracked_limits(*args, **kwargs):
        nonlocal capability_calls
        result = limits(*args, **kwargs)
        capability_calls += 1
        if capability_calls == 2:
            duplicate_ready.set()
        return result

    monkeypatch.setattr(uploads, "_limits", tracked_limits)

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "GET":
            return await scenario.handle(request)
        posts += 1
        entered.set()
        await release.wait()
        return httpx.Response(
            201,
            json={
                "upload_id": uid,
                "offset": 0,
                "chunk_bytes": 1024**2,
                "expires_at": time.time() + 3600,
            },
        )

    async def create(name: str = "payload.bin") -> httpx.Response:
        return await scenario.client.post(
            scenario.base,
            json={"dir": "", "name": name, "size": 3},
            headers={"Idempotency-Key": key},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://runner"
    ) as runner:
        scenario.routing._clients["binary-test"] = runner
        first = asyncio.create_task(create())
        await asyncio.wait_for(entered.wait(), 5)
        duplicate = asyncio.create_task(create())
        try:
            await asyncio.wait_for(duplicate_ready.wait(), 5)
            changed = await asyncio.wait_for(create("different.bin"), 5)
            assert changed.status_code == 422
            assert changed.json()["error"]["code"] == "idempotency_key_conflict"
            assert not duplicate.done()
        finally:
            release.set()
        first_result, duplicate_result = await asyncio.wait_for(
            asyncio.gather(first, duplicate), 5
        )
        assert first_result.status_code == duplicate_result.status_code == 201
        assert first_result.json()["upload_id"] == duplicate_result.json()["upload_id"] == uid
        assert posts == 1


async def test_failed_pending_create_key_wakes_waiters_and_clears_reservation(scenario):
    scenario.hard["max_open_uploads"] = 1
    key = "a" * 32
    uid = uuid.uuid4().hex
    entered = asyncio.Event()
    release = asyncio.Event()
    posts = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "GET":
            return await scenario.handle(request)
        posts += 1
        if posts == 1:
            entered.set()
            await release.wait()
            raise httpx.ReadTimeout("runner acknowledgement lost")
        return httpx.Response(
            201,
            json={
                "upload_id": uid,
                "offset": 0,
                "chunk_bytes": 1024**2,
                "expires_at": time.time() + 3600,
            },
        )

    async def create() -> httpx.Response:
        return await scenario.client.post(
            scenario.base,
            json={"dir": "", "name": "payload.bin", "size": 3},
            headers={"Idempotency-Key": key},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://runner"
    ) as runner:
        scenario.routing._clients["binary-test"] = runner
        first = asyncio.create_task(create())
        await asyncio.wait_for(entered.wait(), 5)
        duplicate = asyncio.create_task(create())
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(duplicate), 0.2)
        finally:
            release.set()
        first_result, duplicate_result = await asyncio.wait_for(
            asyncio.gather(first, duplicate), 5
        )
        assert first_result.status_code == 504
        assert duplicate_result.status_code in {502, 504}
        assert posts == 1
        retry = await create()
        assert retry.status_code == 201
        assert retry.json()["upload_id"] == uid
        assert posts == 2


@pytest.mark.parametrize("fail_first", [False, True], ids=["success", "failure"])
async def test_pending_create_key_across_event_loops(scenario, monkeypatch, fail_first):
    scenario.hard["max_open_uploads"] = 1
    key = "a" * 32
    uid = uuid.uuid4().hex
    entered = asyncio.Event()
    release = asyncio.Event()
    follower_ready = threading.Event()
    limits = uploads._limits
    calls = 0
    posts = 0

    def tracked_limits(*args, **kwargs):
        nonlocal calls
        result = limits(*args, **kwargs)
        calls += 1
        if calls == 2:
            follower_ready.set()
        return result

    monkeypatch.setattr(uploads, "_limits", tracked_limits)

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "GET":
            return await scenario.handle(request)
        posts += 1
        if posts == 1:
            entered.set()
            await release.wait()
            if fail_first:
                raise httpx.ReadTimeout("runner acknowledgement lost")
        return httpx.Response(
            201,
            json={
                "upload_id": uid,
                "offset": 0,
                "chunk_bytes": 1024**2,
                "expires_at": time.time() + 3600,
            },
        )

    async def create(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            scenario.base,
            json={"dir": "", "name": "payload.bin", "size": 3},
            headers={"Idempotency-Key": key},
        )

    def create_on_other_loop() -> httpx.Response:
        async def request() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=scenario.app),
                base_url="http://test",
                headers={"X-Forwarded-Email": ALICE},
            ) as client:
                return await create(client)

        return asyncio.run(request())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://runner"
    ) as runner:
        scenario.routing._clients["binary-test"] = runner
        leader = asyncio.create_task(create(scenario.client))
        await asyncio.wait_for(entered.wait(), 5)
        follower = asyncio.create_task(asyncio.to_thread(create_on_other_loop))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(follower_ready.wait, 5), 5)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(follower), 0.2)
        finally:
            release.set()
        first, second = await asyncio.wait_for(asyncio.gather(leader, follower), 5)
        assert posts == 1
        if fail_first:
            assert first.status_code == 504
            assert second.status_code in {502, 504}
            retry = await create(scenario.client)
            assert retry.status_code == 201
            assert retry.json()["upload_id"] == uid
            assert posts == 2
        else:
            assert first.status_code == second.status_code == 201
            assert first.json()["upload_id"] == second.json()["upload_id"] == uid


async def test_finished_create_key_replay_does_not_reopen_admission(scenario):
    scenario.hard["max_open_uploads"] = 1
    ids = {key * 32: uuid.uuid4().hex for key in "abc"}
    creates: dict[str, int] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return await scenario.handle(request)
        if request.url.path.endswith("/complete"):
            return await scenario.handle(request)
        if request.method == "DELETE":
            return await scenario.handle(request)
        key = request.headers["Idempotency-Key"]
        creates[key] = creates.get(key, 0) + 1
        return httpx.Response(
            201,
            json={
                "upload_id": ids[key],
                "offset": 3 if creates[key] > 1 else 0,
                "chunk_bytes": 1024**2,
                "expires_at": time.time() + 3600,
            },
        )

    async def create(key: str) -> httpx.Response:
        return await scenario.client.post(
            scenario.base,
            json={"dir": "", "name": "payload.bin", "size": 3},
            headers={"Idempotency-Key": key},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://runner"
    ) as runner:
        scenario.routing._clients["binary-test"] = runner
        first = await create("a" * 32)
        assert first.status_code == 201
        completed = await scenario.client.post(scenario.base + "/" + ids["a" * 32] + "/complete")
        assert completed.status_code == 200
        other = await create("b" * 32)
        assert other.status_code == 201
        replay = await create("a" * 32)
        assert replay.status_code == 201
        assert replay.json()["upload_id"] == ids["a" * 32]
        assert replay.json()["offset"] == 3
        assert creates["a" * 32] == 2
        assert other.json()["upload_id"] == ids["b" * 32]
        deleted = await scenario.client.delete(scenario.base + "/" + ids["b" * 32])
        assert deleted.status_code == 204
        next_upload = await create("c" * 32)
        assert next_upload.status_code == 201
        assert next_upload.json()["upload_id"] == ids["c" * 32]


async def test_finished_create_key_replacement_obeys_full_cap(scenario):
    scenario.hard["max_open_uploads"] = 1
    ids = {name: uuid.uuid4().hex for name in ("a", "replacement", "b", "c")}
    creates = 0
    aborted: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal creates
        if request.method in ("GET", "HEAD") or request.url.path.endswith("/complete"):
            return await scenario.handle(request)
        if request.method == "DELETE":
            aborted.append(request.url.path.rsplit("/", 1)[-1])
            return await scenario.handle(request)
        key = request.headers["Idempotency-Key"]
        if key == "a" * 32:
            creates += 1
            uid = ids["a" if creates == 1 else "replacement"]
        else:
            uid = ids[key[0]]
        return httpx.Response(
            201,
            json={
                "upload_id": uid,
                "offset": 0,
                "chunk_bytes": 1024**2,
                "expires_at": time.time() + 3600,
            },
        )

    async def create(key: str) -> httpx.Response:
        return await scenario.client.post(
            scenario.base,
            json={"dir": "", "name": "payload.bin", "size": 3},
            headers={"Idempotency-Key": key * 32},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://runner"
    ) as runner:
        scenario.routing._clients["binary-test"] = runner
        assert (await create("a")).status_code == 201
        assert (
            await scenario.client.post(scenario.base + "/" + ids["a"] + "/complete")
        ).status_code == 200
        assert (await create("b")).status_code == 201
        replay = await create("a")
        assert replay.status_code == 429
        assert replay.json()["error"]["code"] == "upload_limit"
        assert creates == 2
        assert aborted == [ids["replacement"]]
        assert (
            await scenario.client.head(scenario.base + "/" + ids["replacement"])
        ).status_code == 404
        assert (await scenario.client.head(scenario.base + "/" + ids["b"])).status_code == 200
        assert (await scenario.client.delete(scenario.base + "/" + ids["b"])).status_code == 204
        assert (await create("c")).status_code == 201


async def test_create_key_runner_id_change_retires_stale_admission(scenario):
    scenario.hard["max_open_uploads"] = 1
    ids = [uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex]
    posts = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method in ("GET", "HEAD", "DELETE"):
            return await scenario.handle(request)
        posts += 1
        return httpx.Response(
            201,
            json={
                "upload_id": ids[posts - 1],
                "offset": 0,
                "chunk_bytes": 1024**2,
                "expires_at": time.time() + 3600,
            },
        )

    async def create(key: str) -> httpx.Response:
        return await scenario.client.post(
            scenario.base,
            json={"dir": "", "name": "payload.bin", "size": 3},
            headers={"Idempotency-Key": key},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://runner"
    ) as runner:
        scenario.routing._clients["binary-test"] = runner
        first = await create("a" * 32)
        replay = await create("a" * 32)
        assert first.status_code == replay.status_code == 201
        assert first.json()["upload_id"] == ids[0]
        assert replay.json()["upload_id"] == ids[1]
        old = await scenario.client.head(scenario.base + "/" + ids[0])
        assert old.status_code == 404
        current = await scenario.client.head(scenario.base + "/" + ids[1])
        assert current.status_code == 200
        assert (await scenario.client.delete(scenario.base + "/" + ids[1])).status_code == 204
        other = await create("b" * 32)
        assert other.status_code == 201
        assert other.json()["upload_id"] == ids[2]
