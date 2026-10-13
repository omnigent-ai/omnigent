"""Runner upload routes, hard limits and indexed cleanup."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from omnigent.entities import DEFAULT_ENVIRONMENT_ID
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import CallerProcessOSEnvironment, create_os_environment
from omnigent.runner.workspace_upload import UploadLimits, register_upload_routes
from omnigent.runtime.filesystem_registry import AgentEditFilesystemRegistry

BASE = f"/v1/sessions/session/resources/environments/{DEFAULT_ENVIRONMENT_ID}/uploads"
HEADERS = {"Content-Type": "application/offset+octet-stream", "Upload-Offset": "0"}


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = create_os_environment(
        OSEnvSpec(cwd=str(workspace), sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert isinstance(env, CallerProcessOSEnvironment)
    app = FastAPI()

    async def resolve(session_id: str, environment_id: str) -> CallerProcessOSEnvironment:
        return env

    registry = AgentEditFilesystemRegistry(workspace)

    async def registry_for(session_id: str) -> AgentEditFilesystemRegistry:
        return registry

    manager = register_upload_routes(
        app,
        resolve_environment=resolve,
        resolve_registry=registry_for,
        state_path=tmp_path / "state" / "index.json",
        limits=UploadLimits(
            max_file_bytes=300_000,
            chunk_max_bytes=262144,
            max_open_uploads=2,
            max_open_bytes=300_000,
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as c:
        yield c
    await manager.stop()
    env.close()


async def create(client: httpx.AsyncClient, **fields: object) -> httpx.Response:
    return await client.post(
        BASE,
        json={
            "dir": "",
            "name": "payload.bin",
            "size": 3,
            "on_conflict": "fail",
            "created_by": "alice",
            **fields,
        },
    )


@pytest.mark.asyncio
async def test_raw_routes_and_ownership(client: httpx.AsyncClient, tmp_path: Path) -> None:
    payload = b"\x00\xff\xfe"
    resp = await create(client, sha256=hashlib.sha256(payload).hexdigest())
    assert resp.status_code == 201
    url = BASE + "/" + resp.json()["upload_id"]
    for method, suffix in [("HEAD", ""), ("PATCH", ""), ("POST", "/complete"), ("DELETE", "")]:
        wrong = await client.request(
            method,
            url + suffix,
            params={"created_by": "bob"},
            headers=HEADERS,
            content=payload if method == "PATCH" else None,
        )
        assert wrong.status_code == 403
    response = await client.patch(
        url, params={"created_by": "alice"}, headers=HEADERS, content=payload
    )
    assert response.status_code == 204
    assert response.headers["Upload-Offset"] == "3"
    response = await client.head(url, params={"created_by": "alice"})
    assert response.status_code == 200
    assert response.headers["Upload-Offset"] == "3"
    assert response.headers["Upload-Length"] == "3"
    assert response.headers["Cache-Control"] == "no-store"
    response = await client.post(url + "/complete", params={"created_by": "alice"})
    assert response.status_code == 200
    assert response.json()["outcome"] == "created"
    assert (tmp_path / "workspace" / "payload.bin").read_bytes() == payload
    assert (
        await client.post(url + "/complete", params={"created_by": "alice"})
    ).json() == response.json()
    assert (await client.delete(url, params={"created_by": "alice"})).status_code == 200
    assert (tmp_path / "workspace" / "payload.bin").exists()


@pytest.mark.asyncio
async def test_offset_overflow_and_type(client: httpx.AsyncClient, tmp_path: Path) -> None:
    response = await create(client)
    url = BASE + "/" + response.json()["upload_id"] + "?created_by=alice"
    assert (await client.patch(url, headers=HEADERS, content=b"ab")).status_code == 204
    wrong = await client.patch(url, headers=HEADERS, content=b"x")
    assert wrong.status_code == 409
    assert wrong.headers["Upload-Offset"] == "2"
    assert wrong.json()["error"]["code"] == "offset_mismatch"
    assert (
        await client.patch(url, headers={**HEADERS, "Upload-Offset": "2"}, content=b"cd")
    ).status_code == 413
    assert (
        await client.patch(url, headers={**HEADERS, "Content-Type": "text/plain"}, content=b"x")
    ).status_code == 415
    assert (
        await client.patch(url, headers={**HEADERS, "Upload-Offset": "-1"}, content=b"x")
    ).status_code == 400
    assert (
        await client.patch(url, headers={**HEADERS, "Upload-Offset": "2"}, content=b"a" * 262145)
    ).status_code == 413
    assert next((tmp_path / "workspace").iterdir()).read_bytes() == b"ab"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields, status",
    [
        ({"name": "../bad"}, 400),
        ({"name": ".."}, 400),
        ({"name": "a\\b"}, 400),
        ({"name": "\x00"}, 400),
        ({"dir": "../"}, 400),
        ({"dir": "/tmp"}, 400),
        ({"size": -1}, 400),
        ({"size": 300001}, 413),
        ({"sha256": "bad"}, 400),
        ({"created_by": ""}, 400),
    ],
)
async def test_invalid_declarations(
    client: httpx.AsyncClient, fields: dict[str, object], status: int
) -> None:
    assert (await create(client, **fields)).status_code == status


@pytest.mark.asyncio
async def test_open_limits_and_abort(client: httpx.AsyncClient, tmp_path: Path) -> None:
    first = await create(client, size=200_000)
    assert first.status_code == 201
    assert (await create(client, name="too-much", size=200_000)).status_code == 429
    second = await create(client, name="second", size=0)
    assert second.status_code == 201
    assert (await create(client, name="third", size=0)).status_code == 429
    url = BASE + "/" + first.json()["upload_id"] + "?created_by=alice"
    assert (await client.delete(url)).status_code == 204
    assert (await client.head(url)).status_code == 410
    assert (await create(client, name="third", size=0)).status_code == 201
    assert not (
        tmp_path / "workspace" / f"payload.bin.omnigent-upload-{first.json()['upload_id']}.part"
    ).exists()


@pytest.mark.asyncio
async def test_free_disk_and_limits(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    monkeypatch.setattr(
        "omnigent.runner.workspace_upload.shutil.disk_usage",
        lambda _: shutil._ntuple_diskusage(100, 100, 0),
    )
    assert (await create(client)).status_code == 507
    response = await client.get(BASE)
    assert response.status_code == 200
    assert response.json()["supported"] is True
    assert response.json()["limits"]["chunk"]["max"] == 262144


@pytest.mark.asyncio
async def test_sweep_only_indexed_and_restart(client: httpx.AsyncClient, tmp_path: Path) -> None:
    response = await create(client)
    uid = response.json()["upload_id"]
    unrelated = tmp_path / "workspace" / ("other.omnigent-upload-" + "f" * 32 + ".part")
    unrelated.write_bytes(b"keep")
    index_path = tmp_path / "state" / "index.json"
    index = json.loads(index_path.read_text())
    assert index[uid]["created_by"] == "alice"
    app = client._transport.app
    manager = app.state.workspace_uploads
    await manager.sweep(now=index[uid]["created_at"] + 86401)
    assert not (tmp_path / "workspace" / f"payload.bin.omnigent-upload-{uid}.part").exists()
    assert unrelated.read_bytes() == b"keep"
    assert (await client.head(BASE + "/" + uid, params={"created_by": "alice"})).status_code == 410
    assert uid not in json.loads(index_path.read_text())


@pytest.mark.asyncio
async def test_commit_tracking_and_conflict(client: httpx.AsyncClient, tmp_path: Path) -> None:
    target = tmp_path / "workspace" / "payload.bin"
    target.write_text("before\n")
    assert (await create(client)).status_code == 409
    response = await create(client, on_conflict="replace", size=6)
    url = BASE + "/" + response.json()["upload_id"] + "?created_by=alice"
    assert (await client.post(url.replace("?", "/complete?"))).status_code == 409
    assert (await client.patch(url, headers=HEADERS, content=b"after\n")).status_code == 204
    assert (await client.post(url.replace("?", "/complete?"))).status_code == 200
    manager = client._transport.app.state.workspace_uploads
    registry = await manager.resolve_registry("session")
    changes = registry.list_changed_files("session", limit=100)
    assert [c["path"] for c in changes] == ["payload.bin"]
    diff = registry.get_baseline("payload.bin")
    assert diff == "before\n"
    assert target.read_text() == "after\n"


@pytest.mark.asyncio
async def test_lifespan_sweeps_restored_uploads(client: httpx.AsyncClient, tmp_path: Path) -> None:
    from starlette.types import Message

    from omnigent.runner.workspace_upload import UploadManager

    response = await create(client)
    uid = response.json()["upload_id"]
    old = client._transport.app.state.workspace_uploads
    await old.stop()
    app = FastAPI()
    manager = register_upload_routes(
        app,
        resolve_environment=old.resolve_environment,
        resolve_registry=old.resolve_registry,
        state_path=old.state_path,
    )
    assert isinstance(manager, UploadManager)
    # The native runner entrypoint replaces the app's default lifespan.
    import contextlib

    @contextlib.asynccontextmanager
    async def parent_lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield

    app.router.lifespan_context = parent_lifespan
    incoming: asyncio.Queue[Message] = asyncio.Queue()
    outgoing: asyncio.Queue[Message] = asyncio.Queue()
    lifespan = asyncio.create_task(
        app(
            {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}},
            incoming.get,
            outgoing.put,
        )
    )
    await incoming.put({"type": "lifespan.startup"})
    assert (await asyncio.wait_for(outgoing.get(), 10))["type"] == "lifespan.startup.complete"
    assert manager.task is not None
    assert uid not in manager.records
    assert not (tmp_path / "workspace" / f"payload.bin.omnigent-upload-{uid}.part").exists()
    await incoming.put({"type": "lifespan.shutdown"})
    assert (await asyncio.wait_for(outgoing.get(), 10))["type"] == "lifespan.shutdown.complete"
    await lifespan
    assert manager.task is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sandbox_type",
    [
        pytest.param(
            "linux_bwrap",
            marks=pytest.mark.skipif(
                not (sys.platform.startswith("linux") and shutil.which("bwrap")),
                reason="linux_bwrap requires Linux + bwrap on PATH",
            ),
        ),
        pytest.param(
            "darwin_seatbelt",
            marks=pytest.mark.skipif(
                not (sys.platform == "darwin" and shutil.which("sandbox-exec")),
                reason="darwin_seatbelt requires macOS + sandbox-exec on PATH",
            ),
        ),
    ],
)
async def test_startup_cleanup_under_real_sandbox(tmp_path: Path, sandbox_type: str) -> None:
    """A fresh sandboxed helper removes only a restored index's staging file."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = create_os_environment(
        OSEnvSpec(
            cwd=str(workspace), sandbox=OSEnvSandboxSpec(type=sandbox_type, write_paths=["."])
        ),
        additional_read_roots=[Path(__file__).resolve().parents[2]],
    )
    assert isinstance(env, CallerProcessOSEnvironment)

    async def resolve(session_id: str, environment_id: str) -> CallerProcessOSEnvironment:
        return env

    async def registry_for(session_id: str) -> None:
        return None

    def register(app: FastAPI):
        return register_upload_routes(
            app,
            resolve_environment=resolve,
            resolve_registry=registry_for,
            state_path=tmp_path / "state" / "index.json",
        )

    app = FastAPI()
    first = register(app)
    second = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner"
        ) as client:
            response = await create(client)
        assert response.status_code == 201, response.text
        uid = response.json()["upload_id"]
        staging = Path(first.records[uid].staging_path)
        assert staging.exists()
        unrelated = workspace / ("keep.omnigent-upload-" + "f" * 32 + ".part")
        unrelated.write_bytes(b"keep")
        await first.stop()
        second = register(FastAPI())
        assert uid in second.restored
        await second.start()
        assert not staging.exists()
        assert uid not in second.records
        assert uid not in json.loads(second.state_path.read_text())
        assert unrelated.read_bytes() == b"keep"
    finally:
        await first.stop()
        if second is not None:
            await second.stop()
        env.close()
