"""Runner upload routes, hard limits and indexed cleanup."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
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


@pytest.mark.asyncio
async def test_receipt_survives_helper_and_runner_restart(client: httpx.AsyncClient) -> None:
    response = await create(client, size=0)
    uid = response.json()["upload_id"]
    url = BASE + "/" + uid + "?created_by=alice"
    committed = await client.post(url.replace("?", "/complete?"))
    assert committed.status_code == 200
    old = client._transport.app.state.workspace_uploads
    helper = await old._helper("session", DEFAULT_ENVIRONMENT_ID)
    assert helper._proc is not None
    helper._proc.kill()
    helper._proc.wait()
    assert (await client.post(url.replace("?", "/complete?"))).json() == committed.json()
    assert (await client.head(url)).headers["Upload-Offset"] == "0"
    assert (await client.delete(url)).json() == committed.json()
    assert (await client.patch(url, headers=HEADERS, content=b"x")).status_code == 409
    await old.stop()
    app = FastAPI()
    fresh = register_upload_routes(
        app,
        resolve_environment=old.resolve_environment,
        resolve_registry=old.resolve_registry,
        state_path=old.state_path,
    )
    try:
        await fresh.start()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner"
        ) as c:
            assert (await c.post(url.replace("?", "/complete?"))).json() == committed.json()
    finally:
        await fresh.stop()


@pytest.mark.asyncio
async def test_cancelled_create_settles_and_accounts(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    manager = client._transport.app.state.workspace_uploads
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    original = helper.call
    entered, release, settled = threading.Event(), threading.Event(), threading.Event()

    def slow(metadata: dict[str, object], raw: bytes = b"") -> dict[str, object]:
        if metadata["op"] == "begin":
            entered.set()
            assert release.wait(5)
        result = original(metadata, raw)
        settled.set()
        return result

    monkeypatch.setattr(helper, "call", slow)
    task = asyncio.create_task(create(client))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0.05)
    settling = not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(settled.wait, 5)
    assert settling, "Cancelled create returned before its helper operation settled"
    assert not list((tmp_path / "workspace").iterdir())
    assert not manager.records
    assert not json.loads(manager.state_path.read_text())


@pytest.mark.asyncio
async def test_creation_intent_reconciled_after_crash(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = client._transport.app.state.workspace_uploads
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    original = helper.call
    intent: dict[str, object] = {}

    def capture(metadata: dict[str, object], raw: bytes = b"") -> dict[str, object]:
        if metadata["op"] == "begin" and manager.state_path.exists():
            intent.update(json.loads(manager.state_path.read_text()))
        return original(metadata, raw)

    monkeypatch.setattr(helper, "call", capture)
    response = await create(client)
    assert response.status_code == 201
    uid = response.json()["upload_id"]
    assert intent[uid]["pending"] is True
    manager.state_path.write_text(json.dumps(intent))
    await manager.stop()
    fresh = register_upload_routes(
        FastAPI(),
        resolve_environment=manager.resolve_environment,
        resolve_registry=manager.resolve_registry,
        state_path=manager.state_path,
    )
    try:
        await fresh.start()
        assert not fresh.records
        assert not list((tmp_path / "workspace").iterdir())
    finally:
        await fresh.stop()


@pytest.mark.asyncio
async def test_replaced_environment_retires_helper(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = client._transport.app.state.workspace_uploads
    first = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    env = await manager.resolve_environment("session", DEFAULT_ENVIRONMENT_ID)
    other = tmp_path / "other"
    other.mkdir()
    replacement = create_os_environment(
        OSEnvSpec(cwd=str(other), sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert isinstance(replacement, CallerProcessOSEnvironment)

    async def resolve(session: str, environment: str) -> CallerProcessOSEnvironment:
        return replacement

    env.close()
    monkeypatch.setattr(manager, "resolve_environment", resolve)
    try:
        second = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
        assert second is not first
        assert first._closed
        assert second.cwd == other.resolve()
        assert len(manager.helpers) == 1
    finally:
        replacement.close()


@pytest.mark.asyncio
async def test_helper_cache_bounded_and_idle(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnigent.runner.workspace_upload as upload

    manager = client._transport.app.state.workspace_uploads
    monkeypatch.setattr(upload, "HELPER_CACHE_MAX", 2, raising=False)
    monkeypatch.setattr(upload, "HELPER_IDLE_S", 0.0, raising=False)
    first = await manager._helper("one", DEFAULT_ENVIRONMENT_ID)
    await manager._helper("two", DEFAULT_ENVIRONMENT_ID)
    await manager._helper("three", DEFAULT_ENVIRONMENT_ID)
    await manager.sweep()
    assert first._closed
    assert len(manager.helpers) <= 2


@pytest.mark.asyncio
async def test_sweep_quarantines_unresolvable_record(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    manager = client._transport.app.state.workspace_uploads
    response = await create(client)
    uid = response.json()["upload_id"]
    original = manager.resolve_environment

    async def broken(session: str, environment: str) -> CallerProcessOSEnvironment:
        raise ValueError("Environment ended")

    monkeypatch.setattr(manager, "resolve_environment", broken)
    await manager.sweep(now=manager.records[uid].created_at + 86401)
    assert uid not in manager.records
    assert uid in manager.quarantined

    async def unavailable(session: str, environment: str) -> CallerProcessOSEnvironment:
        raise HTTPException(404, "Session ended")

    monkeypatch.setattr(manager, "resolve_environment", original)
    response = await create(client, name="other")
    uid = response.json()["upload_id"]
    monkeypatch.setattr(manager, "resolve_environment", unavailable)
    await manager.sweep(now=manager.records[uid].created_at + 86401)
    assert uid not in manager.records


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,status",
    [(FileNotFoundError(2, "missing"), 400), (OSError(5, "I/O"), 500), (OSError(28, "full"), 507)],
)
async def test_disk_error_mapping(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, error: OSError, status: int
) -> None:
    def fail(_: Path):
        raise error

    monkeypatch.setattr("omnigent.runner.workspace_upload.shutil.disk_usage", fail)
    assert (await create(client)).status_code == status


@pytest.mark.asyncio
async def test_missing_target_directory(client: httpx.AsyncClient) -> None:
    response = await create(client, dir="does-not-exist")
    assert response.status_code in (400, 404)
    assert response.json()["error"]["code"] == "invalid_path"


@pytest.mark.asyncio
async def test_independent_upload_not_blocked_by_slow_finalize(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    manager = client._transport.app.state.workspace_uploads
    first = await create(client, size=0)
    second = await create(client, name="second", size=0)
    original = manager._call
    entered, release = threading.Event(), threading.Event()

    async def slow(uid, record, op, *args, **kwargs):
        if op == "complete":
            entered.set()
            await asyncio.to_thread(release.wait, 5)
        return await original(uid, record, op, *args, **kwargs)

    monkeypatch.setattr(manager, "_call", slow)
    task = asyncio.create_task(
        client.post(BASE + "/" + first.json()["upload_id"] + "/complete?created_by=alice")
    )
    assert await asyncio.to_thread(entered.wait, 5)
    try:
        response = await asyncio.wait_for(
            client.head(BASE + "/" + second.json()["upload_id"] + "?created_by=alice"), 0.5
        )
        assert response.status_code == 200
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_append_offset_persistence_is_lazy(client: httpx.AsyncClient) -> None:
    response = await create(client)
    manager = client._transport.app.state.workspace_uploads
    before = manager.state_path.read_bytes()
    url = BASE + "/" + response.json()["upload_id"] + "?created_by=alice"
    assert (await client.patch(url, headers=HEADERS, content=b"ab")).status_code == 204
    assert (await client.head(url)).headers["Upload-Offset"] == "2"
    assert manager.state_path.read_bytes() == before


@pytest.mark.asyncio
async def test_snapshot_read_failure_still_publishes(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    from omnigent.entities.environment_filesystem import InvalidPath

    (tmp_path / "workspace" / "payload.bin").write_bytes(b"old")
    response = await create(client, on_conflict="replace")
    url = BASE + "/" + response.json()["upload_id"] + "?created_by=alice"
    assert (await client.patch(url, headers=HEADERS, content=b"new")).status_code == 204
    monkeypatch.setattr(
        "omnigent.runner.workspace_upload.CallerProcessFilesystem.read",
        AsyncMock(side_effect=InvalidPath("Unavailable read")),
    )
    assert (await client.post(url.replace("?", "/complete?"))).status_code == 200
    assert (tmp_path / "workspace" / "payload.bin").read_bytes() == b"new"
    manager = client._transport.app.state.workspace_uploads
    registry = await manager.resolve_registry("session")
    assert registry.list_changed_files("session", limit=100)[0]["path"] == "payload.bin"


@pytest.mark.asyncio
async def test_gone_tombstones_bounded(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnigent.runner.workspace_upload as upload

    monkeypatch.setattr(upload, "GONE_MAX", 2, raising=False)
    for i in range(4):
        response = await create(client, name=f"cancel-{i}")
        assert (
            await client.delete(BASE + "/" + response.json()["upload_id"] + "?created_by=alice")
        ).status_code == 204
    assert len(client._transport.app.state.workspace_uploads.gone) <= 2


@pytest.mark.asyncio
async def test_lost_finalize_response_recovers_published_inode(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = client._transport.app.state.workspace_uploads
    response = await create(client, size=0)
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    original = helper.call
    committed: dict[str, object] = {}

    def lose(metadata: dict[str, object], raw: bytes = b"") -> dict[str, object]:
        result = original(metadata, raw)
        if metadata["op"] == "complete":
            committed.update(result)
            helper.close()
            return {"code": "gone", "error": "Lost response after publish"}
        return result

    monkeypatch.setattr(helper, "call", lose)
    url = BASE + "/" + response.json()["upload_id"] + "/complete?created_by=alice"
    result = await client.post(url)
    assert result.status_code == 200
    assert result.json() == committed
    assert (await client.post(url)).json() == committed


@pytest.mark.asyncio
async def test_startup_recovers_publish_before_receipt(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = client._transport.app.state.workspace_uploads
    workspace = tmp_path / "workspace"
    (workspace / "payload.bin").write_text("before\n")
    mask = os.umask(0)
    os.umask(mask)
    response = await create(client, size=0, on_conflict="replace")
    uid = response.json()["upload_id"]
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    original = helper.call
    before_publish: dict[str, object] = {}

    def capture(metadata: dict[str, object], raw: bytes = b"") -> dict[str, object]:
        if metadata["op"] == "complete":
            before_publish.update(json.loads(manager.state_path.read_text()))
        return original(metadata, raw)

    monkeypatch.setattr(helper, "call", capture)
    result = await client.post(BASE + "/" + uid + "/complete?created_by=alice")
    assert result.status_code == 200
    assert before_publish[uid]["finalizing"] is True
    manager.state_path.write_text(json.dumps(before_publish))
    staging = Path(before_publish[uid]["staging_path"])
    staging.hardlink_to(workspace / "payload.bin")
    (workspace / "payload.bin").chmod(0o600)
    await manager.stop()
    registry = AgentEditFilesystemRegistry(workspace)

    async def registry_for(session: str) -> AgentEditFilesystemRegistry:
        return registry

    fresh = register_upload_routes(
        FastAPI(),
        resolve_environment=manager.resolve_environment,
        resolve_registry=registry_for,
        state_path=manager.state_path,
    )
    try:
        await fresh.start()
        assert fresh.records[uid].result == result.json()
        assert registry.list_changed_files("session", limit=100)[0]["path"] == "payload.bin"
        assert registry.get_baseline("payload.bin") == "before\n"
        assert not staging.exists()
        assert (workspace / "payload.bin").stat().st_mode & 0o777 == 0o666 & ~mask
    finally:
        await fresh.stop()


@pytest.mark.asyncio
async def test_helper_cache_capacity(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnigent.runner.workspace_upload as upload

    monkeypatch.setattr(upload, "HELPER_CACHE_MAX", 2, raising=False)
    manager = client._transport.app.state.workspace_uploads
    first = await manager._helper("one", DEFAULT_ENVIRONMENT_ID)
    await manager._helper("two", DEFAULT_ENVIRONMENT_ID)
    await manager._helper("three", DEFAULT_ENVIRONMENT_ID)
    assert first._closed
    assert len(manager.helpers) == 2


@pytest.mark.asyncio
async def test_gone_tombstone_ttl(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    import omnigent.runner.workspace_upload as upload

    response = await create(client)
    uid = response.json()["upload_id"]
    await client.delete(BASE + "/" + uid + "?created_by=alice")
    manager = client._transport.app.state.workspace_uploads
    assert uid in manager.gone
    monkeypatch.setattr(upload, "time", SimpleNamespace(monotonic=lambda: float("inf")))
    await manager.sweep()
    assert not manager.gone


@pytest.mark.asyncio
async def test_production_manual_lifespan_sweeps_restored_uploads(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tunnel runner enters its factory's lifespan directly, without ASGI."""
    from unittest.mock import AsyncMock

    from omnigent.runner import _entry
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    response = await create(client)
    assert response.status_code == 201
    uid = response.json()["upload_id"]
    old = client._transport.app.state.workspace_uploads
    staging = Path(old.records[uid].staging_path)
    await old.stop()
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "runtime-data"))
    monkeypatch.setenv("OMNIGENT_RUNNER_HOST_OWNS_GLOBAL_CLEANUP", "1")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://runner.test")
    monkeypatch.setattr(_entry, "_runner_workspace_from_env", lambda: tmp_path / "workspace")
    monkeypatch.setattr("omnigent.runner.identity.get_stable_runner_id", lambda: "upload-test")
    monkeypatch.setattr(
        "omnigent.runner.workspace_upload.upload_state_path", lambda _: old.state_path
    )
    monkeypatch.setattr(HarnessProcessManager, "start", AsyncMock())
    monkeypatch.setattr(HarnessProcessManager, "shutdown", AsyncMock())
    monkeypatch.setattr("omnigent.runner.subagent_work.run_subagent_launch_reaper", AsyncMock())
    monkeypatch.setattr(
        "omnigent.runner.native.teardown_all_codex_native_app_servers", AsyncMock()
    )
    monkeypatch.setattr("omnigent.runner.native.teardown_all_opencode_native_servers", AsyncMock())
    app = _entry.create_app(auth_token_factory=None, auth_resolved=True)
    app.state.native_pane_reaper = None
    manager = app.state.workspace_uploads
    manager.resolve_environment = old.resolve_environment
    manager.resolve_registry = old.resolve_registry
    assert uid in manager.restored
    lifespan = app.router.lifespan_context(app)
    helper = None
    try:
        await lifespan.__aenter__()
        try:
            started = manager.task is not None
            removed = not staging.exists() and uid not in manager.records
            helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
            assert (await asyncio.to_thread(helper.call, {"op": "probe"}))["raw_upload"] is True
        finally:
            await lifespan.__aexit__(None, None, None)
        stopped = manager.task is None and helper._closed
        print(
            {
                "production_startup_sweep": removed,
                "hourly_task_started": started,
                "production_shutdown_closed_helper": stopped,
            }
        )
        assert started, "Manual production lifespan did not start the upload sweep task"
        assert removed, "Manual production startup left the restored indexed staging file"
        assert stopped, "Manual production shutdown did not close the upload helper"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_unknown_uploads_do_not_retain_locks(client: httpx.AsyncClient) -> None:
    manager = client._transport.app.state.workspace_uploads
    for i in range(10):
        assert (await client.head(BASE + f"/missing-{i}?created_by=alice")).status_code == 404
    assert not manager.upload_locks
