"""Private custom Agent library; launching reuses session-scoped bundle uploads."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import (
    LEVEL_OWNER,
    RESERVED_USER_LOCAL,
    AuthProvider,
    local_single_user_enabled,
)
from omnigent.server.bundles import bundle_location, validate_agent_bundle
from omnigent.server.custom_agent_bundles import MAX_BUNDLE_BYTES, patch_bundle
from omnigent.server.custom_agents_store import CustomAgentsStore
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.permission_store import PermissionStore

MAX_MULTIPART_REQUEST_BYTES = MAX_BUNDLE_BYTES + 1024 * 1024
_INSTRUCTIONS_CACHE_SIZE = 256


class CustomAgentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=8192)
    instructions: str | None = Field(default=None, max_length=262144)
    version: int | None = Field(default=None, ge=1)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str | None) -> str:
        if value is None or not value.strip():
            raise ValueError("name cannot be empty")
        return value.strip()


class CustomAgentImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_session_id: str = Field(min_length=1, max_length=256)


def create_custom_agents_router(
    store: CustomAgentsStore,
    artifact_store: ArtifactStore,
    agent_store: AgentStore,
    conversation_store: ConversationStore,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    router = APIRouter()
    instructions_cache: OrderedDict[str, str | None] = OrderedDict()
    instructions_cache_lock = threading.Lock()
    cache_miss = object()

    def owner(request: Request) -> str:
        return require_user(request, auth_provider) or RESERVED_USER_LOCAL

    def public(row: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in row.items() if key != "bundle_location"}

    def validate(data: bytes):
        if len(data) > MAX_BUNDLE_BYTES:
            raise OmnigentError("Agent bundle exceeds 32 MiB", code=ErrorCode.INVALID_INPUT)
        spec = validate_agent_bundle(
            data, enforce_handler_allowlist=not local_single_user_enabled()
        )
        limits = {
            "name": (spec.name, 256),
            "description": (spec.description, 8192),
            "harness": (spec.executor.harness_kind, 128),
            "model": (spec.executor.model, 512),
            "instructions": (spec.instructions, 262144),
        }
        for field, (value, limit) in limits.items():
            if value is not None and len(value) > limit:
                raise OmnigentError(
                    f"Agent {field} exceeds {limit} characters", code=ErrorCode.INVALID_INPUT
                )
        return spec

    def artifact_bytes(location: str) -> bytes:
        try:
            return artifact_store.get(location)
        except KeyError as exc:
            raise OmnigentError("Custom Agent bundle not found", code=ErrorCode.NOT_FOUND) from exc

    def cache_instructions(location: str, instructions: str | None) -> None:
        with instructions_cache_lock:
            instructions_cache[location] = instructions
            instructions_cache.move_to_end(location)
            while len(instructions_cache) > _INSTRUCTIONS_CACHE_SIZE:
                instructions_cache.popitem(last=False)

    def instructions_for(location: str) -> str | None:
        with instructions_cache_lock:
            cached = instructions_cache.get(location, cache_miss)
            if cached is not cache_miss:
                instructions_cache.move_to_end(location)
                return cached if isinstance(cached, str) else None
        spec = validate(artifact_bytes(location))
        cache_instructions(location, spec.instructions)
        return spec.instructions

    def detail(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **public(row),
            "instructions": instructions_for(row["bundle_location"]),
        }

    def persist_new(owner_id: str, data: bytes) -> dict[str, Any]:
        spec = validate(data)
        agent_id = f"ca_{uuid.uuid4().hex}"
        location = bundle_location(agent_id, data)
        artifact_store.put(location, data)
        row = store.create(
            owner_id,
            {
                "id": agent_id,
                "name": spec.name,
                "description": spec.description,
                "harness": spec.executor.harness_kind,
                "model": spec.executor.model,
                "bundle_location": location,
            },
        )
        cache_instructions(location, spec.instructions)
        return {**public(row), "instructions": spec.instructions}

    async def multipart_form(request: Request):
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_MULTIPART_REQUEST_BYTES:
                    raise HTTPException(413, "Agent bundle exceeds 32 MiB")
            except ValueError:
                pass

        original_receive = request.receive
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await original_receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_MULTIPART_REQUEST_BYTES:
                    raise MultiPartException("Agent bundle exceeds 32 MiB")
            return message

        request._receive = bounded_receive
        try:
            return await request.form(max_files=1, max_fields=0)
        except StarletteHTTPException as exc:
            if exc.detail == "Agent bundle exceeds 32 MiB":
                raise HTTPException(413, exc.detail) from exc
            raise
        finally:
            request._receive = original_receive

    @router.get("/custom-agents")
    async def list_custom_agents(
        request: Request,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        rows = await asyncio.to_thread(store.list, owner(request), limit + 1, offset)
        return {"data": [public(row) for row in rows[:limit]], "has_more": len(rows) > limit}

    @router.post(
        "/custom-agents",
        status_code=201,
        dependencies=[Depends(require_trusted_origin)],
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": CustomAgentImport.model_json_schema()},
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "required": ["bundle"],
                            "properties": {"bundle": {"type": "string", "format": "binary"}},
                        }
                    },
                },
            }
        },
    )
    async def create_custom_agent(request: Request) -> dict[str, Any]:
        owner_id = owner(request)
        source_session_id: str | None = None
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type == "multipart/form-data":
            form = await multipart_form(request)
            try:
                upload = form.get("bundle")
                if not isinstance(upload, UploadFile):
                    raise HTTPException(422, "bundle upload is required")
                data = await upload.read(MAX_BUNDLE_BYTES + 1)
            finally:
                await form.close()
        elif media_type == "application/json":
            try:
                body = CustomAgentImport.model_validate(await request.json())
            except (ValueError, ValidationError) as exc:
                raise HTTPException(422, "source_session_id is required") from exc
            user_id = require_user(request, auth_provider)
            if auth_provider is not None and permission_store is None:
                raise OmnigentError(
                    "Session ownership checks unavailable", code=ErrorCode.FORBIDDEN
                )
            await require_access(
                user_id, body.source_session_id, LEVEL_OWNER, permission_store, conversation_store
            )
            conv = await asyncio.to_thread(
                conversation_store.get_conversation, body.source_session_id
            )
            agent = (
                await asyncio.to_thread(agent_store.get, conv.agent_id)
                if conv and conv.agent_id
                else None
            )
            if agent is None or agent.session_id is None:
                raise OmnigentError("Custom session Agent not found", code=ErrorCode.NOT_FOUND)
            data = await asyncio.to_thread(artifact_bytes, agent.bundle_location)
            source_session_id = body.source_session_id
        else:
            raise HTTPException(415, "Use application/json or multipart/form-data")
        created = await asyncio.to_thread(persist_new, owner_id, data)
        if source_session_id is not None:
            try:
                await asyncio.to_thread(
                    conversation_store.set_labels,
                    source_session_id,
                    {"omnigent:agent-template-id": created["id"]},
                )
            except Exception:
                await asyncio.to_thread(store.delete, owner_id, created["id"])
                raise
        return created

    @router.get("/custom-agents/{agent_id}")
    async def get_custom_agent(request: Request, agent_id: str) -> dict[str, Any]:
        row = await asyncio.to_thread(store.get, owner(request), agent_id)
        return await asyncio.to_thread(detail, row)

    @router.get(
        "/custom-agents/{agent_id}/contents",
        response_class=Response,
        responses={200: {"content": {"application/gzip": {}, "application/x-tar": {}}}},
    )
    async def get_custom_agent_contents(request: Request, agent_id: str) -> Response:
        row = await asyncio.to_thread(store.get, owner(request), agent_id)
        data = await asyncio.to_thread(artifact_bytes, row["bundle_location"])
        media_type = "application/gzip" if data.startswith(b"\x1f\x8b") else "application/x-tar"
        return Response(data, media_type=media_type, headers={"Cache-Control": "no-store"})

    @router.patch("/custom-agents/{agent_id}", dependencies=[Depends(require_json_content_type)])
    async def patch_custom_agent(
        request: Request, agent_id: str, body: CustomAgentPatch
    ) -> dict[str, Any]:
        owner_id = owner(request)
        row = await asyncio.to_thread(store.get, owner_id, agent_id)
        if body.version is not None and body.version != row["version"]:
            raise OmnigentError(
                "Custom Agent changed; reload before saving", code=ErrorCode.CONFLICT
            )
        changes = body.model_dump(exclude_unset=True, exclude={"version"})
        if not changes:
            return await asyncio.to_thread(detail, row)

        def update() -> dict[str, Any]:
            data = patch_bundle(artifact_bytes(row["bundle_location"]), changes)
            spec = validate(data)
            location = bundle_location(agent_id, data)
            artifact_store.put(location, data)
            updated = store.update(
                owner_id,
                agent_id,
                row["version"],
                {
                    "name": spec.name,
                    "description": spec.description,
                    "bundle_location": location,
                },
            )
            cache_instructions(location, spec.instructions)
            return {**public(updated), "instructions": spec.instructions}

        return await asyncio.to_thread(update)

    @router.delete("/custom-agents/{agent_id}", status_code=204)
    async def delete_custom_agent(request: Request, agent_id: str) -> Response:
        await asyncio.to_thread(store.delete, owner(request), agent_id)
        return Response(status_code=204)

    return router
