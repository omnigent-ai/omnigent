"""Long-term memory routes (``/v1/memory``).

Memory belongs to the authenticated user, not to a session, so the routes are
top level like ``/v1/scheduled-tasks``. The owner is always derived here from
the authenticated user; no request field names it. A ``memory_*`` tool call
arrives from a runner carrying the owner's credential, plus the calling session
in the orchestration-source header. For those calls the route also requires that
the session is the owner's, that the runner is the one bound to it, and that no
one else can add input to it (a collaborator's turn would otherwise run with the
owner's memory).

``/v1/projects/{project_id}/memory`` is a project's own memory (see
:mod:`omnigent.server.project_memory`). The project comes from the URL and must be
one the caller can see; for a tool call it must also be the calling session's
project, and the role comes from that session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.feature_flags import Feature
from omnigent.server.project_memory import ProjectMemoryCaller, require_project_memory_write
from omnigent.server.role_side_chat import project_role_of_session
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.session_orchestration import require_private_bound_source
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.memory_store import (
    ACTOR_TYPE_USER,
    DEFAULT_PAGE_SIZE,
    DEFAULT_SEARCH_LIMIT,
    MAX_CONTENT_BYTES,
    MAX_DESCRIPTION_LENGTH,
    MAX_PAGE_SIZE,
    MAX_PATH_LENGTH,
    MAX_SEARCH_LIMIT,
    MEMORY_SEARCH_UNSUPPORTED,
    MemoryActor,
    MemoryEntry,
    MemoryStore,
)
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_store import ProjectStore
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes


class MemoryWriteRequest(BaseModel):
    """Body of ``PUT /v1/memory/entry``."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(max_length=MAX_PATH_LENGTH)
    content: str = Field(max_length=MAX_CONTENT_BYTES)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)


class MemorySearchRequest(BaseModel):
    """Body of ``POST /v1/memory/search``."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    path_prefix: str | None = Field(default=None, max_length=MAX_PATH_LENGTH)
    limit: int = Field(default=DEFAULT_SEARCH_LIMIT, ge=1, le=MAX_SEARCH_LIMIT)


class MemoryEntryResponse(BaseModel):
    """One memory entry. ``content`` is empty in listings."""

    path: str
    content: str
    description: str | None
    created_at: int
    updated_at: int


class MemoryEntryList(BaseModel):
    """Response of ``GET /v1/memory/entries``."""

    entries: list[MemoryEntryResponse]
    next_page_token: str | None = None


class MemoryHitResponse(BaseModel):
    """One ranked search result."""

    entry: MemoryEntryResponse
    score: float


class MemorySearchResponse(BaseModel):
    """Response of ``POST /v1/memory/search``."""

    hits: list[MemoryHitResponse]


class MemoryDeleteResponse(BaseModel):
    """Response of ``DELETE /v1/memory/entry``."""

    deleted: bool


class ProjectMemoryAccessResponse(BaseModel):
    """Response of ``GET /v1/projects/{project_id}/memory``: what the caller may do."""

    project_id: str
    identity_id: str | None
    writable_areas: list[str]


def _entry_response(entry: MemoryEntry) -> MemoryEntryResponse:
    """Drop the actor: it is always the caller."""
    return MemoryEntryResponse(
        path=entry.path,
        content=entry.content,
        description=entry.description,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


def actor_for_user(user_id: str | None) -> MemoryActor:
    """
    Memory actor for an authenticated user.

    :param user_id: Authenticated user id, or ``None`` when auth is disabled.
    :returns: The user actor; ``local`` in single-user mode.
    """
    return MemoryActor(ACTOR_TYPE_USER, RESERVED_USER_LOCAL if user_id is None else user_id)


class _MemoryRoute(APIRoute):
    """Answer 404 while memory is off, before FastAPI parses or validates the request.

    A router dependency would run too late: FastAPI decodes a JSON body before
    resolving dependencies, so malformed JSON would still get a 422.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def memory_handler(request: Request) -> Response:
            if not request.app.state.feature_flags.enabled(Feature.MEMORY):
                raise OmnigentError("Memory is disabled", code=ErrorCode.NOT_FOUND)
            return await handler(request)

        return memory_handler


def create_memory_router(
    memory_store: MemoryStore,
    *,
    auth_provider: AuthProvider | None,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
    project_store: ProjectStore | None = None,
) -> APIRouter:
    """
    Build the ``/memory`` router; mount it with ``prefix="/v1"``.

    :param memory_store: The configured backend.
    :param auth_provider: Resolves the caller; ``None`` is single-user mode.
    :param conversation_store: Resolves the calling session of a tool call.
    :param permission_store: Session sharing state. ``None`` refuses tool calls
        from authenticated users, whose calling session cannot be checked.
    :param project_store: Projects; mounts the project memory routes when set.
    :returns: The router.
    """
    router = APIRouter(route_class=_MemoryRoute)

    def resolve_actor(request: Request) -> MemoryActor:
        user_id = require_user(request, auth_provider)
        source_id = (request.headers.get(ORCHESTRATION_SOURCE_HEADER) or "").strip()
        if source_id and user_id is not None:
            # A tool call: the session must be the owner's and private, and the
            # caller must be its bound runner. Without sharing state that cannot
            # be checked, so the call is refused.
            if permission_store is None:
                raise OmnigentError(
                    "Memory tool calls need session permissions on this server",
                    code=ErrorCode.FORBIDDEN,
                )
            require_private_bound_source(request, user_id, conversation_store, permission_store)
        return actor_for_user(user_id)

    def resolve_project_caller(request: Request, project_id: str) -> ProjectMemoryCaller:
        """The project memory caller: a person on the project, or one of its sessions."""
        assert project_store is not None
        user_id = require_user(request, auth_provider)
        missing = OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        try:
            project_id = uuid_to_bytes(project_id).hex()
        except InvalidUuidError:
            raise missing from None
        project = project_store.get(project_id, user_id=user_id)
        if project is None:
            raise missing
        source_id = (request.headers.get(ORCHESTRATION_SOURCE_HEADER) or "").strip()
        if not source_id:
            # Every project role (owner, Manager, member) edits the project today.
            return ProjectMemoryCaller(project.id, is_agent=False, identity_id=None, may_edit=True)
        if user_id is not None:
            if permission_store is None:
                raise OmnigentError(
                    "Memory tool calls need session permissions on this server",
                    code=ErrorCode.FORBIDDEN,
                )
            source = require_private_bound_source(
                request, user_id, conversation_store, permission_store
            )
        else:
            try:
                source = conversation_store.get_conversation(uuid_to_bytes(source_id).hex())
            except InvalidUuidError:
                source = None
        # A session reads and writes only its own project's memory.
        if source is None or source.project_id != project.id:
            raise missing
        role = project_role_of_session(project, source.id, conversation_store, project_store)
        return ProjectMemoryCaller(
            project.id, is_agent=True, identity_id=role["id"] if role else None, may_edit=False
        )

    @router.put("/memory/entry", response_model=MemoryEntryResponse)
    async def write_entry(request: Request, body: MemoryWriteRequest) -> MemoryEntryResponse:
        actor = await asyncio.to_thread(resolve_actor, request)
        entry = await asyncio.to_thread(
            memory_store.write, actor, body.path, body.content, body.description
        )
        return _entry_response(entry)

    @router.get("/memory/entry", response_model=MemoryEntryResponse)
    async def get_entry(
        request: Request, path: Annotated[str, Query(max_length=MAX_PATH_LENGTH)]
    ) -> MemoryEntryResponse:
        actor = await asyncio.to_thread(resolve_actor, request)
        entry = await asyncio.to_thread(memory_store.get, actor, path)
        if entry is None:
            raise OmnigentError(f"No memory at {path!r}", code=ErrorCode.NOT_FOUND)
        return _entry_response(entry)

    @router.delete("/memory/entry", response_model=MemoryDeleteResponse)
    async def delete_entry(
        request: Request, path: Annotated[str, Query(max_length=MAX_PATH_LENGTH)]
    ) -> MemoryDeleteResponse:
        actor = await asyncio.to_thread(resolve_actor, request)
        return MemoryDeleteResponse(
            deleted=await asyncio.to_thread(memory_store.delete, actor, path)
        )

    @router.get("/memory/entries", response_model=MemoryEntryList)
    async def list_entries(
        request: Request,
        prefix: Annotated[str | None, Query(max_length=MAX_PATH_LENGTH)] = None,
        page_token: Annotated[str | None, Query(max_length=MAX_PATH_LENGTH)] = None,
        page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    ) -> MemoryEntryList:
        actor = await asyncio.to_thread(resolve_actor, request)
        page = await asyncio.to_thread(memory_store.list, actor, prefix, page_token, page_size)
        return MemoryEntryList(
            entries=[_entry_response(entry) for entry in page.entries],
            next_page_token=page.next_page_token,
        )

    @router.post("/memory/search", response_model=MemorySearchResponse)
    async def search_entries(request: Request, body: MemorySearchRequest) -> MemorySearchResponse:
        actor = await asyncio.to_thread(resolve_actor, request)
        if not memory_store.supports_search:
            raise OmnigentError(MEMORY_SEARCH_UNSUPPORTED, code=ErrorCode.NOT_FOUND)
        hits = await asyncio.to_thread(
            memory_store.search, actor, body.query, body.path_prefix, body.limit
        )
        return MemorySearchResponse(
            hits=[
                MemoryHitResponse(entry=_entry_response(hit.entry), score=hit.score)
                for hit in hits
            ]
        )

    if project_store is not None:
        _add_project_memory_routes(router, memory_store, resolve_project_caller)
    return router


def _add_project_memory_routes(
    router: APIRouter,
    memory_store: MemoryStore,
    resolve_caller: Callable[[Request, str], ProjectMemoryCaller],
) -> None:
    """``/projects/{project_id}/memory``: the same operations on a project's memory."""
    base = "/projects/{project_id}/memory"

    @router.get(base, response_model=ProjectMemoryAccessResponse)
    async def get_project_memory_access(
        request: Request, project_id: str
    ) -> ProjectMemoryAccessResponse:
        caller = await asyncio.to_thread(resolve_caller, request, project_id)
        return ProjectMemoryAccessResponse(
            project_id=caller.project_id,
            identity_id=caller.identity_id,
            writable_areas=caller.writable_areas(),
        )

    @router.put(f"{base}/entry", response_model=MemoryEntryResponse)
    async def write_project_entry(
        request: Request, project_id: str, body: MemoryWriteRequest
    ) -> MemoryEntryResponse:
        caller = await asyncio.to_thread(resolve_caller, request, project_id)
        require_project_memory_write(caller, body.path)
        entry = await asyncio.to_thread(
            memory_store.write, caller.actor, body.path, body.content, body.description
        )
        return _entry_response(entry)

    @router.get(f"{base}/entry", response_model=MemoryEntryResponse)
    async def get_project_entry(
        request: Request,
        project_id: str,
        path: Annotated[str, Query(max_length=MAX_PATH_LENGTH)],
    ) -> MemoryEntryResponse:
        caller = await asyncio.to_thread(resolve_caller, request, project_id)
        entry = await asyncio.to_thread(memory_store.get, caller.actor, path)
        if entry is None:
            raise OmnigentError(f"No memory at {path!r}", code=ErrorCode.NOT_FOUND)
        return _entry_response(entry)

    @router.delete(f"{base}/entry", response_model=MemoryDeleteResponse)
    async def delete_project_entry(
        request: Request,
        project_id: str,
        path: Annotated[str, Query(max_length=MAX_PATH_LENGTH)],
    ) -> MemoryDeleteResponse:
        caller = await asyncio.to_thread(resolve_caller, request, project_id)
        require_project_memory_write(caller, path)
        return MemoryDeleteResponse(
            deleted=await asyncio.to_thread(memory_store.delete, caller.actor, path)
        )

    @router.get(f"{base}/entries", response_model=MemoryEntryList)
    async def list_project_entries(
        request: Request,
        project_id: str,
        prefix: Annotated[str | None, Query(max_length=MAX_PATH_LENGTH)] = None,
        page_token: Annotated[str | None, Query(max_length=MAX_PATH_LENGTH)] = None,
        page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    ) -> MemoryEntryList:
        caller = await asyncio.to_thread(resolve_caller, request, project_id)
        page = await asyncio.to_thread(
            memory_store.list, caller.actor, prefix, page_token, page_size
        )
        return MemoryEntryList(
            entries=[_entry_response(entry) for entry in page.entries],
            next_page_token=page.next_page_token,
        )

    @router.post(f"{base}/search", response_model=MemorySearchResponse)
    async def search_project_entries(
        request: Request, project_id: str, body: MemorySearchRequest
    ) -> MemorySearchResponse:
        caller = await asyncio.to_thread(resolve_caller, request, project_id)
        if not memory_store.supports_search:
            raise OmnigentError(MEMORY_SEARCH_UNSUPPORTED, code=ErrorCode.NOT_FOUND)
        hits = await asyncio.to_thread(
            memory_store.search, caller.actor, body.query, body.path_prefix, body.limit
        )
        return MemorySearchResponse(
            hits=[
                MemoryHitResponse(entry=_entry_response(hit.entry), score=hit.score)
                for hit in hits
            ]
        )
