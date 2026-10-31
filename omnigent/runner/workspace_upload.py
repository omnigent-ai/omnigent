"""Dormant runner uploads with an ownership-bound staging index.

HELPER_CACHE_MAX bounds helpers across sessions and environments. Open uploads pin
helpers until drained or max_age (24 hours); HEAD refreshes idle expiry only.
When capacity is fully pinned, new environments receive 429 with Retry-After.

A lost begin response can leave an empty .part staging file without a recorded
allocation identity. It stays hidden from listings and is never moved, adopted
or deleted automatically; only its intent record expires after max_age.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import json
import logging
import os
import random
import re
import shutil
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TypeVar, cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator

from omnigent.debug_logging import runner_primary_session_id
from omnigent.entities.environment_filesystem import InvalidPath, ResourceError
from omnigent.inner import os_env_upload
from omnigent.inner.os_env import CallerProcessOSEnvironment, OSEnvironment
from omnigent.inner.os_env_upload import (
    LONG_OPS,
    SHORT_OP_QUEUE_WAIT_S,
    UploadHelperClient,
    upload_support_reason,
    valid_name,
)
from omnigent.install_ledger import state_dir
from omnigent.runner.environment_filesystem import CallerProcessFilesystem, _validate_path
from omnigent.runtime.filesystem_registry import FilesystemRegistry

WORKSPACE_UPLOAD_CAPABILITY = "workspace_upload_v1"
logger = logging.getLogger(__name__)
HELPER_CACHE_MAX = 32
HELPER_IDLE_S = 300
STARTUP_SWEEP_BUDGET_S = 0.5
GONE_MAX = 4096
COMPLETED_RECEIPTS_MAX = 256
PUBLICATION_ATTEMPTS = 3
T = TypeVar("T")
_sweep_active = ContextVar("upload_sweep_active", default=False)


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
    staging_dev: int = 0
    staging_ino: int = 0
    parent_dev: int
    parent_ino: int
    pending: bool = False
    finalizing: bool = False
    publication: dict[str, object] | None = None
    result: dict[str, object] | None = None
    snapshot: str | None = None


class UploadDeclaration(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    dir: str
    name: str
    size: int = Field(ge=0)
    on_conflict: str = "fail"
    sha256: str | None = None
    created_by: str = Field(min_length=1)

    @field_validator("dir", "name")
    @classmethod
    def path_text(cls, value: str, info: ValidationInfo) -> str:
        if re.search(r"[\x00-\x1f\x7f]", value):
            code = "invalid_name" if info.field_name == "name" else "invalid_path"
            raise RouteError(400, code, "Upload path contains control characters")
        return value

    @field_validator("dir", "created_by")
    @classmethod
    def bounded_text(cls, value: str, info: ValidationInfo) -> str:
        limit = 4096 if info.field_name == "dir" else 256
        if len(value.encode("utf-8")) > limit:
            raise ValueError("Upload field exceeds byte limit")
        return value


class RouteError(Exception):
    def __init__(self, status: int, code: str, message: str, offset: object = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.offset = offset

    def response(self) -> JSONResponse:
        headers = {"Upload-Offset": str(self.offset)} if self.offset is not None else {}
        if self.status == 429:
            headers["Retry-After"] = str(random.randint(1, 5)) if self.code == "busy" else "60"
        return JSONResponse(
            status_code=self.status,
            content={"error": {"code": self.code, "message": str(self)}},
            headers=headers,
        )


class EnvironmentResolutionError(RouteError):
    """The environment resolver failed before any helper operation."""


def _unwrap(result: dict[str, object]) -> dict[str, object]:
    if "error" not in result:
        return result
    code = str(result.get("code", "upload_failed"))
    status = {
        "file_exists": 409,
        "is_directory": 409,
        "offset_mismatch": 409,
        "incomplete": 409,
        "plan_changed": 409,
        "checksum_mismatch": 409,
        "too_large": 413,
        "gone": 410,
        "parent_gone": 410,
        "staging_gone": 410,
        "no_space": 507,
        "timeout": 504,
        "busy": 429,
        "io_error": 500,
        "permission_denied": 403,
        "invalid_name": 400,
        "invalid_path": 400,
        "invalid_request": 400,
    }.get(code, 500)
    raise RouteError(status, code, str(result["error"]), result.get("offset"))


def os_error(exc: OSError) -> RouteError:
    logger.error("Upload I/O failed", exc_info=exc)
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return RouteError(507, "no_space", "Insufficient storage")
    if isinstance(exc, FileNotFoundError):
        return RouteError(400, "invalid_path", "Invalid upload path")
    if isinstance(exc, PermissionError):
        return RouteError(403, "permission_denied", "Upload permission denied")
    return RouteError(500, "io_error", "Upload I/O failed")


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
    if upload_support_reason():
        return False
    client = UploadHelperClient(env)
    try:
        result = await asyncio.to_thread(client.call, {"op": "probe"})
        return result.get("raw_upload") is True
    finally:
        await asyncio.to_thread(client.close)


@dataclass
class HelperBinding:
    env: CallerProcessOSEnvironment
    client: UploadHelperClient
    cwd: Path
    policy: str
    used: float
    users: int = 0
    gate: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_op: object = None


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
        self.helpers: dict[tuple[str, str], HelperBinding] = {}
        self.lock = asyncio.Lock()
        self.index_dirty = False
        self.helper_lock = asyncio.Lock()
        self.ipc_executor = ThreadPoolExecutor(
            max_workers=HELPER_CACHE_MAX, thread_name_prefix="workspace-upload-ipc"
        )
        self.upload_locks: dict[str, asyncio.Lock] = {}
        self.inflight: set[asyncio.Task[object]] = set()
        self.stopping = False
        self.task: asyncio.Task[None] | None = None
        self.gone: OrderedDict[str, tuple[tuple[str, str, str], float]] = OrderedDict()
        self.restored: set[str] = set()
        if state_path.exists():
            try:
                values = json.loads(state_path.read_text())
                for uid, value in values.items():
                    self.records[uid] = UploadRecord.model_validate(value)
            except (OSError, ValueError, AttributeError, TypeError):
                logger.exception("Unreadable upload index; starting with an empty index")
                self.records.clear()
                try:
                    state_path.rename(
                        state_path.with_name(f"index.corrupt-{uuid.uuid4().hex}.json")
                    )
                except OSError:
                    logger.exception("Unable to move unreadable upload index aside")
        self.restored = set(self.records)
        if self._prune_completed_receipts():
            self.index_dirty = True

    def _pinned(self, key: tuple[str, str]) -> bool:
        return any(
            (record.session_id, record.environment_id) == key and record.result is None
            for record in self.records.values()
        )

    async def _evict_helpers(self) -> None:
        for key, binding in list(self.helpers.items()):
            if binding.users or binding.client._lock.locked():
                continue
            if binding.env._helper._closed or (
                not self._pinned(key) and time.monotonic() - binding.used >= HELPER_IDLE_S
            ):
                await asyncio.to_thread(binding.client.close)
                del self.helpers[key]

    async def _helper(
        self, session_id: str, environment_id: str, *, reserve: bool = False
    ) -> UploadHelperClient:
        try:
            env = await self.resolve_environment(session_id, environment_id)
        except Exception as exc:
            if not _sweep_active.get():
                raise
            raise EnvironmentResolutionError(
                503, "environment_unavailable", "Upload environment cannot be resolved"
            ) from exc
        if not isinstance(env, CallerProcessOSEnvironment) or env._helper._closed:
            raise RouteError(
                503, "runner_capability_mismatch", "Environment lacks raw upload support"
            )
        reason = upload_support_reason()
        if reason:
            raise RouteError(503, "runner_capability_mismatch", reason)
        key = session_id, environment_id
        cwd = env.cwd.resolve()
        policy = json.dumps(env.sandbox.to_jsonable(), sort_keys=True)
        async with self.helper_lock:
            binding = self.helpers.get(key)
            if binding and (
                binding.env is not env
                or binding.cwd != cwd
                or binding.policy != policy
                or binding.client._closed
            ):
                if binding.users:
                    raise RouteError(429, "busy", "Retired upload helper is still in use")
                await asyncio.to_thread(binding.client.close)
                del self.helpers[key]
            await self._evict_helpers()
            if key not in self.helpers:
                if len(self.helpers) >= HELPER_CACHE_MAX:
                    idle = [
                        (k, v)
                        for k, v in self.helpers.items()
                        if not self._pinned(k) and not v.users and not v.client._lock.locked()
                    ]
                    if not idle:
                        raise RouteError(429, "busy", "Upload helper cache is busy")
                    oldest, old = min(idle, key=lambda item: item[1].used)
                    await asyncio.to_thread(old.client.close)
                    del self.helpers[oldest]
                self.helpers[key] = HelperBinding(
                    env, UploadHelperClient(env), cwd, policy, time.monotonic()
                )
            binding = self.helpers[key]
            binding.used = time.monotonic()
            if reserve:
                binding.users += 1
            return binding.client

    @contextlib.asynccontextmanager
    async def _using_helper(
        self, session_id: str, environment_id: str
    ) -> AsyncIterator[UploadHelperClient]:
        helper = await self._helper(session_id, environment_id, reserve=True)
        binding = self.helpers[(session_id, environment_id)]
        try:
            yield helper
        finally:
            binding.users -= 1

    async def _call(
        self, uid: str, record: UploadRecord, op: str, raw: bytes = b"", **fields: object
    ) -> dict[str, object]:
        async with self._using_helper(record.session_id, record.environment_id):
            binding = self.helpers[(record.session_id, record.environment_id)]
            metadata = {"op": op, "id": uid, "size": record.size, **fields}
            result = await self._helper_rpc(binding, metadata, raw)
            if result.get("code") == "gone" and op in ("append", "status", "abort", "plan"):
                _unwrap(
                    await self._helper_rpc(
                        binding,
                        {
                            "op": "resume",
                            "id": uid,
                            **record.model_dump(exclude={"snapshot", "staging_path"}),
                        },
                    )
                )
                result = await self._helper_rpc(binding, metadata, raw)
            return _unwrap(result)

    async def _helper_rpc(
        self, binding: HelperBinding, metadata: dict[str, object], raw: bytes = b""
    ) -> dict[str, object]:
        """Queue without executor threads and hold admission until IPC settles."""
        deadline = time.monotonic() + os_env_upload.operation_timeout(metadata)
        queue_deadline = deadline
        while True:
            now = time.monotonic()
            if (
                binding.gate.locked()
                and metadata.get("op") not in LONG_OPS
                and binding.active_op in LONG_OPS
            ):
                queue_deadline = min(queue_deadline, now + SHORT_OP_QUEUE_WAIT_S)
            else:
                queue_deadline = deadline
            remaining = queue_deadline - now
            if remaining <= 0:
                return {"code": "busy", "error": "Upload helper queue timed out"}
            try:
                async with asyncio.timeout(min(remaining, 0.05)):
                    await binding.gate.acquire()
                break
            except TimeoutError:
                continue
        binding.active_op = metadata.get("op")
        try:
            task = asyncio.get_running_loop().run_in_executor(
                self.ipc_executor,
                copy_context().run,
                partial(binding.client.call, metadata, raw, deadline=deadline),
            )
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                try:
                    await self._settle(task)
                except Exception:
                    logger.exception("Upload helper operation failed during cancellation")
                raise
        finally:
            binding.active_op = None
            binding.gate.release()

    def _save(self) -> None:
        self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.state_path.with_name(f".index-{uuid.uuid4().hex}")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                values = {
                    uid: record.model_dump(
                        exclude={"snapshot"} if record.result is not None else set()
                    )
                    for uid, record in self.records.items()
                }
                json.dump(values, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
            parent = os.open(self.state_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            temporary.unlink(missing_ok=True)

    async def _save_settled(self) -> None:
        # The index lock must outlive its save thread, even during shutdown.
        self._prune_completed_receipts()
        self.index_dirty = True
        task = asyncio.create_task(asyncio.to_thread(self._save))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await self._settle(task)
            except Exception:
                logger.exception("Unable to save upload index during cancellation")
            else:
                self.index_dirty = False
            raise
        self.index_dirty = False

    async def _persist(self) -> None:
        async with self.lock:
            await self._save_settled()

    def _expired(self, record: UploadRecord, now: float) -> bool:
        return (
            now - record.last_activity >= self.limits.idle_ttl_s
            or now - record.created_at >= self.limits.max_age_s
        )

    def _prune_gone(self) -> None:
        instant = time.monotonic()
        for uid, (_, deadline) in list(self.gone.items()):
            if instant >= deadline:
                del self.gone[uid]
        while len(self.gone) > GONE_MAX:
            self.gone.popitem(last=False)

    def _drop(self, uid: str, record: UploadRecord) -> None:
        self.gone[uid] = (
            (record.session_id, record.environment_id, record.created_by),
            time.monotonic() + self.limits.max_age_s,
        )
        self._prune_gone()
        self.records.pop(uid, None)
        self.restored.discard(uid)
        self.upload_locks.pop(uid, None)

    def _prune_completed_receipts(self) -> bool:
        completed = sorted(
            (
                (record.last_activity, uid, record)
                for uid, record in self.records.items()
                if record.result is not None
            ),
            key=lambda item: item[0],
        )
        excess = len(completed) - COMPLETED_RECEIPTS_MAX
        for _, uid, record in completed[: max(0, excess)]:
            self._drop(uid, record)
        return excess > 0

    async def _remove(self, uid: str, record: UploadRecord) -> None:
        try:
            if record.result is not None:
                await self._call(uid, record, "forget")
            elif record.staging_dev and record.staging_ino:
                await self._call(
                    uid,
                    record,
                    "cleanup",
                    **record.model_dump(exclude={"snapshot", "staging_path"}),
                )
        except RouteError as exc:
            if exc.code not in ("parent_gone", "staging_gone"):
                raise
        async with self.lock:
            self._drop(uid, record)
            await self._save_settled()

    async def _recover(self, uid: str, record: UploadRecord) -> None:
        try:
            record.result = await self._call(
                uid, record, "recover", **record.model_dump(exclude={"snapshot", "staging_path"})
            )
        except RouteError as exc:
            if exc.code in ("parent_gone", "staging_gone"):
                async with self.lock:
                    self._drop(uid, record)
                    await self._save_settled()
            raise
        record.offset = record.size
        record.finalizing = False
        self.restored.discard(uid)
        try:
            registry = await self.resolve_registry(record.session_id)
            self._track_change(record, registry)
        finally:
            record.snapshot = None
        await self._persist()

    async def _reconcile(
        self, uid: str, record: UploadRecord, *, now: float | None = None
    ) -> bool:
        if record.result is None and (not record.staging_dev or not record.staging_ino):
            logger.warning(
                "Leaving upload intent %s untouched: allocation identity is unknown", uid
            )
            instant = _now() if now is None else now
            if instant - record.created_at >= self.limits.max_age_s:
                await self._remove(uid, record)
            return False
        if record.finalizing and record.result is None:
            try:
                await self._recover(uid, record)
            except RouteError as exc:
                if exc.code in ("parent_gone", "staging_gone"):
                    return False
                if exc.code != "gone":
                    raise
        if uid in self.restored:
            if record.result is None:
                await self._remove(uid, record)
                return False
            self.restored.discard(uid)
        return True

    @staticmethod
    def _track_change(record: UploadRecord, registry: FilesystemRegistry | None) -> None:
        try:
            if registry is None or record.result is None:
                return
            path = cast(str, record.result["path"])
            if record.snapshot is not None and record.result["outcome"] == "replaced":
                registry.seed_snapshot(path, record.snapshot, session_id=record.session_id)
            registry.record_change(path, "write", record.session_id)
        finally:
            record.snapshot = None

    async def sweep(self, *, now: float | None = None) -> None:
        async with self.lock:
            if self.index_dirty:
                try:
                    await self._save_settled()
                except OSError:
                    logger.exception("Unable to retry upload index save")
        self._prune_gone()
        instant = _now() if now is None else now
        for uid in list(self.records):
            if uid not in self.records:
                continue
            lock = self.upload_locks.setdefault(uid, asyncio.Lock())
            async with lock:
                record = self.records.get(uid)
                if record is None:
                    continue
                token = _sweep_active.set(True)
                try:
                    if not await self._reconcile(uid, record, now=instant):
                        continue
                    if not self._expired(record, instant):
                        continue
                    await self._remove(uid, record)
                except EnvironmentResolutionError:
                    if instant - record.created_at >= self.limits.max_age_s:
                        logger.warning(
                            "Retiring expired upload %s: environment unavailable",
                            uid,
                            exc_info=True,
                        )
                        async with self.lock:
                            self._drop(uid, record)
                            await self._save_settled()
                except Exception as exc:
                    logger.exception("Upload cleanup failed for %s", uid)
                    if isinstance(exc, RouteError) and exc.code in (
                        "unverified_intent",
                        "parent_gone",
                        "staging_gone",
                    ):
                        async with self.lock:
                            self._drop(uid, record)
                            await self._save_settled()
                finally:
                    _sweep_active.reset(token)
        async with self.helper_lock:
            await self._evict_helpers()

    async def start(self) -> None:
        if self.task is not None:
            return
        self.stopping = False
        ready = asyncio.Event()
        self.task = asyncio.create_task(self._sweep_loop(ready), name="workspace-upload-sweep")
        try:
            await asyncio.wait_for(ready.wait(), STARTUP_SWEEP_BUDGET_S)
        except TimeoutError:
            logger.warning("Upload startup sweep continues in the background")

    async def _sweep_loop(self, ready: asyncio.Event) -> None:
        try:
            await self.sweep()
        except Exception:
            logger.exception("Upload startup sweep failed; retrying on the next interval")
        finally:
            ready.set()
        while True:
            await asyncio.sleep(min(3600, HELPER_IDLE_S))
            try:
                await self.sweep()
            except Exception:
                logger.exception("Upload sweep failed; retrying on the next interval")

    async def stop(self) -> None:
        self.stopping = True
        task = asyncio.create_task(self._stop())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await self._settle(task)
            raise

    async def _stop(self) -> None:
        try:
            if self.task is not None:
                self.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.task
                self.task = None
            await asyncio.gather(
                *(asyncio.shield(task) for task in list(self.inflight)), return_exceptions=True
            )
        finally:
            self.ipc_executor.shutdown(wait=False, cancel_futures=True)
            await asyncio.gather(
                *(asyncio.to_thread(binding.client.close) for binding in self.helpers.values()),
                return_exceptions=True,
            )
            self.helpers.clear()

    async def _settle(self, task: asyncio.Future[T]) -> T:
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.cancelled():
                    raise

    async def create(
        self, session_id: str, environment_id: str, declaration: UploadDeclaration
    ) -> dict[str, object]:
        if self.stopping:
            raise RouteError(503, "runner_stopping", "Runner is shutting down")
        request = asyncio.current_task()
        assert request is not None
        task = asyncio.create_task(self._create(session_id, environment_id, declaration))
        self.inflight.add(request)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                result = await self._settle(task)
            except (RouteError, OSError):
                pass
            else:
                uid = cast(str, result["upload_id"])
                cleanup = asyncio.create_task(self._abort_creation(uid))
                await self._settle(cleanup)
            raise
        finally:
            self.inflight.discard(request)

    async def _abort_creation(self, uid: str) -> None:
        async with self.upload_locks.setdefault(uid, asyncio.Lock()):
            record = self.records.get(uid)
            if record is not None:
                await self._remove(uid, record)

    async def _create(
        self, session_id: str, environment_id: str, declaration: UploadDeclaration
    ) -> dict[str, object]:
        if not valid_name(declaration.name):
            raise RouteError(400, "invalid_name", "Invalid basename")
        if declaration.dir.startswith("/") or "\\" in declaration.dir or "\0" in declaration.dir:
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
        async with self._using_helper(session_id, environment_id) as helper:
            binding = self.helpers[(session_id, environment_id)]
            uid = uuid.uuid4().hex
            prepared = _unwrap(
                await self._helper_rpc(
                    binding,
                    {"op": "prepare", "id": uid, **declaration.model_dump(), "dir": directory},
                )
            )
            try:
                free = await asyncio.to_thread(shutil.disk_usage, helper.cwd / directory)
            except OSError as exc:
                if exc.errno in (errno.ENAMETOOLONG, errno.ELOOP):
                    raise RouteError(400, "invalid_path", "Invalid upload path") from exc
                raise
            if declaration.size > free.free:
                raise RouteError(507, "no_space", "Insufficient free disk space")
            now = _now()
            record = UploadRecord.model_validate(
                {
                    **declaration.model_dump(),
                    **prepared,
                    "dir": directory,
                    "session_id": session_id,
                    "environment_id": environment_id,
                    "created_at": now,
                    "last_activity": now,
                    "pending": True,
                }
            )
            creation_lock = asyncio.Lock()
            async with creation_lock:
                async with self.lock:
                    opened = [
                        r
                        for r in self.records.values()
                        if r.session_id == session_id and r.result is None
                    ]
                    if (
                        len(opened) >= self.limits.max_open_uploads
                        or sum(r.size for r in opened) + declaration.size
                        > self.limits.max_open_bytes
                    ):
                        raise RouteError(429, "upload_limit", "Session upload limit reached")
                    self.upload_locks[uid] = creation_lock
                    self.records[uid] = record
                    try:
                        await self._save_settled()
                    except OSError as exc:
                        logger.exception("Unable to save new upload intent")
                        # Begin has not run, so there is no allocation to preserve.
                        del self.records[uid]
                        del self.upload_locks[uid]
                        try:
                            await self._save_settled()
                        except OSError:
                            logger.exception("Unable to persist upload intent rollback")
                        message = "Upload index could not be saved"
                        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                            raise RouteError(507, "no_space", message) from exc
                        raise RouteError(500, "io_error", message) from exc
                try:
                    result = await self._call(
                        uid,
                        record,
                        "begin",
                        dir=directory,
                        name=declaration.name,
                        size=declaration.size,
                        on_conflict=declaration.on_conflict,
                        sha256=declaration.sha256,
                        parent_dev=record.parent_dev,
                        parent_ino=record.parent_ino,
                    )
                except RouteError as exc:
                    if exc.code not in ("gone", "timeout"):
                        async with self.lock:
                            self._drop(uid, record)
                            await self._save_settled()
                    raise
                async with self.lock:
                    record.staging_dev = cast(int, result["staging_dev"])
                    record.staging_ino = cast(int, result["staging_ino"])
                    record.pending = False
                    await self._save_settled()
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
        if self.stopping:
            raise RouteError(503, "runner_stopping", "Runner is shutting down")
        request = asyncio.current_task()
        assert request is not None
        task = asyncio.create_task(
            self._operate(session_id, environment_id, uid, owner, op, raw, offset)
        )
        self.inflight.add(request)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with contextlib.suppress(RouteError, OSError):
                await self._settle(task)
            raise
        finally:
            self.inflight.discard(request)

    async def _operate(
        self,
        session_id: str,
        environment_id: str,
        uid: str,
        owner: str,
        op: str,
        raw: bytes,
        offset: int,
    ) -> dict[str, object]:
        lock = (
            self.upload_locks.setdefault(uid, asyncio.Lock())
            if uid in self.records
            else asyncio.Lock()
        )
        async with lock:
            self._prune_gone()
            record = self.records.get(uid)
            if record is None:
                expired = self.gone.get(uid)
                if expired and expired[0] != (session_id, environment_id, owner):
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
            if not await self._reconcile(uid, record):
                raise RouteError(410, "gone", "Upload expired during reconciliation")
            if self._expired(record, _now()):
                await self._remove(uid, record)
                raise RouteError(410, "gone", "Upload expired")
            if record.result is not None:
                if op == "append":
                    raise RouteError(409, "already_complete", "Upload already committed")
                record.offset = record.size
                if op == "status":
                    return {"offset": record.offset, "size": record.size}
                return record.result
            if op == "complete":
                result = await self._finalize(uid, record)
            else:
                result = await self._call(uid, record, op, raw, offset=offset)
            if op == "abort":
                async with self.lock:
                    self._drop(uid, record)
                await self._persist()
            else:
                record.last_activity = _now()
                if "offset" in result:
                    record.offset = cast(int, result["offset"])
            if op == "status":
                return {"offset": record.offset, "size": record.size}
            return result

    async def _finalize(self, uid: str, record: UploadRecord) -> dict[str, object]:
        registry = await self.resolve_registry(record.session_id)
        for _ in range(PUBLICATION_ATTEMPTS):
            publication = await self._call(uid, record, "plan")
            record.snapshot = None
            if registry is not None and publication["outcome"] == "replaced":
                env = await self.resolve_environment(record.session_id, record.environment_id)
                try:
                    existing = await CallerProcessFilesystem(env).read(
                        cast(str, publication["path"])
                    )
                    if existing.encoding:
                        record.snapshot = existing.data.decode(existing.encoding, errors="replace")
                except (ResourceError, OSError, UnicodeError, ValueError, RuntimeError):
                    pass
            record.publication = publication
            record.finalizing = True
            record.last_activity = _now()
            await self._persist()
            try:
                result = await self._call(uid, record, "complete", publication=publication)
            except RouteError as exc:
                if exc.code == "plan_changed":
                    continue
                if exc.code in ("gone", "timeout"):
                    try:
                        await self._recover(uid, record)
                    except RouteError as recovery_error:
                        if recovery_error.code != "gone":
                            raise
                        raise exc from recovery_error
                    assert record.result is not None
                    return record.result
                raise
            record.result = result
            record.offset = record.size
            record.finalizing = False
            record.last_activity = _now()
            self._track_change(record, registry)
            await self._persist()
            return result
        raise RouteError(409, "file_exists", "Destination keeps changing")


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
    base = "/v1/sessions/{session_id}/resources/environments/{environment_id}/uploads"

    @app.get(base)
    async def capabilities(session_id: str, environment_id: str) -> JSONResponse:
        env = await resolve_environment(session_id, environment_id)
        supported = await workspace_upload_supported(env, routes_registered=True)
        return JSONResponse(
            content={
                "supported": supported,
                "reason": upload_support_reason() if not supported else None,
                "limits": manager.limits.public(),
            }
        )

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
            return os_error(exc).response()

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
                return Response(
                    status_code=200,
                    headers={
                        "Upload-Offset": str(result["offset"]),
                        "Upload-Length": str(result["size"]),
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
            return os_error(exc).response()

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
