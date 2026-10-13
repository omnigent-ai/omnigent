"""Dormant runner upload routes and an ownership-bound staging index."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from omnigent.debug_logging import runner_primary_session_id
from omnigent.entities.environment_filesystem import FilesystemPathNotFound, InvalidPath
from omnigent.inner.os_env import CallerProcessOSEnvironment, OSEnvironment
from omnigent.inner.os_env_upload import UploadHelperClient, valid_name
from omnigent.install_ledger import state_dir
from omnigent.runner.environment_filesystem import CallerProcessFilesystem, _validate_path
from omnigent.runtime.filesystem_registry import FilesystemRegistry

WORKSPACE_UPLOAD_CAPABILITY = "workspace_upload_v1"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UploadLimits:
    max_file_bytes: int = 1024**3
    chunk_max_bytes: int = 8 * 1024**2
    max_open_uploads: int = 16
    max_open_bytes: int = 5 * 1024**3
    idle_ttl_s: float = 3600
    max_age_s: float = 86400

    @property
    def chunk_bytes(self) -> int:
        return max(256 * 1024, min(self.chunk_max_bytes, 16 * 1024**2))

    def public(self) -> dict[str, object]:
        return {
            "max_file_bytes": self.max_file_bytes,
            "chunk": {
                "min": 256 * 1024,
                "initial": min(1024**2, self.chunk_bytes),
                "max": self.chunk_bytes,
            },
            "max_open_uploads": self.max_open_uploads,
            "max_open_bytes": self.max_open_bytes,
        }


class UploadRecord(BaseModel):
    session_id: str
    environment_id: str
    dir: str
    name: str
    size: int
    offset: int = 0
    on_conflict: str
    sha256: str | None = None
    created_by: str
    created_at: float
    last_activity: float
    staging_path: str
    staging_dev: int
    staging_ino: int
    parent_dev: int
    parent_ino: int
    result: dict[str, object] | None = None


class UploadDeclaration(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    dir: str
    name: str
    size: int = Field(ge=0)
    on_conflict: str = "fail"
    sha256: str | None = None
    created_by: str = Field(min_length=1)


class RouteError(Exception):
    def __init__(self, status: int, code: str, message: str, offset: object = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.offset = offset

    def response(self) -> JSONResponse:
        headers = {"Upload-Offset": str(self.offset)} if self.offset is not None else {}
        if self.status == 429:
            headers["Retry-After"] = "60"
        return JSONResponse(
            status_code=self.status,
            content={"error": {"code": self.code, "message": str(self)}},
            headers=headers,
        )


def _unwrap(result: dict[str, object]) -> dict[str, object]:
    if "error" not in result:
        return result
    code = str(result.get("code", "upload_failed"))
    status = {
        "file_exists": 409,
        "is_directory": 409,
        "offset_mismatch": 409,
        "incomplete": 409,
        "checksum_mismatch": 409,
        "too_large": 413,
        "gone": 410,
        "no_space": 507,
        "permission_denied": 403,
        "invalid_name": 400,
        "invalid_path": 400,
        "invalid_request": 400,
    }.get(code, 500)
    raise RouteError(status, code, str(result["error"]), result.get("offset"))


def _now() -> float:
    # Persisted deadlines must survive process restarts.
    return datetime.now(UTC).timestamp()


def upload_state_path(workspace: Path | None) -> Path:
    identity = runner_primary_session_id() or str(workspace.resolve() if workspace else "shared")
    key = hashlib.sha256(identity.encode()).hexdigest()
    return state_dir() / "runners" / "workspace-upload" / key / "index.json"


async def workspace_upload_supported(env: OSEnvironment, *, routes_registered: bool) -> bool:
    if not routes_registered or not isinstance(env, CallerProcessOSEnvironment):
        return False
    client = UploadHelperClient(env)
    try:
        result = await asyncio.to_thread(client.call, {"op": "probe"})
        return result.get("raw_upload") is True
    finally:
        await asyncio.to_thread(client.close)


class UploadManager:
    def __init__(
        self,
        *,
        resolve_environment: Callable[[str, str], Awaitable[OSEnvironment]],
        resolve_registry: Callable[[str], Awaitable[FilesystemRegistry | None]],
        state_path: Path,
        limits: UploadLimits,
    ) -> None:
        self.resolve_environment = resolve_environment
        self.resolve_registry = resolve_registry
        self.state_path = state_path
        self.limits = limits
        self.records: dict[str, UploadRecord] = {}
        self.helpers: dict[int, UploadHelperClient] = {}
        self.lock = asyncio.Lock()
        self.task: asyncio.Task[None] | None = None
        self.gone: dict[str, tuple[str, str, str]] = {}
        if state_path.exists():
            self.records = {
                uid: UploadRecord.model_validate(value)
                for uid, value in json.loads(state_path.read_text()).items()
            }
        self.restored = set(self.records)

    async def _helper(self, session_id: str, environment_id: str) -> UploadHelperClient:
        env = await self.resolve_environment(session_id, environment_id)
        if not isinstance(env, CallerProcessOSEnvironment):
            raise RouteError(
                503, "runner_capability_mismatch", "Environment lacks raw upload support"
            )
        key = id(env)
        if key not in self.helpers:
            self.helpers[key] = UploadHelperClient(env)
        return self.helpers[key]

    async def _call(
        self, uid: str, record: UploadRecord, op: str, raw: bytes = b"", **fields: object
    ) -> dict[str, object]:
        helper = await self._helper(record.session_id, record.environment_id)
        return _unwrap(await asyncio.to_thread(helper.call, {"op": op, "id": uid, **fields}, raw))

    def _save(self) -> None:
        self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.state_path.with_name(f".index-{uuid.uuid4().hex}")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(
                    {uid: record.model_dump() for uid, record in self.records.items()}, stream
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _expired(self, uid: str, record: UploadRecord, now: float) -> bool:
        return (
            uid in self.restored
            or now - record.last_activity >= self.limits.idle_ttl_s
            or now - record.created_at >= self.limits.max_age_s
        )

    async def _remove(self, uid: str, record: UploadRecord) -> None:
        await self._call(uid, record, "cleanup", **record.model_dump())
        self.gone[uid] = (record.session_id, record.environment_id, record.created_by)
        del self.records[uid]
        self.restored.discard(uid)

    async def sweep(self, *, now: float | None = None) -> None:
        async with self.lock:
            instant = _now() if now is None else now
            changed = False
            for uid, record in list(self.records.items()):
                if not self._expired(uid, record, instant):
                    continue
                try:
                    await self._remove(uid, record)
                    changed = True
                except (RouteError, OSError):
                    logger.warning("Upload cleanup failed for %s", uid, exc_info=True)
            if changed:
                await asyncio.to_thread(self._save)

    async def start(self) -> None:
        await self.sweep()
        self.task = asyncio.create_task(self._sweep_loop(), name="workspace-upload-sweep")

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            await self.sweep()

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        for helper in self.helpers.values():
            await asyncio.to_thread(helper.close)
        self.helpers.clear()

    async def create(
        self, session_id: str, environment_id: str, declaration: UploadDeclaration
    ) -> dict[str, object]:
        if not valid_name(declaration.name):
            raise RouteError(400, "invalid_name", "Invalid basename")
        if (
            declaration.dir.startswith(("/", "\\"))
            or "\\" in declaration.dir
            or "\0" in declaration.dir
        ):
            raise RouteError(400, "invalid_path", "Destination must be workspace-relative")
        try:
            directory = _validate_path(declaration.dir)
        except InvalidPath as exc:
            raise RouteError(400, "invalid_path", str(exc)) from exc
        if declaration.on_conflict not in ("fail", "replace", "rename") or (
            declaration.sha256 is not None
            and not re.fullmatch(r"[0-9a-f]{64}", declaration.sha256)
        ):
            raise RouteError(400, "invalid_request", "Invalid upload declaration")
        if declaration.size > self.limits.max_file_bytes:
            raise RouteError(413, "too_large", "File exceeds runner limit")
        async with self.lock:
            opened = [
                r for r in self.records.values() if r.session_id == session_id and r.result is None
            ]
            if (
                len(opened) >= self.limits.max_open_uploads
                or sum(r.size for r in opened) + declaration.size > self.limits.max_open_bytes
            ):
                raise RouteError(429, "upload_limit", "Session upload limit reached")
            helper = await self._helper(session_id, environment_id)
            # The helper performs authoritative no-symlink and grant validation.
            free = await asyncio.to_thread(shutil.disk_usage, helper.cwd / directory)
            if declaration.size > free.free:
                raise RouteError(507, "no_space", "Insufficient free disk space")
            uid = uuid.uuid4().hex
            result = _unwrap(
                await asyncio.to_thread(
                    helper.call,
                    {"op": "begin", "id": uid, **declaration.model_dump(), "dir": directory},
                )
            )
            now = _now()
            record = UploadRecord.model_validate(
                {
                    **declaration.model_dump(),
                    **result,
                    "dir": directory,
                    "session_id": session_id,
                    "environment_id": environment_id,
                    "created_at": now,
                    "last_activity": now,
                }
            )
            self.records[uid] = record
            try:
                await asyncio.to_thread(self._save)
            except OSError:
                await self._call(uid, record, "abort")
                del self.records[uid]
                raise
            return {
                "upload_id": uid,
                "offset": 0,
                "chunk_bytes": min(1024**2, self.limits.chunk_bytes),
                "expires_at": now + min(self.limits.idle_ttl_s, self.limits.max_age_s),
            }

    async def operate(
        self,
        session_id: str,
        environment_id: str,
        uid: str,
        owner: str,
        op: str,
        raw: bytes = b"",
        offset: int = 0,
    ) -> dict[str, object]:
        async with self.lock:
            record = self.records.get(uid)
            if record is None:
                expired = self.gone.get(uid)
                if expired is not None and expired != (session_id, environment_id, owner):
                    raise RouteError(
                        403, "upload_owner_mismatch", "Upload belongs to another caller"
                    )
                raise RouteError(
                    410 if expired else 404, "gone" if expired else "not_found", "Unknown upload"
                )
            if (record.session_id, record.environment_id, record.created_by) != (
                session_id,
                environment_id,
                owner,
            ):
                raise RouteError(403, "upload_owner_mismatch", "Upload belongs to another caller")
            if self._expired(uid, record, _now()):
                await self._remove(uid, record)
                await asyncio.to_thread(self._save)
                raise RouteError(410, "gone", "Upload expired")
            if op == "append" and record.result is not None:
                raise RouteError(409, "already_complete", "Upload already committed")
            snapshot: str | None = None
            registry = None
            if op == "complete" and record.result is None:
                registry = await self.resolve_registry(session_id)
                if registry is not None and record.on_conflict == "replace":
                    env = await self.resolve_environment(session_id, environment_id)
                    fs = CallerProcessFilesystem(env)
                    try:
                        existing = await fs.read(
                            f"{record.dir}/{record.name}" if record.dir else record.name
                        )
                        if existing.encoding:
                            snapshot = existing.data.decode(existing.encoding, errors="replace")
                    except FilesystemPathNotFound:
                        pass
            result = await self._call(uid, record, op, raw, offset=offset)
            if op == "abort" and record.result is None:
                self.gone[uid] = (session_id, environment_id, owner)
                del self.records[uid]
            else:
                record.last_activity = _now()
                if "offset" in result:
                    record.offset = cast(int, result["offset"])
                if op == "complete" and record.result is None:
                    record.result = result
                    if registry is not None:
                        path = cast(str, result["path"])
                        if snapshot is not None and result["outcome"] == "replaced":
                            registry.seed_snapshot(path, snapshot, session_id=session_id)
                        registry.record_change(path, "write", session_id)
            await asyncio.to_thread(self._save)
            return result


class UploadLifecycleMiddleware:
    """Observe lifespan even when the native entrypoint supplies its own handler."""

    def __init__(self, app: ASGIApp, manager: UploadManager) -> None:
        self.app = app
        self.manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "lifespan":
            await self.app(scope, receive, send)
            return

        async def lifecycle_send(message: Message) -> None:
            if message["type"] == "lifespan.startup.complete":
                await self.manager.start()
            await send(message)

        try:
            await self.app(scope, receive, lifecycle_send)
        finally:
            await self.manager.stop()


def register_upload_routes(
    app: FastAPI,
    *,
    resolve_environment: Callable[[str, str], Awaitable[OSEnvironment]],
    resolve_registry: Callable[[str], Awaitable[FilesystemRegistry | None]],
    state_path: Path,
    limits: UploadLimits | None = None,
) -> UploadManager:
    manager = UploadManager(
        resolve_environment=resolve_environment,
        resolve_registry=resolve_registry,
        state_path=state_path,
        limits=limits or UploadLimits(),
    )
    app.state.workspace_uploads = manager
    app.add_middleware(UploadLifecycleMiddleware, manager=manager)
    base = "/v1/sessions/{session_id}/resources/environments/{environment_id}/uploads"

    @app.get(base)
    async def capabilities(session_id: str, environment_id: str) -> JSONResponse:
        env = await resolve_environment(session_id, environment_id)
        supported = await workspace_upload_supported(env, routes_registered=True)
        return JSONResponse(content={"supported": supported, "limits": manager.limits.public()})

    @app.post(base)
    async def create(session_id: str, environment_id: str, request: Request) -> JSONResponse:
        try:
            declaration = UploadDeclaration.model_validate(await request.json())
            result = await manager.create(session_id, environment_id, declaration)
            return JSONResponse(status_code=201, content=result)
        except (ValidationError, ValueError):
            return RouteError(400, "invalid_request", "Invalid upload declaration").response()
        except RouteError as exc:
            return exc.response()
        except OSError as exc:
            return RouteError(507, "no_space", str(exc)).response()

    async def operate(
        session_id: str,
        environment_id: str,
        uid: str,
        owner: str,
        op: str,
        request: Request | None = None,
    ) -> Response:
        try:
            raw = b""
            offset = 0
            if op == "append":
                assert request is not None
                if (
                    request.headers.get("content-type", "").split(";")[0].strip().lower()
                    != "application/offset+octet-stream"
                ):
                    raise RouteError(
                        415, "unsupported_media_type", "Raw upload content type required"
                    )
                try:
                    offset = int(request.headers.get("upload-offset", ""))
                except ValueError as exc:
                    raise RouteError(400, "invalid_offset", "Invalid Upload-Offset") from exc
                if offset < 0:
                    raise RouteError(400, "invalid_offset", "Invalid Upload-Offset")
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > manager.limits.chunk_bytes:
                        raise RouteError(413, "too_large", "Chunk exceeds runner limit")
                    body.extend(chunk)
                raw = bytes(body)
            result = await manager.operate(session_id, environment_id, uid, owner, op, raw, offset)
            if op == "status":
                record = manager.records[uid]
                return Response(
                    status_code=200,
                    headers={
                        "Upload-Offset": str(record.offset),
                        "Upload-Length": str(record.size),
                        "Cache-Control": "no-store",
                    },
                )
            if op == "append":
                return Response(status_code=204, headers={"Upload-Offset": str(result["offset"])})
            if op == "abort" and result.get("aborted"):
                return Response(status_code=204)
            return JSONResponse(content=result)
        except RouteError as exc:
            return exc.response()
        except OSError as exc:
            return RouteError(507, "no_space", str(exc)).response()

    @app.patch(base + "/{upload_id}")
    async def append(
        session_id: str,
        environment_id: str,
        upload_id: str,
        request: Request,
        created_by: str = "",
    ) -> Response:
        return await operate(session_id, environment_id, upload_id, created_by, "append", request)

    @app.head(base + "/{upload_id}")
    async def status(
        session_id: str, environment_id: str, upload_id: str, created_by: str = ""
    ) -> Response:
        return await operate(session_id, environment_id, upload_id, created_by, "status")

    @app.post(base + "/{upload_id}/complete")
    async def complete(
        session_id: str, environment_id: str, upload_id: str, created_by: str = ""
    ) -> Response:
        return await operate(session_id, environment_id, upload_id, created_by, "complete")

    @app.delete(base + "/{upload_id}")
    async def abort(
        session_id: str, environment_id: str, upload_id: str, created_by: str = ""
    ) -> Response:
        return await operate(session_id, environment_id, upload_id, created_by, "abort")

    return manager
