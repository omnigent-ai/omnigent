"""Bounded, authenticated workspace upload relays to the session's runner."""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import io
import itertools
import ntpath
import random
import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnigent.entities import Conversation
from omnigent.entities.environment_filesystem import InvalidPath
from omnigent.errors import ErrorCode, OmnigentError

# Reuse the helper's validation and finalize budget so both boundaries agree.
from omnigent.inner.os_env_upload import operation_timeout, valid_name
from omnigent.runner.environment_filesystem import _validate_path
from omnigent.runner.routing import RunnerRouter
from omnigent.runner.workspace_upload import WORKSPACE_UPLOAD_CAPABILITY, UploadLimits
from omnigent.server import server_config as config
from omnigent.server.auth import LEVEL_EDIT, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.feature_flags import Feature
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.server.routes._content_type import _request_media_type
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.server.routes._sessions.common import get_server_runner_router
from omnigent.server.routes._sessions.helpers import (
    _get_runner_client_for_resource_access,
    _publish_changed_files_invalidated,
)
from omnigent.server.routes._sessions.orchestration import ensure_runner_connected
from omnigent.stores import ConversationStore

CREATE_BODY_MAX_BYTES = 32 * 1024
INFLIGHT_CHUNK_MAX_BYTES = 64 * 1024**2
FINALIZE_RELAY_MARGIN_S = 5
EXPIRY_CLEANUP_LIMIT = 64


class UploadDeclaration(BaseModel):
    """Public declaration; caller identity is supplied by the server."""

    model_config = ConfigDict(strict=True, extra="forbid")
    dir: str
    name: str
    size: int = Field(ge=0)
    on_conflict: Literal["fail", "replace", "rename"] = "fail"
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ChunkLimits(BaseModel):
    min: int
    initial: int
    max: int


class PublicLimits(BaseModel):
    max_file_bytes: int
    chunk: ChunkLimits
    max_files_per_batch: int = 100
    max_batch_bytes: int = 5 * 1024**3
    max_open_uploads: int
    max_open_bytes: int
    parallel_files: int = 2


class UploadCapability(BaseModel):
    supported: bool
    reason: str | None = None
    limits: PublicLimits


class CreatedUpload(BaseModel):
    upload_id: str
    offset: int
    chunk_bytes: int
    expires_at: float


class CompletedUpload(BaseModel):
    path: str
    size: int
    sha256: str
    outcome: Literal["created", "replaced", "renamed"]
    final_name: str


_MESSAGES = {
    ErrorCode.INVALID_NAME: "Invalid upload basename",
    ErrorCode.INVALID_PATH: "Destination must be inside the workspace",
    ErrorCode.INVALID_REQUEST: "Invalid upload declaration",
    ErrorCode.IDEMPOTENCY_KEY_CONFLICT: "Upload declaration changed for this key",
    ErrorCode.INVALID_OFFSET: "Upload-Offset must be a nonnegative integer",
    ErrorCode.FILE_EXISTS: "A file already exists at the destination",
    ErrorCode.IS_DIRECTORY: "The destination is a directory",
    ErrorCode.OFFSET_MISMATCH: "Upload offset differs; check the current offset before retrying",
    ErrorCode.INCOMPLETE: "Upload is incomplete",
    ErrorCode.CHECKSUM_MISMATCH: "Upload checksum differs",
    ErrorCode.ALREADY_COMPLETE: "Upload already committed",
    ErrorCode.PLAN_CHANGED: "Upload destination changed; retry completion",
    ErrorCode.TOO_LARGE: "Upload exceeds the allowed size",
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: "Raw upload content type required",
    ErrorCode.GONE: "Upload expired or was aborted",
    ErrorCode.PARENT_GONE: "Upload destination no longer exists",
    ErrorCode.STAGING_GONE: "Upload staging no longer exists",
    ErrorCode.NOT_FOUND: "Unknown upload",
    ErrorCode.PERMISSION_DENIED: "Upload permission denied",
    ErrorCode.UPLOAD_OWNER_MISMATCH: "Upload belongs to another caller",
    ErrorCode.BUSY: "Upload capacity is busy; retry shortly",
    ErrorCode.UPLOAD_LIMIT: "Session upload limit reached",
    ErrorCode.NO_SPACE: "Insufficient storage for upload",
    ErrorCode.RUNNER_CAPABILITY_MISMATCH: (
        "Update the host and restart the session to upload files"
    ),
    ErrorCode.RUNNER_UNAVAILABLE: "Runner unavailable",
    ErrorCode.UPLOAD_RELAY_FAILED: "Upload relay failed; check the offset before retrying",
    ErrorCode.UPLOAD_TIMEOUT: "Upload deadline exceeded; check the offset or retry completion",
}


class UploadError(OmnigentError):
    """Typed upload rejection with protocol headers."""

    def __init__(self, code: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(_MESSAGES[code], code=code)
        self.headers = {"Cache-Control": "no-store", **(headers or {})}


class UploadRoute(APIRoute):
    """Preserve offset and retry headers on typed upload errors."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def with_upload_headers(request: Request) -> Response:
            try:
                return await handler(request)
            except UploadError as exc:
                return JSONResponse(
                    {"error": {"code": exc.code, "message": exc.message}},
                    status_code=exc.http_status,
                    headers=exc.headers,
                )

        return with_upload_headers


class ByteAdmission:
    """Nonwaiting, process-wide weighted semaphore, safe across request loops."""

    def __init__(self) -> None:
        self.used = 0
        self.lock = threading.Lock()

    def acquire(self, size: int) -> None:
        with self.lock:
            if self.used + size > INFLIGHT_CHUNK_MAX_BYTES:
                raise UploadError(ErrorCode.BUSY, _retry_headers())
            self.used += size

    def release(self, size: int) -> None:
        with self.lock:
            self.used -= size


_chunk_admission = ByteAdmission()


def _retry_headers() -> dict[str, str]:
    return {"Retry-After": str(random.randint(1, 5))}


@dataclass
class UploadRecord:
    owner: str
    size: int
    limits: PublicLimits
    created_at: float
    last_activity: float
    idle_ttl_s: int
    max_age_s: int
    runner_id: str | None
    finished: bool = False
    aborted: bool = False
    idempotency_key: str | None = None

    def expired(self, now: float) -> bool:
        return (
            now - self.created_at >= self.max_age_s or now - self.last_activity >= self.idle_ttl_s
        )


UploadKey = tuple[str, str, str]


@dataclass
class SessionAdmission:
    keys: set[UploadKey] = field(default_factory=set)
    size: int = 0


@dataclass
class UploadIndex:
    # The index belongs to one application's router, never another workspace.
    records: dict[UploadKey, UploadRecord] = field(default_factory=dict)
    idempotency_keys: dict[tuple[str, str, str, str], tuple[str, str]] = field(
        default_factory=dict
    )
    pending_creates: dict[
        tuple[str, str, str, str], tuple[str, Future[CreatedUpload | UploadError]]
    ] = field(default_factory=dict)
    sessions: dict[str, SessionAdmission] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    expiry_lock: threading.Lock = field(default_factory=threading.Lock)
    expirations: list[tuple[float, int, UploadKey, UploadRecord]] = field(default_factory=list)
    sequence: itertools.count = field(default_factory=itertools.count)

    def schedule(self, key: UploadKey, record: UploadRecord) -> None:
        deadline = min(
            record.created_at + record.max_age_s, record.last_activity + record.idle_ttl_s
        )
        with self.expiry_lock:
            heapq.heappush(self.expirations, (deadline, next(self.sequence), key, record))

    def add(self, key: UploadKey, record: UploadRecord) -> None:
        self.remove(key)
        self.records[key] = record
        if not record.finished:
            admission = self.sessions.setdefault(key[0], SessionAdmission())
            admission.keys.add(key)
            admission.size += record.size
        self.schedule(key, record)

    def remove(self, key: UploadKey) -> None:
        record = self.records.pop(key, None)
        if record is not None and record.idempotency_key is not None:
            scope = (*key[:2], record.owner, record.idempotency_key)
            if self.idempotency_keys.get(scope, (None,))[0] == key[2]:
                self.idempotency_keys.pop(scope, None)
        if record is not None and not record.finished:
            self.release(key, record)

    def release(self, key: UploadKey, record: UploadRecord) -> None:
        admission = self.sessions.get(key[0])
        if admission is None or key not in admission.keys:
            return
        admission.keys.remove(key)
        admission.size -= record.size
        if not admission.keys:
            del self.sessions[key[0]]

    def has_capacity(
        self, session_id: str, size: int, limits: PublicLimits, replacing: UploadKey | None = None
    ) -> bool:
        admission = self.sessions.get(session_id, SessionAdmission())
        count = len(admission.keys)
        used = admission.size
        if replacing is not None and replacing in admission.keys:
            count -= 1
            used -= self.records[replacing].size
        return count < limits.max_open_uploads and used + size <= limits.max_open_bytes

    def finish(self, key: UploadKey, record: UploadRecord, *, aborted: bool = False) -> bool:
        if record.finished:
            return False
        self.release(key, record)
        if record.idempotency_key is not None and aborted:
            self.idempotency_keys.pop((*key[:2], record.owner, record.idempotency_key), None)
        record.finished = True
        record.aborted = aborted
        return True

    def prune(self, session_id: str | None = None) -> None:
        now = time.monotonic()
        for _ in range(EXPIRY_CLEANUP_LIMIT):
            with self.expiry_lock:
                if not self.expirations or self.expirations[0][0] > now:
                    break
                _, _, key, record = heapq.heappop(self.expirations)
            with self.lock:
                if self.records.get(key) is not record:
                    continue
                if record.expired(now):
                    self.remove(key)
                    continue
                deadline_record = record
            self.schedule(key, deadline_record)
        if session_id is not None:
            with self.lock:
                admission = self.sessions.get(session_id)
                keys = tuple(admission.keys) if admission is not None else ()
            for key in keys:
                with self.lock:
                    record = self.records.get(key)
                    if record is not None and record.expired(now):
                        self.remove(key)


def _limits(runner: dict[str, Any] | None = None) -> PublicLimits:
    try:
        hard_fields = dict(runner if runner is not None else UploadLimits().public())
        hard_fields.setdefault(
            "max_files_per_batch", config.workspace_upload_max_files_per_batch()
        )
        hard_fields.setdefault("max_batch_bytes", config.workspace_upload_max_batch_bytes())
        hard_fields.setdefault("parallel_files", config.workspace_upload_parallel_files())
        hard = PublicLimits.model_validate(hard_fields)
        chunk = hard.chunk
        maximum = min(config.workspace_upload_chunk_max_bytes(), chunk.max)
        minimum = min(256 * 1024, chunk.min, maximum)
        return PublicLimits(
            max_file_bytes=min(config.workspace_upload_max_file_bytes(), hard.max_file_bytes),
            chunk=ChunkLimits(
                min=minimum,
                initial=max(
                    minimum,
                    min(
                        config.workspace_upload_chunk_initial_bytes(),
                        chunk.initial,
                        maximum,
                    ),
                ),
                max=maximum,
            ),
            max_files_per_batch=min(
                config.workspace_upload_max_files_per_batch(),
                hard.max_files_per_batch,
            ),
            max_batch_bytes=min(
                config.workspace_upload_max_batch_bytes(),
                hard.max_batch_bytes,
            ),
            max_open_uploads=min(
                config.workspace_upload_max_open_per_session(), hard.max_open_uploads
            ),
            max_open_bytes=min(
                config.workspace_upload_max_open_bytes_per_session(), hard.max_open_bytes
            ),
            parallel_files=min(config.workspace_upload_parallel_files(), hard.parallel_files),
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise UploadError(ErrorCode.UPLOAD_RELAY_FAILED) from exc


async def _read_capped(request: Request, maximum: int) -> bytes:
    raw_length = request.headers.get("content-length", "")
    content_length: int | None = None
    if re.fullmatch(r"[0-9]+", raw_length):
        normalized_length = raw_length.lstrip("0") or "0"
        maximum_text = str(maximum)
        if len(normalized_length) > len(maximum_text) or (
            len(normalized_length) == len(maximum_text) and normalized_length > maximum_text
        ):
            raise UploadError(ErrorCode.TOO_LARGE)
        content_length = int(normalized_length)

    if content_length is not None:
        first: bytes | None = None
        buffer: io.BytesIO | None = None
        size = 0
        async for chunk in request.stream():
            next_size = size + len(chunk)
            if next_size > maximum or next_size > content_length:
                raise UploadError(ErrorCode.TOO_LARGE)
            if not chunk:
                continue
            if buffer is None and first is None:
                first = chunk
                size = next_size
                continue
            if buffer is None:
                buffer = io.BytesIO()
                if content_length:
                    buffer.seek(content_length - 1)
                    buffer.write(b"\0")
                    buffer.seek(0)
                assert first is not None
                buffer.write(first)
                first = None
            buffer.write(chunk)
            size = next_size
        if size != content_length:
            raise UploadError(ErrorCode.UPLOAD_RELAY_FAILED)
        return buffer.getvalue() if buffer is not None else first or b""

    buffer: io.BytesIO | None = None
    size = 0
    async for chunk in request.stream():
        next_size = size + len(chunk)
        if next_size > maximum:
            raise UploadError(ErrorCode.TOO_LARGE)
        if not chunk:
            continue
        if buffer is None:
            buffer = io.BytesIO()
            if maximum:
                buffer.seek(maximum - 1)
                buffer.write(b"\0")
                buffer.seek(0)
        buffer.write(chunk)
        size = next_size
    if buffer is None:
        return b""
    buffer.truncate(size)
    return buffer.getvalue()


def _response_error(response: httpx.Response) -> UploadError:
    code = {404: ErrorCode.NOT_FOUND, 410: ErrorCode.GONE}.get(response.status_code)
    try:
        candidate = response.json()["error"]["code"]
        if (
            candidate in _MESSAGES
            and OmnigentError("", code=candidate).http_status == response.status_code
        ):
            code = candidate
        elif candidate == "runner_stopping":
            code = ErrorCode.RUNNER_UNAVAILABLE
    except (ValueError, KeyError, TypeError):
        pass
    headers: dict[str, str] = {}
    offset = response.headers.get("Upload-Offset", "")
    if re.fullmatch(r"[0-9]+", offset):
        headers["Upload-Offset"] = offset
    retry = response.headers.get("Retry-After", "")
    if response.status_code == 429:
        headers.update(
            {"Retry-After": retry} if re.fullmatch(r"[0-9]+", retry) else _retry_headers()
        )
    return UploadError(code or ErrorCode.UPLOAD_RELAY_FAILED, headers)


async def _relay(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    timeout: float,
    owner: str | None = None,
    declaration: dict[str, Any] | None = None,
    body: bytes | None = None,
    offset: int = 0,
    idempotency_key: str | None = None,
) -> httpx.Response:
    try:
        async with asyncio.timeout(timeout):
            response = await client.request(
                method,
                path,
                params={"created_by": owner} if owner is not None else None,
                json=declaration,
                content=body,
                headers={
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Offset": str(offset),
                }
                if body is not None
                else {"Idempotency-Key": idempotency_key}
                if idempotency_key is not None
                else None,
                extensions={"runner_tunnel_binary_request_body": True}
                if body is not None
                else None,
            )
    except httpx.PoolTimeout as exc:
        raise UploadError(ErrorCode.BUSY, _retry_headers()) from exc
    except httpx.LocalProtocolError as exc:
        raise UploadError(ErrorCode.TOO_LARGE) from exc
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise UploadError(ErrorCode.UPLOAD_TIMEOUT) from exc
    except (httpx.HTTPError, ConnectionError) as exc:
        raise UploadError(ErrorCode.UPLOAD_RELAY_FAILED) from exc
    if response.is_error:
        raise _response_error(response)
    return response


def _json_model(response: httpx.Response, model: type[BaseModel]) -> Any:
    try:
        return model.model_validate(response.json())
    except (ValueError, ValidationError) as exc:
        raise UploadError(ErrorCode.UPLOAD_RELAY_FAILED) from exc


def _runner_path(session_id: str, environment_id: str, suffix: str = "") -> str:
    return (
        f"/v1/sessions/{quote(session_id, safe='')}/resources/environments"
        f"/{quote(environment_id, safe='')}/uploads{suffix}"
    )


def register_upload_routes(
    router: APIRouter,
    *,
    validate_session: Callable[..., Awaitable[Conversation]],
    browse_level: Callable[..., int],
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    auth_provider: AuthProvider | None,
) -> None:
    """Register uploads using the resource router's authorization boundary."""
    uploads = APIRouter(route_class=UploadRoute)
    index = UploadIndex()
    base = "/sessions/{session_id}/resources/environments/{environment_id}/uploads"

    async def authorize(
        request: Request, session_id: str, directory: str = ""
    ) -> tuple[Conversation, str]:
        if not request.app.state.feature_flags.enabled(Feature.WORKSPACE_UPLOAD):
            raise UploadError(ErrorCode.NOT_FOUND)
        conv = await validate_session(
            session_id, request, browse_level(directory, within_workspace=LEVEL_EDIT)
        )
        owner = get_user_id(request, auth_provider) or RESERVED_USER_LOCAL
        return conv, owner

    def trusted_origin(request: Request) -> None:
        try:
            require_trusted_origin(request)
        except HTTPException as exc:
            raise OmnigentError("Trusted Origin required", code=ErrorCode.FORBIDDEN) from exc

    async def classify(
        conv: Conversation, *, allow_unavailable: bool = False
    ) -> httpx.AsyncClient | None:
        try:
            return await _get_runner_client_for_resource_access(conv.id, conversation=conv)
        except OmnigentError as exc:
            if allow_unavailable and exc.code in (
                ErrorCode.RUNNER_UNAVAILABLE,
                ErrorCode.CONFLICT,
            ):
                return None
            if exc.code in _MESSAGES:
                raise UploadError(exc.code) from exc
            if exc.code == ErrorCode.CONFLICT:
                raise UploadError(ErrorCode.RUNNER_UNAVAILABLE) from exc
            raise OmnigentError("Session runner cannot serve this request", code=exc.code) from exc

    async def runner_client(request: Request, conv: Conversation) -> tuple[httpx.AsyncClient, Any]:
        client = await classify(conv)
        routing = runner_router or get_server_runner_router()
        if client is None or routing is None:
            raise UploadError(ErrorCode.RUNNER_UNAVAILABLE)
        routed = routing.client_for_session_resources(conv.id, conversation=conv)
        registry = getattr(request.app.state, "tunnel_registry", None)
        tunnel = registry.get(routed.runner_id) if registry is not None else None
        if tunnel is None:
            raise UploadError(ErrorCode.RUNNER_UNAVAILABLE)
        if (
            client is not routed.client
            or WORKSPACE_UPLOAD_CAPABILITY not in tunnel.hello.capabilities
        ):
            raise UploadError(ErrorCode.RUNNER_CAPABILITY_MISMATCH)
        return client, tunnel

    async def probe_capability(
        client: httpx.AsyncClient, conv: Conversation, environment_id: str
    ) -> dict[str, Any]:
        response = await _relay(
            client,
            "GET",
            _runner_path(conv.id, environment_id),
            timeout=config.workspace_upload_chunk_timeout_s(),
        )
        try:
            payload = response.json()
            if not isinstance(payload["supported"], bool) or not isinstance(
                payload["limits"], dict
            ):
                raise ValueError("Invalid capability")
            return payload
        except (ValueError, KeyError, TypeError) as exc:
            raise UploadError(ErrorCode.UPLOAD_RELAY_FAILED) from exc

    async def capability(
        request: Request, conv: Conversation, environment_id: str
    ) -> tuple[httpx.AsyncClient, UploadCapability]:
        client, tunnel = await runner_client(request, conv)
        # Connection-owned futures also coalesce requests from different loops.
        key = (conv.id, environment_id)
        with index.lock:
            cache = getattr(tunnel, "workspace_upload_capabilities", None)
            if cache is None:
                cache = {}
                tunnel.workspace_upload_capabilities = cache
            future = cache.get(key)
            leader = future is None
            if leader:
                future = Future()
                cache[key] = future
        if leader:
            try:
                payload = await probe_capability(client, conv, environment_id)
            except BaseException as exc:
                error = (
                    exc
                    if isinstance(exc, OmnigentError)
                    else UploadError(ErrorCode.UPLOAD_RELAY_FAILED)
                )
                future.set_exception(error)
                with index.lock:
                    cache.pop(key, None)
                if isinstance(exc, Exception) and not isinstance(exc, OmnigentError):
                    raise error from exc
                raise
            future.set_result(payload)
        else:
            payload = await asyncio.shield(asyncio.wrap_future(future))
        supported = payload["supported"] is True
        return client, UploadCapability(
            supported=supported,
            reason=None if supported else "runner_capability_mismatch",
            limits=_limits(payload["limits"]),
        )

    @uploads.get(base, response_model=UploadCapability, response_model_exclude_none=True)
    async def get_capability(
        request: Request, session_id: str, environment_id: str
    ) -> UploadCapability:
        conv, _ = await authorize(request, session_id)
        try:
            _, result = await capability(request, conv, environment_id)
            return result
        except UploadError as exc:
            if exc.code in (ErrorCode.RUNNER_UNAVAILABLE, ErrorCode.RUNNER_CAPABILITY_MISMATCH):
                return UploadCapability(supported=False, reason=exc.code, limits=_limits())
            raise

    @uploads.post(
        base,
        status_code=201,
        response_model=CreatedUpload,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": UploadDeclaration.model_json_schema()}},
            }
        },
    )
    async def create(
        request: Request,
        session_id: str,
        environment_id: str,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> CreatedUpload:
        conv, owner = await authorize(request, session_id)
        trusted_origin(request)
        if _request_media_type(request) != "application/json":
            raise UploadError(ErrorCode.UNSUPPORTED_MEDIA_TYPE)
        try:
            declaration = UploadDeclaration.model_validate_json(
                await _read_capped(request, CREATE_BODY_MAX_BYTES)
            )
        except ValidationError as exc:
            raise UploadError(ErrorCode.INVALID_REQUEST) from exc
        if idempotency_key is not None and not re.fullmatch(r"[0-9a-f]{32}", idempotency_key):
            raise UploadError(ErrorCode.INVALID_REQUEST)
        await validate_session(
            session_id, request, browse_level(declaration.dir, within_workspace=LEVEL_EDIT)
        )
        if (
            ntpath.isabs(declaration.dir)
            or ntpath.splitdrive(declaration.dir)[0]
            or "\\" in declaration.dir
            or re.search(r"[\x00-\x1f\x7f]", declaration.dir)
            or len(declaration.dir.encode()) > 4096
        ):
            raise UploadError(ErrorCode.INVALID_PATH)
        try:
            declaration.dir = _validate_path(declaration.dir)
        except InvalidPath as exc:
            raise UploadError(ErrorCode.INVALID_PATH) from exc
        try:
            if not valid_name(declaration.name):
                raise UploadError(ErrorCode.INVALID_NAME)
        except UnicodeEncodeError as exc:
            raise UploadError(ErrorCode.INVALID_NAME) from exc
        await classify(conv, allow_unavailable=True)
        try:
            _, conv = await ensure_runner_connected(
                session_id=session_id,
                conv=conv,
                app_state=request.app.state,
                conversation_store=conversation_store,
                runner_router=runner_router or get_server_runner_router(),
                raise_host_refusal=True,
            )
        except OmnigentError as exc:
            raise OmnigentError("Session runner could not connect", code=exc.code) from exc
        client, cap = await capability(request, conv, environment_id)
        if not cap.supported:
            raise UploadError(ErrorCode.RUNNER_CAPABILITY_MISMATCH)
        if declaration.size > cap.limits.max_file_bytes:
            raise UploadError(ErrorCode.TOO_LARGE)
        digest = hashlib.sha256(declaration.model_dump_json().encode()).hexdigest()
        scope = (
            (session_id, environment_id, owner, idempotency_key)
            if idempotency_key is not None
            else None
        )
        now = time.monotonic()
        record = UploadRecord(
            owner,
            declaration.size,
            cap.limits,
            now,
            now,
            config.workspace_upload_staging_idle_ttl_s(),
            config.workspace_upload_staging_max_age_s(),
            conv.runner_id,
            idempotency_key=idempotency_key,
        )
        pending = (session_id, environment_id, uuid.uuid4().hex)
        index.prune(session_id)
        pending_result: Future[CreatedUpload | UploadError] | None = None
        wait_for: Future[CreatedUpload | UploadError] | None = None
        cached_id: str | None = None
        reserved = False
        with index.lock:
            in_flight = index.pending_creates.get(scope) if scope is not None else None
            if in_flight is not None:
                if in_flight[0] != digest:
                    raise UploadError(ErrorCode.IDEMPOTENCY_KEY_CONFLICT)
                wait_for = in_flight[1]
            else:
                replay = index.idempotency_keys.get(scope) if scope is not None else None
                live = (
                    index.records.get((session_id, environment_id, replay[0]))
                    if replay is not None
                    else None
                )
                if live is not None and not live.aborted:
                    if replay is not None and replay[1] != digest:
                        raise UploadError(ErrorCode.IDEMPOTENCY_KEY_CONFLICT)
                    cached_id = replay[0] if replay is not None else None
                else:
                    if scope is not None:
                        index.idempotency_keys.pop(scope, None)
                    if not index.has_capacity(session_id, declaration.size, cap.limits):
                        raise UploadError(ErrorCode.UPLOAD_LIMIT, {"Retry-After": "60"})
                    reserved = True
                if scope is not None:
                    pending_result = Future()
                    index.pending_creates[scope] = (digest, pending_result)
                if reserved:
                    try:
                        index.add(pending, record)
                    except BaseException:
                        if scope is not None:
                            index.pending_creates.pop(scope, None)
                        raise
        if wait_for is not None:
            outcome = await asyncio.shield(asyncio.wrap_future(wait_for))
            if isinstance(outcome, UploadError):
                raise outcome
            return outcome
        outcome: CreatedUpload | UploadError = UploadError(ErrorCode.UPLOAD_RELAY_FAILED)
        try:
            response = await _relay(
                client,
                "POST",
                _runner_path(session_id, environment_id),
                timeout=config.workspace_upload_chunk_timeout_s(),
                declaration={**declaration.model_dump(), "created_by": owner},
                idempotency_key=idempotency_key,
            )
            result: CreatedUpload = _json_model(response, CreatedUpload)
            if not re.fullmatch(r"[0-9a-f]{32}", result.upload_id) or not (
                0 <= result.offset <= declaration.size
            ):
                raise UploadError(ErrorCode.UPLOAD_RELAY_FAILED)
            rejected = False
            abort_new = False
            settled = record
            with index.lock:
                if reserved:
                    index.remove(pending)
                replaced_key = (
                    (session_id, environment_id, cached_id)
                    if cached_id is not None and cached_id != result.upload_id
                    else None
                )
                result_key = (session_id, environment_id, result.upload_id)
                existing = index.records.get(result_key)
                if (
                    idempotency_key is not None
                    and existing is not None
                    and existing.owner == owner
                    and existing.idempotency_key == idempotency_key
                ):
                    if replaced_key is not None:
                        index.remove(replaced_key)
                    settled = existing
                elif not reserved and not index.has_capacity(
                    session_id, declaration.size, cap.limits, replacing=replaced_key
                ):
                    rejected = True
                    abort_new = existing is None
                else:
                    if replaced_key is not None:
                        index.remove(replaced_key)
                    index.add(result_key, record)
                if not rejected and scope is not None and not settled.finished:
                    index.idempotency_keys[scope] = (result.upload_id, digest)
            if rejected:
                if abort_new:
                    with suppress(UploadError):
                        await _relay(
                            client,
                            "DELETE",
                            _runner_path(session_id, environment_id, "/" + result.upload_id),
                            timeout=config.workspace_upload_chunk_timeout_s(),
                            owner=owner,
                        )
                raise UploadError(ErrorCode.UPLOAD_LIMIT, {"Retry-After": "60"})
            result.chunk_bytes = cap.limits.chunk.initial
            result.expires_at = min(
                result.expires_at, time.time() + min(settled.idle_ttl_s, settled.max_age_s)
            )
            outcome = result
            return result
        except UploadError as exc:
            outcome = exc
            raise
        finally:
            # The runner accounts for creates whose acknowledgement was lost.
            with index.lock:
                if reserved:
                    index.remove(pending)
                if scope is not None and pending_result is not None:
                    in_flight = index.pending_creates.get(scope)
                    if in_flight is not None and in_flight[1] is pending_result:
                        index.pending_creates.pop(scope)
                        pending_result.set_result(outcome)

    async def owned(
        request: Request, session_id: str, environment_id: str, upload_id: str
    ) -> tuple[Conversation, str, UploadRecord]:
        conv, owner = await authorize(request, session_id)
        if request.method not in ("GET", "HEAD"):
            trusted_origin(request)
        await classify(conv, allow_unavailable=True)
        with index.lock:
            record = index.records.get((session_id, environment_id, upload_id))
            if record is None:
                raise UploadError(ErrorCode.NOT_FOUND)
            if record.owner != owner:
                raise UploadError(ErrorCode.UPLOAD_OWNER_MISMATCH)
            if (
                record.expired(time.monotonic())
                or record.aborted
                or record.runner_id != conv.runner_id
            ):
                raise UploadError(ErrorCode.GONE)
        return conv, owner, record

    def touched(record: UploadRecord) -> None:
        with index.lock:
            record.last_activity = time.monotonic()

    @uploads.patch(
        base + "/{upload_id}",
        status_code=204,
        response_class=Response,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/offset+octet-stream": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                },
            },
            "parameters": [
                {
                    "name": "Upload-Offset",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "integer", "minimum": 0},
                }
            ],
        },
    )
    async def append(
        request: Request, session_id: str, environment_id: str, upload_id: str
    ) -> Response:
        conv, owner, record = await owned(request, session_id, environment_id, upload_id)
        if _request_media_type(request) != "application/offset+octet-stream":
            raise UploadError(ErrorCode.UNSUPPORTED_MEDIA_TYPE)
        raw_offset = request.headers.get("upload-offset", "")
        if not re.fullmatch(r"[0-9]+", raw_offset) or len(raw_offset) > 20:
            raise UploadError(ErrorCode.INVALID_OFFSET)
        offset = int(raw_offset)
        reservation = record.limits.chunk.max + 1
        _chunk_admission.acquire(reservation)
        try:
            try:
                async with asyncio.timeout(config.workspace_upload_chunk_timeout_s()):
                    body = await _read_capped(request, record.limits.chunk.max)
            except TimeoutError as exc:
                raise UploadError(ErrorCode.UPLOAD_TIMEOUT) from exc
            client, _ = await runner_client(request, conv)
            response = await _relay(
                client,
                "PATCH",
                _runner_path(session_id, environment_id, "/" + upload_id),
                timeout=config.workspace_upload_chunk_timeout_s(),
                owner=owner,
                body=body,
                offset=offset,
            )
            returned = response.headers.get("Upload-Offset", "")
            if response.status_code != 204 or returned != str(offset + len(body)):
                raise UploadError(ErrorCode.UPLOAD_RELAY_FAILED)
            touched(record)
            return Response(
                status_code=204, headers={"Upload-Offset": returned, "Cache-Control": "no-store"}
            )
        finally:
            _chunk_admission.release(reservation)

    @uploads.head(base + "/{upload_id}", response_class=Response)
    async def status(
        request: Request, session_id: str, environment_id: str, upload_id: str
    ) -> Response:
        conv, owner, record = await owned(request, session_id, environment_id, upload_id)
        client, _ = await runner_client(request, conv)
        response = await _relay(
            client,
            "HEAD",
            _runner_path(session_id, environment_id, "/" + upload_id),
            timeout=config.workspace_upload_chunk_timeout_s(),
            owner=owner,
        )
        offset = response.headers.get("Upload-Offset", "")
        if (
            response.status_code != 200
            or not re.fullmatch(r"[0-9]+", offset)
            or response.headers.get("Upload-Length") != str(record.size)
        ):
            raise UploadError(ErrorCode.UPLOAD_RELAY_FAILED)
        touched(record)
        return Response(
            headers={
                "Upload-Offset": offset,
                "Upload-Length": str(record.size),
                "Cache-Control": "no-store",
            }
        )

    async def finalize_or_abort(
        request: Request, session_id: str, environment_id: str, upload_id: str, abort: bool
    ) -> Response:
        conv, owner, record = await owned(request, session_id, environment_id, upload_id)
        if not abort and request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            raise UploadError(ErrorCode.PERMISSION_DENIED)
        client, _ = await runner_client(request, conv)
        key = (session_id, environment_id, upload_id)
        timeout = (
            max(
                config.workspace_upload_finalize_timeout_s(),
                operation_timeout({"op": "complete", "size": record.size}),
            )
            + FINALIZE_RELAY_MARGIN_S
        )
        response = await _relay(
            client,
            "DELETE" if abort else "POST",
            _runner_path(
                session_id, environment_id, "/" + upload_id + ("" if abort else "/complete")
            ),
            timeout=timeout,
            owner=owner,
        )
        if abort and response.status_code == 204:
            with index.lock:
                index.finish(key, record, aborted=True)
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
        result: CompletedUpload = _json_model(response, CompletedUpload)
        try:
            safe_path = _validate_path(result.path)
        except InvalidPath as exc:
            raise UploadError(ErrorCode.UPLOAD_RELAY_FAILED) from exc
        if (
            result.size != record.size
            or ntpath.isabs(result.path)
            or "\\" in result.path
            or safe_path != result.path
            or "/" in result.final_name
            or "\\" in result.final_name
        ):
            raise UploadError(ErrorCode.UPLOAD_RELAY_FAILED)
        touched(record)
        with index.lock:
            transitioned = index.finish(key, record)
        if transitioned:
            _publish_changed_files_invalidated(session_id, environment_id)
        return JSONResponse(result.model_dump(), headers={"Cache-Control": "no-store"})

    @uploads.post(base + "/{upload_id}/complete", response_model=CompletedUpload)
    async def complete(
        request: Request, session_id: str, environment_id: str, upload_id: str
    ) -> Response:
        return await finalize_or_abort(request, session_id, environment_id, upload_id, False)

    @uploads.delete(
        base + "/{upload_id}",
        response_class=Response,
        responses={204: {"description": "Upload aborted"}, 200: {"model": CompletedUpload}},
    )
    async def abort(
        request: Request, session_id: str, environment_id: str, upload_id: str
    ) -> Response:
        return await finalize_or_abort(request, session_id, environment_id, upload_id, True)

    router.include_router(uploads)
