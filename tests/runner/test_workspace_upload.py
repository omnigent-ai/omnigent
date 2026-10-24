"""Runner upload routes, hard limits and indexed cleanup."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import io
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
from omnigent.runner.workspace_upload import UploadLimits, UploadManager, register_upload_routes
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


def restart(manager: UploadManager) -> UploadManager:
    return register_upload_routes(
        FastAPI(),
        resolve_environment=manager.resolve_environment,
        resolve_registry=manager.resolve_registry,
        state_path=manager.state_path,
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
        async with asyncio.timeout(5):
            while uid in second.records or uid in json.loads(second.state_path.read_text()):
                await asyncio.sleep(0.01)
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

    def slow(metadata: dict[str, object], raw: bytes = b"", **kwargs: float) -> dict[str, object]:
        if metadata["op"] == "begin":
            entered.set()
            assert release.wait(5)
        result = original(metadata, raw, **kwargs)
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
@pytest.mark.parametrize("record_identity", [False, True])
async def test_creation_intent_reconciled_after_crash(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_identity: bool,
) -> None:
    manager = client._transport.app.state.workspace_uploads
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    original = helper.call
    intent: dict[str, object] = {}

    def capture(
        metadata: dict[str, object], raw: bytes = b"", **kwargs: float
    ) -> dict[str, object]:
        if metadata["op"] == "begin" and manager.state_path.exists():
            intent.update(json.loads(manager.state_path.read_text()))
        return original(metadata, raw, **kwargs)

    monkeypatch.setattr(helper, "call", capture)
    response = await create(client)
    assert response.status_code == 201
    uid = response.json()["upload_id"]
    assert intent[uid]["pending"] is True
    if record_identity:
        intent[uid]["staging_dev"] = manager.records[uid].staging_dev
        intent[uid]["staging_ino"] = manager.records[uid].staging_ino
    manager.state_path.write_text(json.dumps(intent))
    await manager.stop()
    fresh = restart(manager)
    try:
        await fresh.start()
        await fresh.sweep()
        if record_identity:
            assert not fresh.records
            assert not list((tmp_path / "workspace").iterdir())
        else:
            staging = Path(intent[uid]["staging_path"])
            assert staging.exists()
            assert uid in fresh.records
            assert fresh.records[uid].staging_ino == 0
            await fresh.sweep(now=fresh.records[uid].created_at + fresh.limits.max_age_s + 1)
            assert uid not in fresh.records
            assert staging.exists()
            assert uid not in json.loads(fresh.state_path.read_text())
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
async def test_sweep_drops_unresolvable_record(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from omnigent.errors import OmnigentError

    manager = client._transport.app.state.workspace_uploads
    response = await create(client)
    uid = response.json()["upload_id"]
    record = manager.records[uid]
    staging = Path(record.staging_path)
    original = manager.resolve_environment

    async def unavailable(session: str, environment: str) -> CallerProcessOSEnvironment:
        raise OmnigentError("Session has no agent_id")

    monkeypatch.setattr(manager, "resolve_environment", unavailable)
    await manager.sweep(now=record.created_at + manager.limits.max_age_s + 1)
    await manager.sweep(now=record.created_at + manager.limits.max_age_s + 2)
    assert uid not in manager.records
    assert uid not in manager.upload_locks
    assert uid not in json.loads(manager.state_path.read_text())
    assert not manager._pinned(("session", DEFAULT_ENVIRONMENT_ID))
    assert staging.read_bytes() == b""
    assert caplog.text.count(uid) == 1
    assert len(caplog.records) == 1 and caplog.records[0].exc_info is not None
    assert "Session has no agent_id" in caplog.text
    assert not any(log.levelname == "ERROR" for log in caplog.records)
    monkeypatch.setattr(manager, "resolve_environment", original)
    manager.limits = UploadLimits(max_open_uploads=1)
    assert (await create(client, name="new")).status_code == 201


@pytest.mark.asyncio
async def test_sweep_retains_unexpired_record_on_resolver_failure(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager = client._transport.app.state.workspace_uploads
    uid = (await create(client)).json()["upload_id"]
    record = manager.records[uid]
    staging = Path(record.staging_path)
    await manager.stop()
    restored = restart(manager)
    original = restored.resolve_environment

    entered, release = asyncio.Event(), asyncio.Event()
    sweep = None

    async def unavailable(session: str, environment: str) -> CallerProcessOSEnvironment:
        if asyncio.current_task() is sweep:
            entered.set()
            await release.wait()
        raise ValueError("Temporarily unable to resolve the environment")

    monkeypatch.setattr(restored, "resolve_environment", unavailable)
    try:
        sweep = asyncio.create_task(restored.sweep(now=record.created_at + 1))
        await entered.wait()
        with pytest.raises(ValueError, match="Temporarily unable"):
            await restored._helper("session", DEFAULT_ENVIRONMENT_ID)
        release.set()
        await sweep
        await restored.sweep(now=record.created_at + 2)
        with pytest.raises(ValueError, match="Temporarily unable"):
            await restored._helper("session", DEFAULT_ENVIRONMENT_ID)
        assert uid in restored.records
        assert uid in json.loads(restored.state_path.read_text())
        assert restored._pinned(("session", DEFAULT_ENVIRONMENT_ID))
        assert staging.read_bytes() == b""
        assert not caplog.records
        monkeypatch.setattr(restored, "resolve_environment", original)
        assert await restored.resolve_environment("session", DEFAULT_ENVIRONMENT_ID)
    finally:
        release.set()
        if sweep is not None:
            await sweep
        await restored.stop()


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
async def test_shared_helper_bounds_wait_during_slow_finalize(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    import omnigent.inner.os_env_upload as upload

    first = await create(client, size=0)
    second = await create(client, name="second", size=0)
    original = upload.read_frame
    entered, release = threading.Event(), threading.Event()

    def slow(stream):
        entered.set()
        assert release.wait(5)
        return original(stream)

    monkeypatch.setattr(upload, "read_frame", slow)
    monkeypatch.setattr(upload, "CALL_TIMEOUT_S", 0.05)
    task = asyncio.create_task(
        client.post(BASE + "/" + first.json()["upload_id"] + "/complete?created_by=alice")
    )
    assert await asyncio.to_thread(entered.wait, 5)
    try:
        response = await asyncio.wait_for(
            client.head(BASE + "/" + second.json()["upload_id"] + "?created_by=alice"), 0.5
        )
        assert response.status_code == 429
        assert 1 <= int(response.headers["Retry-After"]) <= 5
    finally:
        release.set()
        completed = await task
    assert completed.status_code == 200
    response = await client.head(BASE + "/" + second.json()["upload_id"] + "?created_by=alice")
    assert response.status_code == 200
    assert response.headers["Upload-Offset"] == "0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_number,status",
    [
        (errno.ENOSPC, 507),
        (errno.EDQUOT, 507),
        (errno.EIO, 500),
        (errno.EACCES, 500),
        (errno.ENOENT, 500),
    ],
)
async def test_initial_save_failure_releases_admission(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
    status: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = client._transport.app.state.workspace_uploads
    manager.limits = UploadLimits(max_open_uploads=1, max_open_bytes=3)

    def fail() -> None:
        raise OSError(error_number, "initial save failed", "/secret/path")

    with monkeypatch.context() as fault:
        fault.setattr(manager, "_save", fail)
        response = await create(client)
    assert response.status_code == status
    assert response.json()["error"]["code"] == ("no_space" if status == 507 else "io_error")
    assert response.json()["error"]["message"] == "Upload index could not be saved"
    assert "/secret/path" not in response.text
    assert "/secret/path" in caplog.text
    assert not manager.records
    assert not manager.upload_locks
    assert not list((tmp_path / "workspace").iterdir())
    assert (await create(client)).status_code == 201


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", ["immediate", "sweep", "save"])
async def test_post_replace_save_failure_durably_releases_admission(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry: str
) -> None:
    import stat

    from omnigent.runner.workspace_upload import UploadDeclaration

    manager = client._transport.app.state.workspace_uploads
    manager.limits = UploadLimits(max_open_uploads=1, max_open_bytes=3)
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    original_call, original_save, original_fsync = helper.call, manager._save, os.fsync
    operations = []
    replaced = {}
    saves = 0

    def call(metadata, raw=b"", **kwargs):
        operations.append(metadata["op"])
        return original_call(metadata, raw, **kwargs)

    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) and not replaced:
            replaced.update(json.loads(manager.state_path.read_text()))
            raise OSError(errno.ENOSPC, "directory sync failed", str(manager.state_path.parent))
        return original_fsync(fd)

    def save():
        nonlocal saves
        assert manager.lock.locked()
        saves += 1
        if saves > 1 and retry != "immediate":
            raise OSError(errno.EIO, "corrective save failed")
        return original_save()

    with monkeypatch.context() as fault:
        fault.setattr(helper, "call", call)
        fault.setattr(manager, "_save", save)
        fault.setattr(os, "fsync", sync)
        response = await create(client)
        assert response.status_code == 507
        assert "begin" not in operations
        assert not manager.records
        assert not manager.upload_locks
        assert len(replaced) == 1
        assert next(iter(replaced.values()))["staging_dev"] == 0
        assert not list((tmp_path / "workspace").iterdir())
        if retry == "sweep":
            await manager.sweep()
        if retry != "immediate":
            assert manager.index_dirty
            assert json.loads(manager.state_path.read_text()) == replaced
    if retry == "sweep":
        await manager.sweep()
    elif retry == "save":
        await manager._persist()
    fresh = restart(manager)
    fresh.limits = manager.limits
    try:
        assert not fresh.records
        assert json.loads(manager.state_path.read_text()) == {}
        assert not manager.index_dirty
        result = await fresh.create(
            "session",
            DEFAULT_ENVIRONMENT_ID,
            UploadDeclaration(dir="", name="retry", size=3, created_by="alice"),
        )
        assert result["upload_id"] in fresh.records
    finally:
        await fresh.stop()


@pytest.mark.asyncio
async def test_begin_queue_timeout_releases_admission(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnigent.inner.os_env_upload as upload

    manager = client._transport.app.state.workspace_uploads
    manager.limits = UploadLimits(max_open_uploads=1, max_open_bytes=3)
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    saved, dispatch = asyncio.Event(), asyncio.Event()
    original_save = manager._save_settled
    original_timeout = upload.operation_timeout

    async def pause_after_save() -> None:
        await original_save()
        saved.set()
        await dispatch.wait()

    def timeout(metadata: dict[str, object]) -> float:
        return 0.05 if metadata.get("op") == "begin" else original_timeout(metadata)

    monkeypatch.setattr(manager, "_save_settled", pause_after_save)
    monkeypatch.setattr(upload, "operation_timeout", timeout)
    task = asyncio.create_task(create(client))
    await asyncio.wait_for(saved.wait(), 5)
    assert helper._lock.acquire(blocking=False)
    try:
        dispatch.set()
        response = await asyncio.wait_for(task, 1)
    finally:
        helper._lock.release()
    assert response.status_code == 429
    assert 1 <= int(response.headers["Retry-After"]) <= 5
    assert response.json()["error"]["code"] == "busy"
    assert not manager.records
    assert not manager.upload_locks
    assert json.loads(manager.state_path.read_text()) == {}
    assert not list((tmp_path / "workspace").iterdir())
    assert (await create(client)).status_code == 201


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_head_uses_metadata_captured_before_expiry(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    manager = client._transport.app.state.workspace_uploads
    response = await create(client)
    uid = response.json()["upload_id"]
    url = BASE + "/" + uid + "?created_by=alice"
    assert (await client.patch(url, headers=HEADERS, content=b"abc")).status_code == 204
    if committed:
        assert (await client.post(url.replace("?", "/complete?"))).status_code == 200
    original = manager.operate

    async def expire_after_operation(session, environment, uid, owner, op, *args):
        result = await original(session, environment, uid, owner, op, *args)
        if op == "status":
            await manager.sweep(now=manager.records[uid].created_at + manager.limits.max_age_s + 1)
            assert uid not in manager.records
        return result

    monkeypatch.setattr(manager, "operate", expire_after_operation)
    response = await client.head(url)
    assert response.status_code == 200
    assert response.headers["Upload-Offset"] == "3"
    assert response.headers["Upload-Length"] == "3"
    assert response.headers["Cache-Control"] == "no-store"
    assert (await client.head(url)).status_code == 410


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

    def lose(metadata: dict[str, object], raw: bytes = b"", **kwargs: float) -> dict[str, object]:
        result = original(metadata, raw, **kwargs)
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
@pytest.mark.parametrize("failure", ["unlink", "fchmod"])
async def test_same_helper_recovery_releases_handles_and_expires(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import omnigent.inner.os_env as os_env

    descriptor_state = tmp_path / "descriptor-state.json"
    script = f"""
import errno
import json
import os
from pathlib import Path
from omnigent.inner.os_env import main
from omnigent.inner import os_env_upload as upload

original = getattr(os, {failure!r})
failed = False
def fail_once(*args, **kwargs):
    global failed
    if not failed:
        failed = True
        raise OSError(errno.EIO, 'Injected post-publication failure')
    return original(*args, **kwargs)
setattr(os, {failure!r}, fail_once)

recover = upload.UploadEngine._recover
def observe_recovery(self, uid, metadata):
    live = self.uploads[uid]
    descriptors = (live.fd, live.parent_fd)
    result = recover(self, uid, metadata)
    closed = []
    for fd in descriptors:
        try:
            os.fstat(fd)
        except OSError as exc:
            closed.append(exc.errno == errno.EBADF)
        else:
            closed.append(False)
    Path({str(descriptor_state)!r}).write_text(json.dumps(closed))
    return result
upload.UploadEngine._recover = observe_recovery
raise SystemExit(main())
"""
    original_popen = os_env.subprocess.Popen

    def spawn(argv, **kwargs):
        assert argv[1:4] == ["-P", "-m", "omnigent.inner.os_env"]
        return original_popen([*argv[:2], "-c", script, *argv[4:]], **kwargs)

    monkeypatch.setattr(os_env.subprocess, "Popen", spawn)
    manager = client._transport.app.state.workspace_uploads
    created = await create(client)
    uid = created.json()["upload_id"]
    url = BASE + "/" + uid + "?created_by=alice"
    assert (await client.patch(url, headers=HEADERS, content=b"\x00\xffx")).status_code == 204
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    process = helper._proc
    assert process is not None
    first = await client.post(url.replace("?", "/complete?"))
    assert first.status_code == 500
    assert first.json()["error"]["code"] == "io_error"
    published = tmp_path / "workspace" / "payload.bin"
    assert published.read_bytes() == b"\x00\xffx"
    recovered = await client.post(url.replace("?", "/complete?"))
    assert recovered.status_code == 200
    assert recovered.json()["outcome"] == "created"
    assert json.loads(descriptor_state.read_text()) == [True, True]
    assert helper._proc is process and process.poll() is None
    assert (await client.post(url.replace("?", "/complete?"))).json() == recovered.json()
    await manager.sweep(now=manager.records[uid].created_at + manager.limits.max_age_s + 1)
    assert uid not in manager.records
    assert uid not in manager.upload_locks
    assert uid not in json.loads(manager.state_path.read_text())
    assert (await client.head(url)).status_code == 410
    assert published.read_bytes() == b"\x00\xffx"
    assert helper._proc is process and process.poll() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields, code",
    [
        ({"created_by": "x" * 70000}, "invalid_request"),
        ({"created_by": "é" * 129}, "invalid_request"),
        ({"dir": "x" * 4097}, "invalid_request"),
        ({"dir": "é" * 2049}, "invalid_request"),
        ({"name": "x" * 256}, "invalid_name"),
        ({"name": "é" * 128}, "invalid_name"),
    ],
    ids=["owner-frame", "owner-utf8", "dir-ascii", "dir-utf8", "name-ascii", "name-utf8"],
)
async def test_oversized_declaration_preserves_shared_helper(
    client: httpx.AsyncClient, fields: dict[str, str], code: str
) -> None:
    manager = client._transport.app.state.workspace_uploads
    existing = await create(client)
    uid = existing.json()["upload_id"]
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    process = helper._proc
    assert process is not None
    rejected = await create(client, **fields)
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == code
    assert helper._proc is process and process.poll() is None
    assert list(manager.records) == [uid]
    assert (
        await client.patch(BASE + "/" + uid + "?created_by=alice", headers=HEADERS, content=b"abc")
    ).status_code == 204
    boundary = await create(client, name="é" * 127 + "x", created_by="é" * 128, size=0)
    assert boundary.status_code == 201


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

    def capture(
        metadata: dict[str, object], raw: bytes = b"", **kwargs: float
    ) -> dict[str, object]:
        if metadata["op"] == "complete":
            before_publish.update(json.loads(manager.state_path.read_text()))
        return original(metadata, raw, **kwargs)

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
    # Block start() on the startup sweep's ready event rather than its wall-clock budget.
    monkeypatch.setattr("omnigent.runner.workspace_upload.STARTUP_SWEEP_BUDGET_S", None)
    try:
        await fresh.start()
        assert fresh.records[uid].result == result.json()
        assert fresh.records[uid].snapshot is None
        assert "before\\n" not in fresh.state_path.read_text()
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
    async with manager._using_helper("three", DEFAULT_ENVIRONMENT_ID) as active:
        await asyncio.to_thread(active.close)
        with pytest.raises(upload.RouteError) as exc:
            await manager._helper("three", DEFAULT_ENVIRONMENT_ID)
        assert exc.value.status == 429
        assert exc.value.code == "busy"
        assert 1 <= int(exc.value.response().headers["Retry-After"]) <= 5
    await manager._helper("three", DEFAULT_ENVIRONMENT_ID)


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
    # Block start() on the startup sweep's ready event rather than its wall-clock budget.
    monkeypatch.setattr("omnigent.runner.workspace_upload.STARTUP_SWEEP_BUDGET_S", None)
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


@pytest.mark.asyncio
async def test_late_replace_target_lost_response_preserves_outcome(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = client._transport.app.state.workspace_uploads
    response = await create(client, size=0, on_conflict="replace")
    (tmp_path / "workspace" / "payload.bin").write_text("late baseline\n")
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    original = helper.call
    committed = {}

    def lose(metadata, raw=b"", **kwargs):
        result = original(metadata, raw, **kwargs)
        if metadata["op"] == "complete":
            committed.update(result)
            helper.close()
            return {"code": "gone", "error": "Lost committed response"}
        return result

    monkeypatch.setattr(helper, "call", lose)
    result = await client.post(
        BASE + "/" + response.json()["upload_id"] + "/complete?created_by=alice"
    )
    assert result.status_code == 200
    assert result.json() == committed
    assert result.json()["outcome"] == "replaced"
    registry = await manager.resolve_registry("session")
    assert registry.get_baseline("payload.bin") == "late baseline\n"


@pytest.mark.asyncio
async def test_pending_intent_preserves_unrelated_occupant(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    from omnigent.runner.workspace_upload import RouteError

    manager = client._transport.app.state.workspace_uploads
    response = await create(client)
    uid = response.json()["upload_id"]
    record = manager.records[uid]
    staging = Path(record.staging_path)
    await manager.stop()
    staging.unlink()
    staging.write_bytes(b"unrelated")
    staging.chmod(0o600)
    record.pending = True
    record.staging_dev = record.staging_ino = 0
    manager.state_path.write_text(json.dumps({uid: record.model_dump()}))
    fresh = restart(manager)
    try:
        await fresh.start()
        await fresh.sweep()
        await fresh.sweep(now=record.created_at + fresh.limits.max_age_s + 1)
        assert staging.read_bytes() == b"unrelated"
        assert list(staging.parent.iterdir()) == [staging]
        assert "allocation identity is unknown" in caplog.text
        assert uid not in fresh.records
        assert uid not in json.loads(fresh.state_path.read_text())
        for op in ("status", "append", "complete", "abort"):
            with pytest.raises(RouteError) as exc:
                await fresh.operate("session", DEFAULT_ENVIRONMENT_ID, uid, "alice", op)
            assert exc.value.status == 410
        assert staging.read_bytes() == b"unrelated"
    finally:
        await fresh.stop()


@pytest.mark.asyncio
async def test_open_upload_pins_idle_helper(client: httpx.AsyncClient) -> None:
    import omnigent.runner.workspace_upload as upload

    manager = client._transport.app.state.workspace_uploads
    response = await create(client)
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    manager.helpers[("session", DEFAULT_ENVIRONMENT_ID)].used -= upload.HELPER_IDLE_S + 1
    await manager.sweep()
    assert not helper._closed
    url = BASE + "/" + response.json()["upload_id"] + "?created_by=alice"
    assert (await client.patch(url, headers=HEADERS, content=b"a")).status_code == 204


@pytest.mark.asyncio
async def test_all_pinned_helper_capacity_returns_429(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnigent.runner.workspace_upload as upload

    manager = client._transport.app.state.workspace_uploads
    monkeypatch.setattr(upload, "HELPER_CACHE_MAX", 1)
    response = await create(client)
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    with pytest.raises(upload.RouteError) as exc:
        await manager._helper("other", DEFAULT_ENVIRONMENT_ID)
    assert exc.value.status == 429
    assert exc.value.code == "busy"
    assert 1 <= int(exc.value.response().headers["Retry-After"]) <= 5
    assert not helper._closed
    url = BASE + "/" + response.json()["upload_id"] + "?created_by=alice"
    assert (await client.patch(url, headers=HEADERS, content=b"a")).status_code == 204


@pytest.mark.asyncio
async def test_sweep_rechecks_expiry_after_append_refresh(client: httpx.AsyncClient) -> None:
    import omnigent.runner.workspace_upload as upload

    manager = client._transport.app.state.workspace_uploads
    response = await create(client)
    uid = response.json()["upload_id"]
    record = manager.records[uid]
    record.last_activity = upload._now() - manager.limits.idle_ttl_s - 1
    lock = manager.upload_locks.setdefault(uid, asyncio.Lock())
    async with lock:
        sweep = asyncio.create_task(manager.sweep())
        await asyncio.sleep(0)
        result = await manager._call(uid, record, "append", b"a", offset=0)
        record.offset = result["offset"]
        record.last_activity = upload._now()
    await sweep
    assert uid in manager.records
    assert Path(record.staging_path).read_bytes() == b"a"


@pytest.mark.asyncio
async def test_cancelled_index_save_settles_before_next_save(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    manager = client._transport.app.state.workspace_uploads
    await create(client)
    original = manager._save
    entered, release = threading.Event(), threading.Event()
    calls = []

    def delayed():
        calls.append("start")
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        original()
        calls.append("end")

    monkeypatch.setattr(manager, "_save", delayed)
    first = asyncio.create_task(manager._persist())
    assert await asyncio.to_thread(entered.wait, 5)
    first.cancel()
    second = asyncio.create_task(manager._persist())
    await asyncio.sleep(0.05)
    serialized = calls == ["start"] and manager.lock.locked()
    release.set()
    await asyncio.gather(first, second, return_exceptions=True)
    assert serialized, calls
    assert calls == ["start", "end", "start", "end"]


@pytest.mark.asyncio
async def test_shutdown_closes_helpers_after_inflight_error(client: httpx.AsyncClient) -> None:
    from omnigent.runner.workspace_upload import RouteError

    manager = client._transport.app.state.workspace_uploads
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    release = asyncio.Event()

    async def failing():
        await release.wait()
        raise RouteError(413, "too_large", "Rejected append")

    task = asyncio.create_task(failing())
    manager.inflight.add(task)
    stop = asyncio.create_task(manager.stop())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(task, return_exceptions=True)
    await stop
    assert helper._closed
    assert not manager.helpers


@pytest.mark.asyncio
async def test_committed_index_has_no_snapshot_contents(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    target = tmp_path / "workspace" / "payload.bin"
    target.write_text("private baseline text")
    response = await create(client, size=0, on_conflict="replace")
    uid = response.json()["upload_id"]
    assert (await client.post(BASE + "/" + uid + "/complete?created_by=alice")).status_code == 200
    manager = client._transport.app.state.workspace_uploads
    assert manager.records[uid].snapshot is None
    assert "private baseline text" not in manager.state_path.read_text()
    registry = await manager.resolve_registry("session")
    assert registry.get_baseline("payload.bin") == "private baseline text"


@pytest.mark.asyncio
async def test_startup_sweep_failure_is_isolated(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    manager = client._transport.app.state.workspace_uploads
    monkeypatch.setattr(manager, "sweep", AsyncMock(side_effect=OSError("index unavailable")))
    await manager.start()
    await asyncio.sleep(0)
    assert manager.task is not None and not manager.task.done()
    await manager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("contents", ["{corrupt", '{"id":{"name":"incompatible"}}'])
async def test_corrupt_index_does_not_prevent_startup(
    client: httpx.AsyncClient, contents: str
) -> None:
    manager = client._transport.app.state.workspace_uploads
    manager.state_path.parent.mkdir(parents=True, exist_ok=True)
    manager.state_path.write_text(contents)
    fresh = restart(manager)
    try:
        await fresh.start()
        assert not fresh.records
        assert fresh.task is not None
        assert list(manager.state_path.parent.glob("index.corrupt-*.json"))
    finally:
        await fresh.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("next_op", ["append", "complete"])
async def test_other_upload_resumes_after_helper_timeout(
    client: httpx.AsyncClient, next_op: str
) -> None:
    import signal

    manager = client._transport.app.state.workspace_uploads
    first = await create(client, name="first")
    second = await create(client, name="second", size=3 if next_op == "append" else 0)
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    assert helper._proc is not None
    os.kill(helper._proc.pid, signal.SIGSTOP)
    assert (
        await asyncio.to_thread(
            helper.call, {"op": "status", "id": first.json()["upload_id"]}, timeout_s=0.05
        )
    )["code"] == "timeout"
    url = BASE + "/" + second.json()["upload_id"] + "?created_by=alice"
    if next_op == "append":
        assert (await client.patch(url, headers=HEADERS, content=b"abc")).status_code == 204
    assert (await client.post(url.replace("?", "/complete?"))).status_code == 200


@pytest.mark.asyncio
async def test_slow_startup_sweep_does_not_delay_readiness(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnigent.runner.workspace_upload as upload

    manager = client._transport.app.state.workspace_uploads
    release = asyncio.Event()
    entered = asyncio.Event()

    async def slow():
        entered.set()
        await release.wait()

    monkeypatch.setattr(manager, "sweep", slow)
    monkeypatch.setattr(upload, "STARTUP_SWEEP_BUDGET_S", 0.02, raising=False)
    started = asyncio.create_task(manager.start())
    await entered.wait()
    try:
        await asyncio.wait_for(asyncio.shield(started), 0.1)
        assert manager.task is not None
    finally:
        release.set()
        await started
        await manager.stop()


@pytest.mark.asyncio
async def test_expiry_sweep_waits_for_begin_identity(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = client._transport.app.state.workspace_uploads
    original = manager._call
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(uid, record, op, *args, **kwargs):
        if op == "begin":
            entered.set()
            await release.wait()
        return await original(uid, record, op, *args, **kwargs)

    monkeypatch.setattr(manager, "_call", delayed)
    creating = asyncio.create_task(create(client))
    await entered.wait()
    record = next(iter(manager.records.values()))
    sweep = asyncio.create_task(manager.sweep(now=record.created_at + 86401))
    await asyncio.sleep(0.02)
    waiting = not sweep.done()
    release.set()
    response = await creating
    await sweep
    assert waiting
    assert response.status_code == 201
    assert not manager.records
    assert not Path(record.staging_path).exists()


@pytest.mark.asyncio
async def test_shutdown_waits_for_cancelled_creation_cleanup(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = client._transport.app.state.workspace_uploads
    original = manager._abort_creation
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(uid):
        entered.set()
        await release.wait()
        return await original(uid)

    monkeypatch.setattr(manager, "_abort_creation", delayed)
    original_create = manager._create
    begun, finish = asyncio.Event(), asyncio.Event()

    async def delayed_create(*args):
        result = await original_create(*args)
        begun.set()
        await finish.wait()
        return result

    monkeypatch.setattr(manager, "_create", delayed_create)
    creating = asyncio.create_task(create(client))
    await begun.wait()
    creating.cancel()
    finish.set()
    await entered.wait()
    stopping = asyncio.create_task(manager.stop())
    await asyncio.sleep(0.02)
    waiting = not stopping.done()
    release.set()
    await asyncio.gather(creating, return_exceptions=True)
    await stopping
    assert waiting
    assert not manager.helpers
    assert not manager.records


@pytest.mark.asyncio
async def test_prepare_lease_prevents_cache_eviction(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    import omnigent.runner.workspace_upload as upload

    monkeypatch.setattr(upload, "HELPER_CACHE_MAX", 1)
    manager = client._transport.app.state.workspace_uploads
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    original = helper.call
    entered, release = threading.Event(), threading.Event()

    def delayed(metadata, raw=b"", **kwargs):
        result = original(metadata, raw, **kwargs)
        if metadata["op"] == "prepare":
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(helper, "call", delayed)
    creating = asyncio.create_task(create(client))
    assert await asyncio.to_thread(entered.wait, 5)
    try:
        with pytest.raises(upload.RouteError) as exc:
            await manager._helper("other", DEFAULT_ENVIRONMENT_ID)
        assert exc.value.status == 429
    finally:
        release.set()
        response = await creating
    assert response.status_code == 201
    assert not helper._closed


@pytest.mark.asyncio
async def test_unpublished_finalize_timeout_can_retry(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.runner.workspace_upload import RouteError

    target = tmp_path / "workspace" / "payload.bin"
    target.write_text("before timeout")
    response = await create(client, size=0, on_conflict="replace")
    manager = client._transport.app.state.workspace_uploads
    original = manager._call
    first = True

    async def timeout(uid, record, op, *args, **kwargs):
        nonlocal first
        if op == "complete" and first:
            first = False
            raise RouteError(504, "timeout", "Unpublished operation timed out")
        return await original(uid, record, op, *args, **kwargs)

    monkeypatch.setattr(manager, "_call", timeout)
    url = BASE + "/" + response.json()["upload_id"] + "/complete?created_by=alice"
    assert (await client.post(url)).status_code == 504
    assert target.read_text() == "before timeout"
    assert (await client.post(url)).status_code == 200
    registry = await manager.resolve_registry("session")
    assert registry.get_baseline("payload.bin") == "before timeout"


@pytest.mark.asyncio
async def test_changed_staging_bytes_reject_complete_and_abort_releases_quota(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"a" * 200_000
    response = await create(
        client,
        name="changed.bin",
        size=len(payload),
    )
    uid = response.json()["upload_id"]
    url = BASE + "/" + uid
    manager = client._transport.app.state.workspace_uploads
    record = manager.records[uid]
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    original = helper.call

    def alter_before_complete(metadata, raw=b"", **kwargs):
        if metadata["op"] == "complete":
            Path(record.staging_path).write_bytes(b"b" * len(payload))
        return original(metadata, raw, **kwargs)

    monkeypatch.setattr(helper, "call", alter_before_complete)
    assert (
        await client.patch(url, params={"created_by": "alice"}, headers=HEADERS, content=payload)
    ).status_code == 204

    completed = await client.post(url + "/complete", params={"created_by": "alice"})
    assert completed.status_code == 409
    assert completed.json()["error"]["code"] == "checksum_mismatch"
    assert not (tmp_path / "workspace" / "changed.bin").exists()
    assert Path(record.staging_path).read_bytes() == b"b" * len(payload)

    assert (await client.delete(url, params={"created_by": "alice"})).status_code == 204
    assert not Path(record.staging_path).exists()
    assert uid not in manager.records
    assert not manager._pinned(("session", DEFAULT_ENVIRONMENT_ID))
    assert (await asyncio.to_thread(helper.call, {"op": "status", "id": uid}))["code"] == "gone"
    assert (await create(client, name="quota-after-abort", size=len(payload))).status_code == 201


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "conflict,expected", [("fail", "created"), ("replace", "replaced"), ("rename", "renamed")]
)
async def test_publication_plan_recovers_without_file_metadata(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conflict: str,
    expected: str,
) -> None:
    manager = client._transport.app.state.workspace_uploads
    workspace = tmp_path / "workspace"
    if conflict != "fail":
        (workspace / "payload.bin").write_text("baseline")
    response = await create(client, size=0, on_conflict=conflict)
    uid = response.json()["upload_id"]
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    original = helper.call
    durable = {}
    committed = {}

    def lose(metadata, raw=b"", **kwargs):
        if metadata["op"] == "complete":
            durable.update(json.loads(manager.state_path.read_text()))
        result = original(metadata, raw, **kwargs)
        if metadata["op"] == "complete":
            committed.update(result)
            helper.close()
            return {"code": "gone", "error": "Lost publication response"}
        return result

    monkeypatch.setattr(helper, "call", lose)
    url = BASE + "/" + uid + "/complete?created_by=alice"
    result = await client.post(url)
    assert result.status_code == 200
    assert result.json() == committed
    assert result.json()["outcome"] == expected
    assert durable[uid].get("publication") is not None
    target = workspace / result.json()["final_name"]
    if sys.platform == "darwin":
        import subprocess

        assert "user.omnigent-upload-receipt" not in subprocess.check_output(
            ["xattr", str(target)], text=True
        )
    else:
        assert "user.omnigent-upload-receipt" not in os.listxattr(target)
    await manager.stop()
    manager.state_path.write_text(json.dumps(durable))
    fresh = restart(manager)
    try:
        await fresh.start()
        await fresh.sweep()
        assert fresh.records[uid].result == committed
    finally:
        await fresh.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["complete", "status", "abort", "append"])
async def test_request_reconciles_before_background_startup(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    manager = client._transport.app.state.workspace_uploads
    response = await create(client, size=0)
    uid = response.json()["upload_id"]
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    original = helper.call
    durable = {}

    def capture(metadata, raw=b"", **kwargs):
        if metadata["op"] == "complete":
            durable.update(json.loads(manager.state_path.read_text()))
        return original(metadata, raw, **kwargs)

    monkeypatch.setattr(helper, "call", capture)
    committed = await client.post(BASE + "/" + uid + "/complete?created_by=alice")
    assert committed.status_code == 200
    await manager.stop()
    manager.state_path.write_text(json.dumps(durable))
    fresh = restart(manager)
    entered, release = asyncio.Event(), asyncio.Event()
    real_sweep = fresh.sweep

    async def delayed():
        entered.set()
        await release.wait()
        await real_sweep()

    monkeypatch.setattr(fresh, "sweep", delayed)
    await fresh.start()
    assert entered.is_set()
    try:
        if op == "append":
            from omnigent.runner.workspace_upload import RouteError

            with pytest.raises(RouteError) as exc:
                await fresh.operate("session", DEFAULT_ENVIRONMENT_ID, uid, "alice", op)
            assert exc.value.code == "already_complete"
        elif op == "status":
            assert await fresh.operate("session", DEFAULT_ENVIRONMENT_ID, uid, "alice", op) == {
                "offset": 0,
                "size": 0,
            }
        else:
            assert (
                await fresh.operate("session", DEFAULT_ENVIRONMENT_ID, uid, "alice", op)
                == committed.json()
            )
        assert fresh.records[uid].result == committed.json()
    finally:
        release.set()
        await fresh.stop()


@pytest.mark.asyncio
async def test_cancelled_stop_still_settles_and_closes(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = client._transport.app.state.workspace_uploads
    response = await create(client)
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    original = manager._call
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(uid, record, op, *args, **kwargs):
        if op == "append":
            entered.set()
            await release.wait()
        return await original(uid, record, op, *args, **kwargs)

    monkeypatch.setattr(manager, "_call", delayed)
    request = asyncio.create_task(
        client.patch(
            BASE + "/" + response.json()["upload_id"] + "?created_by=alice",
            headers=HEADERS,
            content=b"a",
        )
    )
    await entered.wait()
    stopping = asyncio.create_task(manager.stop())
    await asyncio.sleep(0)
    stopping.cancel()
    await asyncio.sleep(0.02)
    waiting = not stopping.done()
    release.set()
    await request
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert waiting
    assert helper._closed
    assert not manager.helpers


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["open", "committed", "gone"])
async def test_cross_session_upload_rejected(client: httpx.AsyncClient, phase: str) -> None:
    manager = client._transport.app.state.workspace_uploads
    response = await create(client, size=0)
    uid = response.json()["upload_id"]
    if phase == "committed":
        await manager.operate("session", DEFAULT_ENVIRONMENT_ID, uid, "alice", "complete")
    if phase == "gone":
        await manager.operate("session", DEFAULT_ENVIRONMENT_ID, uid, "alice", "abort")
    from omnigent.runner.workspace_upload import RouteError

    for op in ("status", "append", "complete", "abort"):
        with pytest.raises(RouteError) as exc:
            await manager.operate("session-b", DEFAULT_ENVIRONMENT_ID, uid, "alice", op)
        assert exc.value.status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["replace", "rename"])
async def test_publication_target_changes_after_wal(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflict: str
) -> None:
    workspace = tmp_path / "workspace"
    if conflict == "rename":
        (workspace / "payload.bin").write_text("original")
    manager = client._transport.app.state.workspace_uploads
    response = await create(client, size=0, on_conflict=conflict)
    original = manager._call
    first = True
    planned = []

    async def change(uid, record, op, *args, **kwargs):
        nonlocal first
        if op == "complete":
            planned.append(json.loads(manager.state_path.read_text())[uid]["publication"])
            if first:
                first = False
                (workspace / kwargs["publication"]["final_name"]).write_text("intervening")
        return await original(uid, record, op, *args, **kwargs)

    monkeypatch.setattr(manager, "_call", change)
    result = await client.post(
        BASE + "/" + response.json()["upload_id"] + "/complete?created_by=alice"
    )
    assert result.status_code == 200
    assert len(planned) == 2
    if conflict == "replace":
        assert result.json()["outcome"] == "replaced"
        registry = await manager.resolve_registry("session")
        assert registry.get_baseline("payload.bin") == "intervening"
    else:
        assert result.json()["final_name"] == "payload (2).bin"
        assert (workspace / "payload (1).bin").read_text() == "intervening"
        assert (workspace / "payload.bin").read_text() == "original"


@pytest.mark.asyncio
async def test_restart_after_wal_before_publish_never_reports_commit(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.runner.workspace_upload import RouteError

    manager = client._transport.app.state.workspace_uploads
    response = await create(client, size=0)
    uid = response.json()["upload_id"]
    original = manager._call

    async def lost(uid, record, op, *args, **kwargs):
        if op == "complete":
            raise RouteError(410, "gone", "Lost helper before publication")
        return await original(uid, record, op, *args, **kwargs)

    monkeypatch.setattr(manager, "_call", lost)
    assert (await client.post(BASE + "/" + uid + "/complete?created_by=alice")).status_code == 410
    data = json.loads(manager.state_path.read_text())[uid]
    assert data["publication"] is not None
    staging = Path(data["staging_path"])
    await manager.stop()
    fresh = restart(manager)
    try:
        await fresh.start()
        await fresh.sweep()
        assert uid not in fresh.records
        assert not staging.exists()
        assert not (staging.parent / "payload.bin").exists()
    finally:
        await fresh.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["plan_response", "index_save"])
async def test_publication_never_precedes_durable_plan(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from omnigent.runner.workspace_upload import RouteError

    manager = client._transport.app.state.workspace_uploads
    response = await create(client, size=0)
    uid = response.json()["upload_id"]
    record = manager.records[uid]
    staging = Path(record.staging_path)
    original = manager._call
    published = []

    async def fail(uid, record, op, *args, **kwargs):
        if op == "complete":
            published.append(True)
        result = await original(uid, record, op, *args, **kwargs)
        if op == "plan" and failure == "plan_response":
            raise RouteError(410, "gone", "Lost plan response")
        return result

    def save_failure():
        raise OSError("Index storage failure")

    with monkeypatch.context() as patch:
        patch.setattr(manager, "_call", fail)
        if failure == "index_save":
            patch.setattr(manager, "_save", save_failure)
        result = await client.post(BASE + "/" + uid + "/complete?created_by=alice")
    assert result.status_code == (410 if failure == "plan_response" else 500)
    assert not published
    assert staging.exists()
    assert not (staging.parent / "payload.bin").exists()
    assert (await client.post(BASE + "/" + uid + "/complete?created_by=alice")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    ["post_begin", "begin_failure_drop", "abort", "plan", "complete", "recovery", "expiry"],
)
@pytest.mark.parametrize(
    "error_number,status,code", [(errno.ENOSPC, 507, "no_space"), (errno.EIO, 500, "io_error")]
)
async def test_non_initial_index_error_hides_host_path(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stage: str,
    error_number: int,
    status: int,
    code: str,
) -> None:
    import omnigent.runner.workspace_upload as upload

    manager = client._transport.app.state.workspace_uploads
    if stage != "post_begin":
        created = await create(client, size=0)
        assert created.status_code == 201
        uid = created.json()["upload_id"]
        url = BASE + "/" + uid + "?created_by=alice"
        complete_url = BASE + "/" + uid + "/complete?created_by=alice"
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    original_call, original_save = helper.call, manager._save
    saves = 0
    fail_at = 2 if stage in ("post_begin", "begin_failure_drop", "complete", "recovery") else 1
    published = False

    def call(metadata, raw=b"", **kwargs):
        nonlocal published
        if stage == "begin_failure_drop" and metadata["op"] == "begin":
            return {"code": "invalid_request", "error": "Begin refused"}
        result = original_call(metadata, raw, **kwargs)
        if metadata["op"] == "complete":
            published = True
            if stage == "recovery":
                helper.close()
                return {"code": "gone", "error": "Lost finalize response"}
        return result

    def fail_save():
        nonlocal saves
        saves += 1
        if saves == fail_at:
            raise OSError(error_number, "x", "/secret/path")
        original_save()

    with monkeypatch.context() as fault:
        fault.setattr(helper, "call", call)
        fault.setattr(manager, "_save", fail_save)
        if stage in ("post_begin", "begin_failure_drop"):
            response = await create(client, name="new")
        elif stage == "abort":
            response = await client.delete(url)
        elif stage == "expiry":
            manager.records[uid].last_activity = upload._now() - manager.limits.idle_ttl_s - 1
            response = await client.post(complete_url)
        else:
            response = await client.post(complete_url)
    assert saves == fail_at
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["message"] == (
        "Insufficient storage" if code == "no_space" else "Upload I/O failed"
    )
    assert "/secret/path" not in response.text
    assert "/secret/path" in caplog.text
    assert published == (stage in ("complete", "recovery"))


@pytest.mark.asyncio
@pytest.mark.parametrize("error_number", [errno.ENOSPC, errno.EIO])
async def test_cancelled_failing_save_preserves_cancellation(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, error_number: int
) -> None:
    import threading

    manager = client._transport.app.state.workspace_uploads
    await create(client)
    entered, release = threading.Event(), threading.Event()

    def fail_save():
        entered.set()
        assert release.wait(5)
        raise OSError(error_number, "settled save failed")

    with monkeypatch.context() as fault:
        fault.setattr(manager, "_save", fail_save)
        task = asyncio.create_task(manager._persist())
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert manager.lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert manager.index_dirty
    assert not manager.lock.locked()
    await manager._persist()
    assert not manager.index_dirty


def test_retry_after_distinguishes_busy_from_upload_limit() -> None:
    from omnigent.runner.workspace_upload import RouteError

    for _ in range(20):
        assert (
            1
            <= int(RouteError(429, "busy", "Helper is busy").response().headers["Retry-After"])
            <= 5
        )
    assert (
        RouteError(429, "upload_limit", "Session limit reached").response().headers["Retry-After"]
        == "60"
    )


@pytest.mark.asyncio
async def test_helper_queue_preserves_default_executor_capacity(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import omnigent.inner.os_env_upload as upload

    manager = client._transport.app.state.workspace_uploads
    manager.limits = UploadLimits(max_open_uploads=16)
    ids = [
        (await create(client, name=f"queued-{i}", size=1)).json()["upload_id"] for i in range(9)
    ]
    original = upload.read_frame
    entered, release = threading.Event(), threading.Event()

    def slow(stream):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        return original(stream)

    monkeypatch.setattr(upload, "read_frame", slow)
    previous_executor = asyncio.get_running_loop()._default_executor
    assert previous_executor is not None
    with ThreadPoolExecutor(max_workers=8) as executor:
        asyncio.get_running_loop().set_default_executor(executor)
        holder = asyncio.create_task(
            client.patch(BASE + "/" + ids[0] + "?created_by=alice", headers=HEADERS, content=b"x")
        )
        waiters = []
        background = []
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            waiters = [
                asyncio.create_task(client.head(BASE + "/" + uid + "?created_by=alice"))
                for uid in ids[1:]
            ]
            await asyncio.sleep(0.1)
            background = [
                asyncio.create_task(asyncio.to_thread(lambda: 42)),
                asyncio.create_task(manager._persist()),
            ]
            done, pending = await asyncio.wait(background, timeout=0.5)
            assert not pending, "Helper waiters blocked unrelated executor work or an index save"
            assert len(done) == 2
            assert background[0].result() == 42
            assert json.loads(manager.state_path.read_text())
        finally:
            release.set()
            results = await asyncio.gather(holder, *waiters, return_exceptions=True)
            await asyncio.gather(*background, return_exceptions=True)
            asyncio.get_running_loop().set_default_executor(previous_executor)
    assert all(isinstance(r, httpx.Response) and r.status_code in (200, 204) for r in results)


@pytest.mark.asyncio
async def test_expired_receipt_forgotten_by_pinned_helper(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    import omnigent.runner.workspace_upload as upload

    manager = client._transport.app.state.workspace_uploads
    first = await create(client, size=0)
    uid = first.json()["upload_id"]
    url = BASE + "/" + uid + "/complete?created_by=alice"
    receipt = await client.post(url)
    assert (await client.post(url)).json() == receipt.json()
    second = await create(client, name="pinned", size=1)
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    assert (await asyncio.to_thread(helper.call, {"op": "status", "id": uid}))[
        "outcome"
    ] == "created"
    manager.records[uid].last_activity = upload._now() - manager.limits.idle_ttl_s - 1
    await manager.sweep()
    assert uid not in manager.records
    assert not helper._closed
    assert await manager._helper("session", DEFAULT_ENVIRONMENT_ID) is helper
    assert (await asyncio.to_thread(helper.call, {"op": "status", "id": uid}))["code"] == "gone"
    assert (tmp_path / "workspace" / "payload.bin").read_bytes() == b""
    assert (await asyncio.to_thread(helper.call, {"op": "forget", "id": uid})) == {
        "forgotten": True
    }
    assert (
        await asyncio.to_thread(helper.call, {"op": "forget", "id": second.json()["upload_id"]})
    )["code"] == "incomplete"
    assert (
        await client.patch(
            BASE + "/" + second.json()["upload_id"] + "?created_by=alice",
            headers=HEADERS,
            content=b"x",
        )
    ).status_code == 204
    assert (await client.post(url)).status_code == 410


@pytest.mark.asyncio
async def test_abort_removes_staging_after_destination_becomes_out_of_grant_symlink(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    manager = client._transport.app.state.workspace_uploads
    created = await create(client, name="abort.bin", size=1)
    uid = created.json()["upload_id"]
    record = manager.records[uid]
    staging = Path(record.staging_path)
    url = BASE + "/" + uid + "?created_by=alice"
    assert (await client.patch(url, headers=HEADERS, content=b"x")).status_code == 204

    target = tmp_path / "outside.bin"
    target.write_bytes(b"keep")
    (tmp_path / "workspace" / "abort.bin").symlink_to(target)
    response = await client.delete(url)

    assert response.status_code == 204
    assert not staging.exists()
    assert (tmp_path / "workspace" / "abort.bin").is_symlink()
    assert target.read_bytes() == b"keep"
    assert uid not in manager.records
    assert not manager._pinned(("session", DEFAULT_ENVIRONMENT_ID))
    assert (await create(client, name="quota-one", size=1)).status_code == 201
    assert (await create(client, name="quota-two", size=1)).status_code == 201


@pytest.mark.asyncio
async def test_indexed_cleanup_removes_staging_after_destination_becomes_out_of_grant_symlink(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    import omnigent.runner.workspace_upload as upload

    manager = client._transport.app.state.workspace_uploads
    created = await create(client, name="cleanup.bin", size=1)
    uid = created.json()["upload_id"]
    record = manager.records[uid]
    staging = Path(record.staging_path)
    url = BASE + "/" + uid + "?created_by=alice"
    assert (await client.patch(url, headers=HEADERS, content=b"x")).status_code == 204

    target = tmp_path / "outside.bin"
    target.write_bytes(b"keep")
    (tmp_path / "workspace" / "cleanup.bin").symlink_to(target)
    record.last_activity = upload._now() - manager.limits.idle_ttl_s - 1
    await manager.sweep()

    assert uid not in manager.records
    assert not staging.exists()
    assert (tmp_path / "workspace" / "cleanup.bin").is_symlink()
    assert target.read_bytes() == b"keep"
    assert not manager._pinned(("session", DEFAULT_ENVIRONMENT_ID))
    assert (await create(client, name="quota-one", size=1)).status_code == 201
    assert (await create(client, name="quota-two", size=1)).status_code == 201


@pytest.mark.asyncio
async def test_completed_receipts_are_bounded_and_recent_completion_replays(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnigent.runner.workspace_upload as upload

    retention_limit = 4
    monkeypatch.setattr(upload, "COMPLETED_RECEIPTS_MAX", retention_limit, raising=False)
    manager = client._transport.app.state.workspace_uploads
    recent_url = ""
    recent_receipt: dict[str, object] = {}
    oldest_url = ""
    for index in range(retention_limit + 3):
        created = await create(client, name=f"receipt-{index}.bin", size=0)
        uid = created.json()["upload_id"]
        url = BASE + "/" + uid + "/complete?created_by=alice"
        completed = await client.post(url)
        assert completed.status_code == 200
        if index == 0:
            oldest_url = url
        if index == retention_limit + 2:
            recent_url = url
            recent_receipt = completed.json()

    persisted = json.loads(manager.state_path.read_text())
    assert len(manager.records) <= retention_limit
    assert len(persisted) <= retention_limit
    assert (await client.post(recent_url)).json() == recent_receipt
    assert (await client.post(oldest_url)).status_code == 410


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_change", ["deleted", "renamed", "replaced", "file"])
@pytest.mark.parametrize("cleanup", ["sweep", "request", "restart"])
async def test_expiry_drops_upload_with_gone_parent(
    client: httpx.AsyncClient, tmp_path: Path, parent_change: str, cleanup: str
) -> None:
    import omnigent.runner.workspace_upload as upload

    manager = client._transport.app.state.workspace_uploads
    manager.limits = UploadLimits(max_open_uploads=1)
    sub = tmp_path / "workspace" / "sub"
    sub.mkdir()
    created = await create(client, dir="sub", size=1)
    uid = created.json()["upload_id"]
    staging = Path(manager.records[uid].staging_path)
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    if parent_change == "deleted":
        staging.unlink()
        sub.rmdir()
    else:
        sub.rename(sub.parent / "retired")
        if parent_change == "replaced":
            sub.mkdir()
        elif parent_change == "file":
            sub.write_bytes(b"unrelated")
    manager.records[uid].last_activity = upload._now() - manager.limits.idle_ttl_s - 1
    await manager._persist()
    url = BASE + "/" + uid + "?created_by=alice"
    if cleanup == "request":
        assert (await client.head(url)).status_code == 410
    elif cleanup == "restart":
        await manager.stop()
        fresh = restart(manager)
        try:
            await fresh.sweep()
            assert uid not in fresh.records
            assert not json.loads(fresh.state_path.read_text())
            with pytest.raises(upload.RouteError) as exc:
                await fresh.operate("session", DEFAULT_ENVIRONMENT_ID, uid, "alice", "status")
            assert exc.value.status == 410
            if parent_change != "deleted":
                assert (sub.parent / "retired" / staging.name).exists()
            if parent_change == "file":
                assert sub.read_bytes() == b"unrelated"
            fresh.limits = manager.limits
            declaration = upload.UploadDeclaration(
                dir="", name="quota-free", size=1, created_by="alice"
            )
            assert (await fresh.create("session", DEFAULT_ENVIRONMENT_ID, declaration))[
                "offset"
            ] == 0
        finally:
            await fresh.stop()
        return
    else:
        await manager.sweep()
    assert uid not in manager.records
    assert uid not in json.loads(manager.state_path.read_text())
    assert (await client.head(url)).status_code == 410
    assert (await asyncio.to_thread(helper.call, {"op": "status", "id": uid}))["code"] == "gone"
    if parent_change != "deleted":
        assert (sub.parent / "retired" / staging.name).exists()
    if parent_change == "file":
        assert sub.read_bytes() == b"unrelated"
    assert (await create(client, name="quota-free", size=1)).status_code == 201


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["sweep", "request", "restart"])
async def test_expiry_drops_upload_with_replaced_staging(
    client: httpx.AsyncClient, cleanup: str
) -> None:
    import omnigent.runner.workspace_upload as upload

    manager = client._transport.app.state.workspace_uploads
    manager.limits = UploadLimits(max_open_uploads=1, max_open_bytes=1)
    created = await create(client, size=1)
    uid = created.json()["upload_id"]
    staging = Path(manager.records[uid].staging_path)
    moved = staging.with_name("owned-moved")
    staging.rename(moved)
    unrelated = b"unrelated occupant\x00\xff"
    staging.write_bytes(unrelated)
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    manager.records[uid].last_activity = upload._now() - manager.limits.idle_ttl_s - 1
    await manager._persist()
    url = BASE + "/" + uid + "?created_by=alice"
    fresh = None
    try:
        if cleanup == "request":
            assert (await client.head(url)).status_code == 410
        elif cleanup == "restart":
            await manager.stop()
            fresh = restart(manager)
            fresh.limits = manager.limits
            manager = fresh
            await manager.sweep()
        else:
            await manager.sweep()
        assert uid not in manager.records
        assert uid not in manager.upload_locks
        assert uid not in json.loads(manager.state_path.read_text())
        assert staging.read_bytes() == unrelated
        assert moved.read_bytes() == b""
        with pytest.raises(upload.RouteError) as exc:
            await manager.operate("session", DEFAULT_ENVIRONMENT_ID, uid, "alice", "status")
        assert exc.value.status == 410
        if fresh is None:
            assert (await client.head(url)).status_code == 410
            assert (await asyncio.to_thread(helper.call, {"op": "status", "id": uid}))[
                "code"
            ] == "gone"
        declaration = upload.UploadDeclaration(
            dir="", name="quota-free", size=1, created_by="alice"
        )
        assert (await manager.create("session", DEFAULT_ENVIRONMENT_ID, declaration))[
            "offset"
        ] == 0
        assert staging.read_bytes() == unrelated
    finally:
        if fresh is not None:
            await fresh.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("holder_op", ["plan", "complete"])
async def test_async_short_operation_queue_budget(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, holder_op: str
) -> None:
    import threading
    import time

    import omnigent.inner.os_env_upload as upload
    from omnigent.runner.workspace_upload import RouteError

    manager = client._transport.app.state.workspace_uploads
    ids = [(await create(client, name=str(i), size=0)).json()["upload_id"] for i in range(2)]
    original = upload.read_frame
    entered, release = threading.Event(), threading.Event()

    def slow(stream):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original(stream)

    monkeypatch.setattr(upload, "read_frame", slow)
    holder = asyncio.create_task(manager._call(ids[0], manager.records[ids[0]], holder_op))
    assert await asyncio.to_thread(entered.wait, 5)
    timer = threading.Timer(2, release.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(RouteError) as exc:
            await manager._call(ids[1], manager.records[ids[1]], "status")
        assert exc.value.status == 429 and exc.value.code == "busy"
        assert 0.8 <= time.monotonic() - started < 1.5
    finally:
        release.set()
        timer.cancel()
        timer.join()
        await holder
    assert (await manager._call(ids[1], manager.records[ids[1]], "status"))["offset"] == 0


@pytest.mark.asyncio
async def test_helper_deadline_includes_executor_queue(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import omnigent.inner.os_env_upload as upload
    from omnigent.runner.workspace_upload import RouteError

    manager = client._transport.app.state.workspace_uploads
    uid = (await create(client)).json()["upload_id"]
    entered, release = threading.Event(), threading.Event()
    written = []
    original = upload.write_frame

    def blocked():
        entered.set()
        assert release.wait(5)

    def write(stream, metadata, raw=b""):
        written.append(metadata["op"])
        original(stream, metadata, raw)

    monkeypatch.setattr(upload, "CALL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(upload, "write_frame", write)
    with ThreadPoolExecutor(max_workers=1) as executor, monkeypatch.context() as fault:
        fault.setattr(manager, "ipc_executor", executor)
        blocker = asyncio.get_running_loop().run_in_executor(executor, blocked)
        while not entered.is_set():
            await asyncio.sleep(0.001)
        timer = threading.Timer(0.15, release.set)
        timer.start()
        try:
            with pytest.raises(RouteError) as exc:
                await manager._call(uid, manager.records[uid], "status")
            assert exc.value.code == "busy"
            assert not written
        finally:
            release.set()
            timer.cancel()
            timer.join()
            await blocker


@pytest.mark.asyncio
async def test_cancelled_helper_call_keeps_gate_until_settled(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    import omnigent.inner.os_env_upload as upload

    manager = client._transport.app.state.workspace_uploads
    ids = [(await create(client, name=str(i))).json()["upload_id"] for i in range(2)]
    original = upload.read_frame
    entered, release = threading.Event(), threading.Event()
    reads = []

    def slow(stream):
        reads.append("start")
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        result = original(stream)
        reads.append("end")
        return result

    monkeypatch.setattr(upload, "read_frame", slow)
    first = asyncio.create_task(manager._call(ids[0], manager.records[ids[0]], "status"))
    assert await asyncio.to_thread(entered.wait, 5)
    first.cancel()
    waiter = asyncio.create_task(manager._call(ids[1], manager.records[ids[1]], "status"))
    await asyncio.sleep(0.05)
    still_settling = not first.done() and not waiter.done()
    release.set()
    result = await asyncio.gather(first, waiter, return_exceptions=True)
    assert still_settling
    assert isinstance(result[0], asyncio.CancelledError)
    assert result[1] == {"offset": 0, "size": 3}
    assert reads == ["start", "end", "start", "end"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value,code",
    [
        ("dir", "/".join(["\x01" * 250] * 15), "invalid_path"),
        ("dir", "sub\x7f", "invalid_path"),
        ("name", "file\x01", "invalid_name"),
        ("name", "file\x7f", "invalid_name"),
    ],
    ids=["long-control-dir", "del-dir", "control-name", "del-name"],
)
async def test_control_character_paths_rejected_before_admission(
    client: httpx.AsyncClient, field: str, value: str, code: str
) -> None:
    from omnigent.runner.workspace_upload import RouteError, UploadDeclaration

    manager = client._transport.app.state.workspace_uploads
    with pytest.raises(RouteError) as exc:
        UploadDeclaration.model_validate(
            {"dir": "", "name": "file", "size": 0, "created_by": "alice", field: value}
        )
    assert exc.value.status == 400 and exc.value.code == code
    response = await create(client, **{field: value})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code
    assert not manager.records and not manager.upload_locks and not manager.helpers
    assert not manager.inflight and not manager.state_path.exists()
    assert (await create(client, name="first")).status_code == 201
    assert (await create(client, name="second")).status_code == 201


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["resume", "recover", "cleanup"])
async def test_maximum_record_frames_fit(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    from omnigent.inner.os_env_upload import MAX_METADATA_BYTES, staging_basename, write_frame
    from omnigent.runner.workspace_upload import UploadDeclaration, UploadRecord

    manager = client._transport.app.state.workspace_uploads
    uid = "f" * 32
    directory = "/".join(["\U00010000" * 60] * 17)
    name = "\U00010000" * 63 + "abc"
    assert len(directory.encode()) == 4096 and len(name.encode()) == 255
    declaration = UploadDeclaration(
        dir=directory,
        name=name,
        size=1024**3,
        created_by="\x01" * 256,
        on_conflict="replace",
        sha256="f" * 64,
    )
    # A workspace prefix is not governed by the declaration's path validation.
    cwd = "/" + "/".join(["\x01" * 255] * 16)
    publication = {
        "path": directory + "/" + name,
        "final_name": name,
        "size": declaration.size,
        "sha256": "f" * 64,
        "outcome": "replaced",
        "target_dev": 2**64 - 1,
        "target_ino": 2**64 - 1,
    }
    record = UploadRecord(
        **declaration.model_dump(),
        session_id="session",
        environment_id=DEFAULT_ENVIRONMENT_ID,
        created_at=9999999999.0,
        last_activity=9999999999.0,
        staging_path=cwd + "/" + directory + "/" + staging_basename(name, uid),
        staging_dev=2**64 - 1,
        staging_ino=2**64 - 1,
        parent_dev=2**64 - 1,
        parent_ino=2**64 - 1,
        finalizing=True,
        publication=publication,
    )
    manager.records[uid] = record
    frames: list[dict[str, object]] = []

    async def rpc(
        binding: object, metadata: dict[str, object], raw: bytes = b""
    ) -> dict[str, object]:
        frames.append(metadata)
        if metadata["op"] == "status" and len(frames) == 1:
            return {"code": "gone", "error": "Helper restarted"}
        if metadata["op"] == "recover":
            return {k: publication[k] for k in ("path", "final_name", "size", "sha256", "outcome")}
        return {"offset": 0}

    async def registry_for(session: str) -> None:
        return None

    monkeypatch.setattr(manager, "_helper_rpc", rpc)
    monkeypatch.setattr(manager, "resolve_registry", registry_for)
    if op == "resume":
        await manager._call(uid, record, "status")
    elif op == "recover":
        await manager._recover(uid, record)
    else:
        await manager._remove(uid, record)
    metadata = next(frame for frame in frames if frame["op"] == op)
    encoded = json.dumps(metadata, separators=(",", ":")).encode()
    assert len(encoded) <= MAX_METADATA_BYTES
    write_frame(io.BytesIO(), metadata)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_number", [errno.ENAMETOOLONG, errno.ELOOP])
async def test_advisory_path_errors_are_client_errors(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_number: int,
) -> None:
    manager = client._transport.app.state.workspace_uploads

    def fail(path: Path) -> None:
        raise OSError(error_number, "Invalid client path", str(path))

    monkeypatch.setattr("omnigent.runner.workspace_upload.shutil.disk_usage", fail)
    response = await create(client)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_path"
    assert not [r for r in caplog.records if r.levelname in ("ERROR", "CRITICAL")]
    assert not manager.records and not manager.upload_locks and not manager.state_path.exists()


@pytest.mark.asyncio
async def test_over_path_max_directory_is_client_error(
    client: httpx.AsyncClient,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = client._transport.app.state.workspace_uploads
    workspace = tmp_path / "workspace"
    path_max = os.pathconf(workspace, "PC_PATH_MAX")
    part = "d" * 200
    count = (path_max - len(str(workspace).encode())) // (len(part) + 1) + 1
    directory = "/".join([part] * count)
    assert len(directory.encode()) <= 4096
    assert len(str(workspace / directory).encode()) >= path_max
    fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    parents = []
    try:
        for _ in range(count):
            os.mkdir(part, dir_fd=fd)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            parents.append(fd)
            fd = child
        response = await create(client, dir=directory)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_path"
        assert not [r for r in caplog.records if r.levelname in ("ERROR", "CRITICAL")]
        assert not manager.records and not manager.upload_locks and not manager.state_path.exists()
    finally:
        os.close(fd)
        for parent in reversed(parents):
            os.rmdir(part, dir_fd=parent)
            os.close(parent)


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_change", ["renamed", "replaced", "file"])
@pytest.mark.parametrize("retire", ["restart_sweep", "status", "complete", "recovery"])
async def test_finalizing_parent_loss_retires_upload(
    client: httpx.AsyncClient, tmp_path: Path, parent_change: str, retire: str
) -> None:
    from omnigent.runner.workspace_upload import RouteError, UploadDeclaration

    manager = client._transport.app.state.workspace_uploads
    payload = b"\x00\xffabc"
    original = b"unchanged destination\x00\xfe"
    manager.limits = UploadLimits(max_open_uploads=1, max_open_bytes=len(payload))
    sub = tmp_path / "workspace" / "sub"
    sub.mkdir()
    (sub / "payload.bin").write_bytes(original)
    response = await create(client, dir="sub", size=len(payload), on_conflict="replace")
    assert response.status_code == 201
    uid = response.json()["upload_id"]
    url = BASE + "/" + uid + "?created_by=alice"
    assert (await client.patch(url, headers=HEADERS, content=payload)).status_code == 204
    record = manager.records[uid]
    staging = Path(record.staging_path)
    record.publication = await manager._call(uid, record, "plan")
    assert record.publication["sha256"] == hashlib.sha256(payload).hexdigest()
    record.finalizing = True
    await manager._persist()
    persisted = json.loads(manager.state_path.read_text())[uid]
    assert persisted["finalizing"] and persisted["publication"] == record.publication
    assert persisted["result"] is None
    sub.rename(sub.parent / "retired")
    moved = sub.parent / "retired"
    if parent_change == "replaced":
        sub.mkdir()
        (sub / "unrelated").write_bytes(original)
    elif parent_change == "file":
        sub.write_bytes(original)
    with pytest.raises(RouteError) as failed_complete:
        await manager._call(uid, record, "complete", publication=record.publication)
    assert failed_complete.value.code == "invalid_path"
    assert (moved / staging.name).read_bytes() == payload
    assert (moved / "payload.bin").read_bytes() == original
    fresh = None
    try:
        if retire == "restart_sweep":
            await manager.stop()
            fresh = restart(manager)
            fresh.limits = manager.limits
            manager = fresh
            assert manager.records[uid].finalizing and uid in manager.restored
            await manager.sweep(now=record.created_at + manager.limits.max_age_s + 1)
        elif retire == "recovery":
            with pytest.raises(RouteError) as failed_recovery:
                await manager._recover(uid, record)
            assert failed_recovery.value.status == 410
        elif retire == "status":
            assert (await client.head(url)).status_code == 410
        else:
            assert (await client.post(url.replace("?", "/complete?"))).status_code == 410
        assert uid not in manager.records
        assert uid not in manager.upload_locks
        assert uid not in manager.restored
        assert uid not in json.loads(manager.state_path.read_text())
        assert not manager._pinned(("session", DEFAULT_ENVIRONMENT_ID))
        helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
        assert (await asyncio.to_thread(helper.call, {"op": "status", "id": uid}))[
            "code"
        ] == "gone"
        for op in ("status", "complete"):
            with pytest.raises(RouteError) as expired:
                await manager.operate("session", DEFAULT_ENVIRONMENT_ID, uid, "alice", op)
            assert expired.value.status == 410
        declaration = UploadDeclaration(
            dir="", name="quota-free", size=len(payload), created_by="alice"
        )
        assert (await manager.create("session", DEFAULT_ENVIRONMENT_ID, declaration))[
            "offset"
        ] == 0
        assert (moved / staging.name).read_bytes() == payload
        assert (moved / "payload.bin").read_bytes() == original
        if parent_change == "replaced":
            assert (sub / "unrelated").read_bytes() == original
        elif parent_change == "file":
            assert sub.read_bytes() == original
    finally:
        if fresh is not None:
            await fresh.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("default_workers", [4, 8])
async def test_distinct_helpers_preserve_default_executor_capacity(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, default_workers: int
) -> None:
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from io import TextIOWrapper
    from typing import cast

    import omnigent.inner.os_env_upload as upload
    from omnigent.runner.workspace_upload import RouteError, UploadDeclaration

    manager = client._transport.app.state.workspace_uploads
    ids = [
        (
            await manager.create(
                f"session-{i}",
                DEFAULT_ENVIRONMENT_ID,
                UploadDeclaration(dir="", name=f"distinct-{i}", size=0, created_by="alice"),
            )
        )["upload_id"]
        for i in range(9)
    ]
    original = upload.read_frame
    release = threading.Event()
    entered = set()
    guard = threading.Lock()
    held_streams = set()
    for i in range(8):
        proc = manager.helpers[(f"session-{i}", DEFAULT_ENVIRONMENT_ID)].client._proc
        assert proc is not None
        held_streams.add(cast(TextIOWrapper, proc.stdout).buffer)

    def slow(stream):
        if stream in held_streams:
            with guard:
                entered.add(stream)
            assert release.wait(10)
        return original(stream)

    monkeypatch.setattr(upload, "read_frame", slow)
    previous_executor = asyncio.get_running_loop()._default_executor
    assert previous_executor is not None
    with ThreadPoolExecutor(max_workers=default_workers) as executor:
        asyncio.get_running_loop().set_default_executor(executor)
        tasks = [
            asyncio.create_task(manager._call(uid, manager.records[uid], "plan"))
            for uid in ids[:8]
        ]
        background = []
        try:
            async with asyncio.timeout(5):
                while len(entered) < 4:
                    await asyncio.sleep(0.01)
            background = [
                asyncio.create_task(asyncio.to_thread(lambda: 42)),
                asyncio.create_task(manager._persist()),
            ]
            done, pending = await asyncio.wait(background, timeout=0.5)
            assert not pending, "Distinct helpers starved executor work or an index save"
            assert len(done) == 2
            assert background[0].result() == 42
            assert len(json.loads(manager.state_path.read_text())) == 9
            idle = ids[8]
            assert (
                await asyncio.wait_for(manager._call(idle, manager.records[idle], "status"), 0.5)
            )["offset"] == 0
            async with asyncio.timeout(0.5):
                while len(entered) < 8:
                    await asyncio.sleep(0.01)
            # A short call only waits briefly behind a long call on its own binding.
            started = time.monotonic()
            with pytest.raises(RouteError) as busy:
                await asyncio.wait_for(
                    manager._call(ids[0], manager.records[ids[0]], "status"), 1.5
                )
            assert busy.value.code == "busy"
            assert 0.8 <= time.monotonic() - started < 1.5
            queued = asyncio.create_task(manager._call(ids[0], manager.records[ids[0]], "status"))
            await asyncio.sleep(0)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            tasks[0].cancel()
            await asyncio.sleep(0)
            assert not tasks[0].done()
        finally:
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*background, return_exceptions=True)
            asyncio.get_running_loop().set_default_executor(previous_executor)
    assert sum(isinstance(result, asyncio.CancelledError) for result in results) == 1
    assert all(
        not binding.gate.locked() and binding.users == 0 for binding in manager.helpers.values()
    )
    assert (await manager._call(ids[0], manager.records[ids[0]], "plan"))["outcome"] == "created"


@pytest.mark.asyncio
async def test_sweep_does_not_recreate_aborted_upload_lock(client: httpx.AsyncClient) -> None:
    manager = client._transport.app.state.workspace_uploads
    first = (await create(client)).json()["upload_id"]
    second = (await create(client, name="second")).json()["upload_id"]
    lock = manager.upload_locks[first]
    await lock.acquire()
    sweep = asyncio.create_task(manager.sweep())
    try:
        await asyncio.sleep(0)
        assert not sweep.done()
        assert (await client.delete(BASE + "/" + second + "?created_by=alice")).status_code == 204
        assert second not in manager.records
        assert second not in manager.upload_locks
    finally:
        lock.release()
        await sweep
    assert second not in manager.upload_locks
    assert first in manager.records


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["idle", "inflight", "blocked_worker"])
async def test_ipc_executor_shutdown(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    import threading

    import omnigent.inner.os_env_upload as upload

    manager = client._transport.app.state.workspace_uploads
    uid = (await create(client, size=0)).json()["upload_id"]
    helper = await manager._helper("session", DEFAULT_ENVIRONMENT_ID)
    executor = manager.ipc_executor
    entered, release = threading.Event(), threading.Event()
    original = upload.read_frame
    request = stopping = worker = None

    def slow(stream):
        entered.set()
        assert release.wait(5)
        return original(stream)

    def blocked_worker():
        entered.set()
        assert release.wait(5)

    try:
        if state == "inflight":
            monkeypatch.setattr(upload, "read_frame", slow)
            request = asyncio.create_task(
                client.post(BASE + "/" + uid + "/complete?created_by=alice")
            )
            assert await asyncio.to_thread(entered.wait, 5)
            stopping = asyncio.create_task(manager.stop())
            await asyncio.sleep(0.02)
            assert not stopping.done()
            release.set()
            assert (await request).status_code == 200
            await asyncio.wait_for(stopping, 0.5)
        else:
            if state == "blocked_worker":
                worker = executor.submit(blocked_worker)
                assert await asyncio.to_thread(entered.wait, 5)
            await asyncio.wait_for(manager.stop(), 0.5)
            if worker is not None:
                assert not worker.done(), "Teardown joined the blocked executor worker"
        assert helper._closed and not manager.helpers
        with pytest.raises(RuntimeError, match="shutdown"):
            executor.submit(lambda: None)
    finally:
        release.set()
        if request is not None:
            await request
        if stopping is not None:
            await stopping
        if worker is not None:
            await asyncio.wrap_future(worker)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,status",
    [
        ("not_found", 404),
        ("missing_agent", 410),
        ("value_error", 400),
        ("http_exception", 404),
        ("bug", 500),
    ],
)
async def test_upload_resolver_errors_match_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, status: int
) -> None:
    from fastapi import HTTPException

    from omnigent.errors import ErrorCode, OmnigentError
    from omnigent.runner import create_runner_app

    def snapshot(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/session"
        return httpx.Response(200, json={"id": "session", "agent_id": "agent"})

    async def resolve(agent_id: str, session_id: str | None = None):
        assert agent_id == "agent"
        if failure in ("not_found", "missing_agent"):
            raise OmnigentError(
                "Missing session agent",
                code=ErrorCode.NOT_FOUND
                if failure == "not_found"
                else ErrorCode.SESSION_AGENT_MISSING,
            )
        if failure == "value_error":
            raise ValueError("Invalid session environment")
        if failure == "http_exception":
            raise HTTPException(404, "No OS environment")
        raise RuntimeError("Resolver programming error")

    monkeypatch.setattr(
        "omnigent.runner.workspace_upload.upload_state_path", lambda _: tmp_path / "index.json"
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(snapshot), base_url="http://server"
    ) as server:
        app = create_runner_app(server_client=server, spec_resolver=resolve)
        manager = app.state.workspace_uploads
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://runner",
            ) as c:
                filesystem = await c.get(BASE.replace("/uploads", "/filesystem"))
                capabilities = await c.get(BASE)
                creation = await create(c)
            assert (
                filesystem.status_code
                == capabilities.status_code
                == creation.status_code
                == status
            )
            if failure in ("not_found", "missing_agent"):
                assert filesystem.json() == capabilities.json() == creation.json()
            assert not manager.records and not manager.upload_locks
            assert not manager.state_path.exists()
        finally:
            await manager.stop()
