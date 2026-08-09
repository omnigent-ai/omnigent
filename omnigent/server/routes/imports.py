"""API route for importing normalized local harness transcripts."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, ValidationError, field_validator

from omnigent.db.utils import builtin_agent_id
from omnigent.entities import NewConversationItem, parse_item_data
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.native_coding_agents import native_coding_agent_for_harness
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._host_session_import import (
    LocalSessionHostUnavailableError,
    LocalSessionNotFoundError,
    LocalSessionProxyError,
    list_local_sessions_on_host,
    load_local_session_on_host,
)
from omnigent.session_import import (
    IMPORT_EXTERNAL_SESSION_ID_LABEL_KEY,
    IMPORT_SOURCE_LABEL_KEY,
    ImportSource,
    title_from_items,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.conversation_store import ConversationAlreadyExistsError
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)

# The web picker only browses the two harnesses whose transcripts the
# host daemon can read; the CLI still imports the other sources by id.
ImportBrowseSource = Literal["claude", "codex"]

# The picker parses a full transcript per row, so a deep list is slow
# for little benefit; the CLI still imports older chats by id.
_MAX_RECENT_LOCAL_SESSIONS = 20


class ImportItemInput(BaseModel):
    """One normalized existing Omnigent item received from the CLI."""

    type: str
    response_id: str = Field(min_length=1, max_length=64)
    data: dict[str, object]

    def to_item(self) -> NewConversationItem:
        """Validate the type-specific payload and return a new item entity."""
        try:
            data = parse_item_data(self.type, self.data)
            return NewConversationItem(type=self.type, response_id=self.response_id, data=data)
        except (TypeError, ValueError) as exc:
            raise OmnigentError(
                f"Invalid imported {self.type!r} item: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc


class ImportSessionRequest(BaseModel):
    """Request body for importing one local harness session."""

    source: ImportSource
    external_session_id: str = Field(min_length=1, max_length=128)
    workspace: str | None = Field(default=None, max_length=2048)
    force: bool = False
    items: list[ImportItemInput] = Field(min_length=1, max_length=100_000)

    @field_validator("external_session_id")
    @classmethod
    def strip_external_session_id(cls, value: str) -> str:
        """Reject a source session id that is only whitespace."""
        value = value.strip()
        if not value:
            raise ValueError("external_session_id must not be blank")
        return value


class ImportSessionResponse(BaseModel):
    """Result of importing or locating one source session."""

    session_id: str
    status: Literal["imported"]
    item_count: int


class LocalSessionPreviewOutput(BaseModel):
    """One preview message shown when a picker row is expanded."""

    role: str
    text: str


class LocalSessionSummaryOutput(BaseModel):
    """One browsable local session, as returned to the import picker."""

    source: ImportBrowseSource
    external_session_id: str
    workspace: str | None = None
    title: str | None = None
    item_count: int
    preview: list[LocalSessionPreviewOutput] = Field(default_factory=list)


class LocalSessionListOutput(BaseModel):
    """Envelope returned by the local-session browse endpoint."""

    object: Literal["list"] = "list"
    data: list[LocalSessionSummaryOutput] = Field(default_factory=list)


class ImportLocalSessionRequest(BaseModel):
    """Request body for importing one session read from a host."""

    host_id: str = Field(min_length=1, max_length=128)
    source: ImportBrowseSource
    external_session_id: str = Field(min_length=1, max_length=128)

    @field_validator("external_session_id")
    @classmethod
    def strip_external_session_id(cls, value: str) -> str:
        """Reject a source session id that is only whitespace."""
        value = value.strip()
        if not value:
            raise ValueError("external_session_id must not be blank")
        return value


@dataclass
class _ImportLockEntry:
    """One process-local source lock and its active/waiting user count."""

    lock: asyncio.Lock
    users: int = 0


_IMPORT_LOCKS: dict[tuple[ImportSource, str], _ImportLockEntry] = {}
_IMPORT_LOCKS_GUARD = threading.Lock()


def _import_conversation_id(source: ImportSource, external_session_id: str) -> str:
    """Derive one stable database identity for an imported source session."""
    value = f"import:{source}:{external_session_id}"
    return hashlib.sha256(value.encode()).hexdigest()[:32]


@asynccontextmanager
async def _source_import_lock(
    source: ImportSource,
    external_session_id: str,
) -> AsyncIterator[None]:
    """Serialize concurrent imports for one source identity in this server.

    The sole owner of :data:`_IMPORT_LOCKS`; both the CLI's
    ``POST /v1/imports`` and the Web UI's
    ``POST /v1/imports/local-sessions`` contend on the same map.

    :param source: Harness that owns the source session.
    :param external_session_id: Harness-native session id.
    :returns: Async context manager held for the duration of one import.
    """
    key = (source, external_session_id)
    with _IMPORT_LOCKS_GUARD:
        entry = _IMPORT_LOCKS.setdefault(key, _ImportLockEntry(lock=asyncio.Lock()))
        entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        with _IMPORT_LOCKS_GUARD:
            entry.users -= 1
            if entry.users == 0:
                _IMPORT_LOCKS.pop(key, None)


async def _serialize_source_import(body: ImportSessionRequest) -> AsyncIterator[None]:
    """Serialize concurrent imports for one source identity in this server."""
    async with _source_import_lock(body.source, body.external_session_id):
        yield


async def _create_imported_session(
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    permission_store: PermissionStore | None,
    user_id: str | None,
    source: ImportSource,
    external_session_id: str,
    workspace: str | None,
    items: list[NewConversationItem],
    force: bool = False,
) -> ImportSessionResponse:
    """Create one imported session, rejecting an already-imported source.

    Shared by the CLI's ``POST /v1/imports`` and the Web UI's
    ``POST /v1/imports/local-sessions`` so both produce identical
    provenance labels, duplicate handling, and ownership grants.

    :param conversation_store: Store the session is created in.
    :param agent_store: Store used to confirm the built-in agent exists.
    :param permission_store: Store granting the caller ownership, or
        ``None`` when permissions are disabled.
    :param user_id: Authenticated caller, or ``None`` when auth is off.
    :param source: Harness that owns the source session.
    :param external_session_id: Harness-native session id.
    :param workspace: Working directory recorded in the transcript.
    :param items: Normalized items to append.
    :param force: Replace a prior import of the same source session
        instead of rejecting it.
    :returns: The import result.
    :raises OmnigentError: ``CONFLICT`` when already imported,
        ``INVALID_INPUT`` for an unsupported source, ``INTERNAL_ERROR``
        when the built-in agent is missing.
    """
    existing = await asyncio.to_thread(
        conversation_store.find_imported_conversation,
        source,
        external_session_id,
    )
    if existing is not None:
        await require_access(
            user_id,
            existing.id,
            LEVEL_OWNER,
            permission_store,
            conversation_store,
        )
        if not force:
            raise OmnigentError(
                f"This {source} session has already been imported as {existing.id}",
                code=ErrorCode.CONFLICT,
            )

    native_agent = native_coding_agent_for_harness(f"{source}-native")
    if native_agent is None:
        raise OmnigentError(
            f"Unsupported import source: {source}",
            code=ErrorCode.INVALID_INPUT,
        )
    agent_id = builtin_agent_id(native_agent.agent_name)
    if await asyncio.to_thread(agent_store.get, agent_id) is None:
        raise OmnigentError(
            f"The {native_agent.display_name} built-in agent is unavailable",
            code=ErrorCode.INTERNAL_ERROR,
        )

    if existing is not None:
        await conversation_store.delete_conversation(existing.id)

    try:
        conversation = await asyncio.to_thread(
            conversation_store.create_conversation,
            title=title_from_items(items),
            agent_id=agent_id,
            workspace=workspace,
            conversation_id=_import_conversation_id(source, external_session_id),
        )
    except ConversationAlreadyExistsError as exc:
        raise OmnigentError(
            "This source session has already been imported",
            code=ErrorCode.CONFLICT,
        ) from exc
    try:
        await asyncio.to_thread(
            conversation_store.set_external_session_id,
            conversation.id,
            external_session_id,
        )
        await asyncio.to_thread(conversation_store.append, conversation.id, items)
        labels = {
            **native_agent.presentation_labels,
            IMPORT_SOURCE_LABEL_KEY: source,
            IMPORT_EXTERNAL_SESSION_ID_LABEL_KEY: external_session_id,
        }
        await asyncio.to_thread(conversation_store.set_labels, conversation.id, labels)
        if permission_store is not None and user_id is not None:
            await asyncio.to_thread(permission_store.ensure_user, user_id)
            await asyncio.to_thread(
                permission_store.grant,
                user_id,
                conversation.id,
                LEVEL_OWNER,
            )
    except Exception:
        await conversation_store.delete_conversation(conversation.id)
        raise

    return ImportSessionResponse(
        session_id=conversation.id,
        status="imported",
        item_count=len(items),
    )


def create_imports_router(
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    host_registry: HostRegistry | None = None,
    host_store: HostStore | None = None,
) -> APIRouter:
    """Create the local-session import router.

    :param conversation_store: Store imported sessions are created in.
    :param agent_store: Store used to confirm the built-in agent exists.
    :param auth_provider: Provider resolving the caller, or ``None``
        when auth is disabled.
    :param permission_store: Store granting the caller ownership, or
        ``None`` when permissions are disabled.
    :param host_registry: Per-replica live host connections; ``None``
        on a server without host support, which makes the
        browse/import-from-host endpoints answer 503.
    :param host_store: Store of registered hosts; ``None`` on a server
        without host support.
    :returns: The router, to be mounted under ``/v1``.
    """
    router = APIRouter()

    @router.post(
        "/imports",
        response_model=ImportSessionResponse,
        dependencies=[
            Depends(require_json_content_type),
            Depends(_serialize_source_import),
        ],
    )
    async def import_session(
        body: ImportSessionRequest,
        request: Request,
        response: Response,
    ) -> ImportSessionResponse:
        """Import one normalized transcript, optionally replacing its prior import."""
        user_id = require_user(request, auth_provider)
        items = [item.to_item() for item in body.items]
        result = await _create_imported_session(
            conversation_store=conversation_store,
            agent_store=agent_store,
            permission_store=permission_store,
            user_id=user_id,
            source=body.source,
            external_session_id=body.external_session_id,
            workspace=body.workspace,
            items=items,
            force=body.force,
        )
        response.status_code = 201
        return result

    async def _resolve_online_host(
        request: Request,
        host_id: str,
    ) -> tuple[str | None, HostRegistry, HostConnection]:
        """Authorize the caller and return a live connection to their host.

        Also returns the registry so callers get it already narrowed out
        of the optional host-support configuration.

        :param request: FastAPI request (for auth).
        :param host_id: Host identifier from the caller.
        :returns: ``(user_id, host_registry, host_connection)``.
        :raises HTTPException: 503 when host support is unconfigured,
            404 when the host is unknown, 403 when it is not the
            caller's, 409 when it is offline.
        """
        if host_registry is None or host_store is None:
            raise HTTPException(
                status_code=503, detail="host support is not configured on this server"
            )
        user_id = require_user(request, auth_provider)
        host = await asyncio.to_thread(host_store.get_host, host_id)
        if host is None:
            raise HTTPException(status_code=404, detail="host not found")
        if user_id is not None and host.user_id != user_id:
            raise HTTPException(status_code=403, detail="not your host")
        conn = host_registry.get(host.host_id)
        if conn is None:
            raise HTTPException(status_code=409, detail="host is offline")
        return user_id, host_registry, conn

    @router.get("/imports/local-sessions", response_model=LocalSessionListOutput)
    async def list_local_sessions(
        request: Request,
        host_id: Annotated[str, Query()],
        source: Annotated[ImportBrowseSource, Query()],
        limit: Annotated[int, Query(ge=1, le=_MAX_RECENT_LOCAL_SESSIONS)] = 10,
    ) -> LocalSessionListOutput:
        """
        List recent Claude Code or Codex sessions on a host.

        Backs the New Session import picker, which shows each session's
        title, workspace, item count, and expandable context. Titles use
        the same synthesis the import itself applies, so a browsed row
        and the session it creates read the same.

        A row the server cannot parse — e.g. from a host running an
        older protocol version — is skipped rather than failing the
        whole listing, matching how the CLI's own local scan treats an
        unreadable transcript.

        :param request: FastAPI request (for auth).
        :param host_id: Host to browse, e.g. ``"host_a1b2c3d4..."``.
        :param source: ``"claude"`` or ``"codex"``.
        :param limit: Maximum sessions to return (1-20).
        :returns: ``{"object": "list", "data": [...]}`` newest first.
        :raises HTTPException: 400 when the host reports a failure, 403
            when the host is not the caller's, 404 when the host is
            unknown, 409 when it is offline, 503 when the server has no
            host support.
        """
        _user_id, registry, conn = await _resolve_online_host(request, host_id)
        try:
            sessions = await list_local_sessions_on_host(
                host_registry=registry,
                host_conn=conn,
                source=source,
                limit=limit,
            )
        # The unavailable subclass must precede its base or a dropped
        # tunnel would read as bad user input.
        except LocalSessionHostUnavailableError as exc:
            raise HTTPException(status_code=409, detail=exc.message) from exc
        except LocalSessionProxyError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
        summaries: list[LocalSessionSummaryOutput] = []
        for entry in sessions:
            try:
                summaries.append(LocalSessionSummaryOutput(**entry))
            except (TypeError, ValidationError):
                # A host on a different version can send a row this server
                # can't parse; skip it rather than failing the whole picker.
                _logger.warning(
                    "host '%s' returned a malformed local-session row, skipping it",
                    conn.host_id,
                )
        return LocalSessionListOutput(data=summaries)

    @router.post(
        "/imports/local-sessions",
        response_model=ImportSessionResponse,
        dependencies=[Depends(require_json_content_type)],
    )
    async def import_local_session(
        body: ImportLocalSessionRequest,
        request: Request,
        response: Response,
    ) -> ImportSessionResponse:
        """
        Import one Claude Code or Codex session read from a host.

        The host loads and normalizes the transcript; the session is
        then created through the same path the CLI's ``POST /v1/imports``
        uses, so provenance labels and duplicate handling match.

        :param body: Host, source, and harness-native session id.
        :param request: FastAPI request (for auth).
        :param response: Response, set to 201 on success.
        :returns: The created session id and item count.
        :raises HTTPException: 400 when the host reports a failure, 403
            when the host is not the caller's, 404 when the host or the
            session is unknown, 409 when the host is offline, 503 when
            the server has no host support.
        :raises OmnigentError: ``INVALID_INPUT`` when the host found no
            importable history or returned a malformed item, plus
            whatever :func:`_create_imported_session` raises.
        """
        user_id, registry, conn = await _resolve_online_host(request, body.host_id)
        try:
            payload = await load_local_session_on_host(
                host_registry=registry,
                host_conn=conn,
                source=body.source,
                external_session_id=body.external_session_id,
            )
        # Both specific errors subclass LocalSessionProxyError, so they
        # must be caught before it or every 404/409 becomes a 400.
        except LocalSessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=exc.message) from exc
        except LocalSessionHostUnavailableError as exc:
            raise HTTPException(status_code=409, detail=exc.message) from exc
        except LocalSessionProxyError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc

        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise OmnigentError(
                "The host returned no importable history for this session",
                code=ErrorCode.INVALID_INPUT,
            )
        try:
            items = [ImportItemInput(**entry).to_item() for entry in raw_items]
        except (TypeError, ValidationError) as exc:
            raise OmnigentError(
                f"host '{conn.host_id}' returned an invalid session item",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        workspace = payload.get("workspace")
        async with _source_import_lock(body.source, body.external_session_id):
            result = await _create_imported_session(
                conversation_store=conversation_store,
                agent_store=agent_store,
                permission_store=permission_store,
                user_id=user_id,
                source=body.source,
                external_session_id=body.external_session_id,
                workspace=workspace if isinstance(workspace, str) else None,
                items=items,
            )
        response.status_code = 201
        return result

    return router
