"""Public uploads through the real server app and the runner tunnel."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from omnigent.entities import DEFAULT_ENVIRONMENT_ID
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import create_os_environment
from omnigent.runner.transports.ws_tunnel.frames import HelloFrame, RequestFrame, decode_frame
from omnigent.runner.transports.ws_tunnel.serve import _handle_tunnel_frame
from omnigent.runner.workspace_upload import UploadLimits, register_upload_routes
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.routes import routes_uploads as uploads
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
                    "upload_id": "a" * 32,
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

        async def client_for(*args, **kwargs):
            return test.runner

        async def wake(**kwargs):
            test.wake_calls += 1
            assert kwargs["raise_host_refusal"] is True
            assert kwargs["session_id"] == session_id
            return test.runner, kwargs["conv"]

        monkeypatch.setattr(uploads, "_get_runner_client_for_resource_access", client_for)
        monkeypatch.setattr(uploads, "ensure_runner_connected", wake)
        yield test


@pytest.mark.parametrize("enabled", [True, False], ids=["binary", "json-fallback"])
async def test_tunnel_upload_and_commit(scenario, tmp_path, monkeypatch, enabled):
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
            scenario.app.state.tunnel_registry = registry

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
                    == 2
                )
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.gather(*dispatches.values(), return_exceptions=True)
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
async def test_declaration_validation(scenario, fields, code):
    response = await scenario.create(**fields)
    assert response.status_code in (400, 403)
    if response.status_code == 400:
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
