"""API route for importing normalized local harness transcripts."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, cast, get_args

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, field_validator

from omnigent.db.utils import builtin_agent_id
from omnigent.entities import NewConversationItem, parse_item_data
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import HostImportLocalFrame, encode_host_frame
from omnigent.native_coding_agents import native_coding_agent_for_harness
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.server.routes._session_create_validation import resolve_project_session_create
from omnigent.server.schemas import SessionCreateRequest
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
from omnigent.stores.project_store import ProjectStore

# Upper bound on items in one imported session, shared by the CLI-normalized
# ``/imports`` body and the host-streamed ``/imports/local`` path.
_MAX_IMPORT_ITEMS = 100_000


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
    """Request body for importing one local harness session.

    ``project_id`` files the imported session into a first-class project the
    caller owns, with the same ownership, default-fill, and mismatch-warning
    semantics as ``POST /v1/sessions``.
    """

    source: ImportSource
    external_session_id: str = Field(min_length=1, max_length=128)
    workspace: str | None = Field(default=None, max_length=2048)
    title: str | None = Field(default=None, max_length=512)
    force: bool = False
    project_id: str | None = None
    items: list[ImportItemInput] = Field(min_length=1, max_length=_MAX_IMPORT_ITEMS)

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


class LocalImportRequest(BaseModel):
    """Request to import the caller's recent local harness sessions from a host.

    Unlike ``/imports`` (the CLI posts already-normalized items), the server
    asks the chosen host to read + normalize its own transcripts over the
    tunnel — the transcripts live on the caller's machine, not the server.
    """

    host_id: str
    # A specific harness, or "all" to import from every supported harness on
    # the host in one batch (each imported session keeps its own source).
    source: ImportSource | Literal["all"]
    limit: int = Field(default=10, ge=1, le=100)


class ImportedSessionRef(BaseModel):
    """One freshly imported session: its new id plus display title.

    ``title`` is ``None`` when the session has no native title and no first user
    message to synthesize from; the UI falls back to a placeholder.
    """

    session_id: str
    title: str | None = None


class LocalImportResponse(BaseModel):
    """Batch result for ``POST /v1/imports/local``."""

    imported: int
    already_imported: int
    failed: int
    sessions: list[ImportedSessionRef]


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


async def _serialize_source_import(body: ImportSessionRequest) -> AsyncIterator[None]:
    """Serialize concurrent imports for one source identity in this server."""
    key = (body.source, body.external_session_id)
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


# Per-frame (inter-session) timeout: the host streams one session at a time, so
# this bounds the gap between frames — one transcript's read — not the whole
# batch. A batch of any size can take arbitrarily long without tripping it, so
# this can be tight: it's how fast a stalled or silently-dropped host is caught.
_HOST_IMPORT_TIMEOUT_S: float = 60.0


async def _stream_local_sessions_from_host(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    source: str,
    limit: int,
    stats: dict[str, int] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield the host's recent local sessions one at a time as they stream in.

    Sends a ``host.import_local`` frame and drains the per-request queue the
    tunnel fills: each ``host.import_local_session`` frame yields one session
    dict (``{total, external_session_id, workspace, items, title, source}``); the
    terminal ``host.import_local_done`` ends the stream. The caller persists each
    session as it arrives, so a large batch never buffers in one frame.

    :raises OmnigentError: If the host connection drops, a frame times out, or
        the host reports a read failure.
    """
    request_id = secrets.token_hex(8)
    frame = encode_host_frame(
        HostImportLocalFrame(request_id=request_id, source=source, limit=limit)
    )
    queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
    host_conn.pending_import_local[request_id] = queue
    try:
        try:
            host_registry.send_text(host_conn, frame)
        except ConnectionError as exc:
            raise OmnigentError(
                f"host '{host_conn.host_id}' connection lost during import",
                code=ErrorCode.CONFLICT,
            ) from exc
        while True:
            try:
                kind, data = await asyncio.wait_for(queue.get(), timeout=_HOST_IMPORT_TIMEOUT_S)
            except asyncio.TimeoutError as exc:
                raise OmnigentError(
                    f"host '{host_conn.host_id}' stalled mid-import "
                    f"(no session within {_HOST_IMPORT_TIMEOUT_S:.0f}s)",
                    code=ErrorCode.CONFLICT,
                ) from exc
            if kind == "session":
                yield data
            else:  # "done"
                if data.get("status") != "ok":
                    raise OmnigentError(
                        data.get("error") or "host failed to read local sessions",
                        code=ErrorCode.INTERNAL_ERROR,
                    )
                # Sessions the host enumerated but couldn't read send no frame;
                # surface their count so the caller's tally covers every target.
                if stats is not None:
                    stats["host_failed"] = int(data.get("failed") or 0)
                return
    finally:
        host_conn.pending_import_local.pop(request_id, None)


def create_imports_router(
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    project_store: ProjectStore | None = None,
    host_registry: HostRegistry | None = None,
    host_store: HostStore | None = None,
) -> APIRouter:
    """Create the local-session import router."""
    router = APIRouter()

    async def _persist_import(
        *,
        source: ImportSource,
        external_session_id: str,
        items: list[NewConversationItem],
        workspace: str | None,
        user_id: str | None,
        native_title: str | None = None,
        project_id: str | None = None,
    ) -> tuple[str, str | None]:
        """Create the conversation, append items, stamp import labels, grant owner.

        Shared by ``/imports`` (client-normalized items) and ``/imports/local``
        (server-read transcripts). ``native_title`` is the harness's own title
        when the caller has one; otherwise the title is synthesized from the
        first user message. ``project_id`` files the session into a project the
        caller owns (``/imports/local`` passes none). Caller handles the
        already-imported / force decision first. Returns
        ``(conversation id, title)``.
        """
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
        # Route the optional target project through the shared create
        # chokepoint: ownership (unowned/unknown → 404), default-fill of
        # omitted fields from the project config, and mismatch warnings all
        # behave exactly as on POST /v1/sessions. Only genuinely-present
        # fields go into the body so absent ones stay defaultable.
        create_kwargs: dict[str, Any] = {"agent_id": agent_id}
        if workspace is not None:
            create_kwargs["workspace"] = workspace
        if project_id is not None:
            create_kwargs["project_id"] = project_id
        resolved_create = await resolve_project_session_create(
            body=SessionCreateRequest(**create_kwargs),
            user_id=user_id,
            project_store=project_store,
        )
        agent_id = resolved_create.body.agent_id
        workspace = resolved_create.body.workspace
        title = (native_title or "").strip() or title_from_items(items)
        try:
            conversation = await asyncio.to_thread(
                conversation_store.create_conversation,
                title=title,
                agent_id=agent_id,
                workspace=workspace,
                conversation_id=_import_conversation_id(source, external_session_id),
                project_id=resolved_create.project_id,
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
        return conversation.id, title

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
        existing = await asyncio.to_thread(
            conversation_store.find_imported_conversation,
            body.source,
            body.external_session_id,
        )
        if existing is not None:
            await require_access(
                user_id,
                existing.id,
                LEVEL_OWNER,
                permission_store,
                conversation_store,
            )
            if not body.force:
                raise OmnigentError(
                    f"This {body.source} session has already been imported as {existing.id}",
                    code=ErrorCode.CONFLICT,
                )

        if existing is not None:
            await conversation_store.delete_conversation(existing.id)

        session_id, _title = await _persist_import(
            source=body.source,
            external_session_id=body.external_session_id,
            items=items,
            workspace=body.workspace,
            user_id=user_id,
            native_title=body.title,
            project_id=body.project_id,
        )

        response.status_code = 201
        return ImportSessionResponse(
            session_id=session_id,
            status="imported",
            item_count=len(items),
        )

    @router.post(
        "/imports/local",
        response_model=LocalImportResponse,
        dependencies=[Depends(require_json_content_type)],
    )
    async def import_local_sessions(
        body: LocalImportRequest,
        request: Request,
    ) -> LocalImportResponse:
        """Import the caller's recent local transcripts from a chosen host.

        The transcripts live on the caller's machine, so the read happens on
        the connected host over its tunnel — the server can't see them. The
        host enumerates + normalizes the most recent sessions; the server
        imports those not already imported. Drives the web "Import sessions"
        button.

        Not atomic: each session is persisted as its frame arrives. If the host
        drops mid-stream this raises after the sessions read so far are already
        committed; a retry is idempotent (they come back as already-imported).
        """
        if host_registry is None or host_store is None:
            raise OmnigentError(
                "host-mediated import is not available on this server",
                code=ErrorCode.INTERNAL_ERROR,
            )
        user_id = require_user(request, auth_provider)
        # Owns-host check + live connection, mirroring the runner-launch path.
        resolve_host_owner(user_id=user_id, host_id=body.host_id, host_store=host_store)
        host_conn = host_registry.get(body.host_id)
        if host_conn is None:
            raise OmnigentError(
                f"host '{body.host_id}' is not connected",
                code=ErrorCode.CONFLICT,
            )

        # Each session carries its own source (an "all" import mixes harnesses),
        # falling back to the request source for a single-harness import.
        valid_sources = set(get_args(ImportSource))
        imported = 0
        already_imported = 0
        failed = 0
        sessions: list[ImportedSessionRef] = []
        # Set by the stream to the count of sessions the host couldn't read (no
        # frame arrives for them); folded into ``failed`` after the loop.
        stats: dict[str, int] = {}
        # Persist each session as it streams in from the host (one frame each),
        # rather than buffering the whole batch.
        async for session in _stream_local_sessions_from_host(
            host_registry=host_registry,
            host_conn=host_conn,
            source=body.source,
            limit=body.limit,
            stats=stats,
        ):
            external_session_id = session.get("external_session_id")
            raw_items = session.get("items")
            session_source = session.get("source")
            source = (
                session_source
                if session_source in valid_sources
                else (body.source if body.source in valid_sources else None)
            )
            if (
                not isinstance(external_session_id, str)
                or not isinstance(raw_items, list)
                or source is None
                # Mirror the /imports item cap so one oversized transcript can't
                # balloon a batch import's memory.
                or len(raw_items) > _MAX_IMPORT_ITEMS
            ):
                failed += 1
                continue
            # The guard above rejected None and anything outside valid_sources
            # (get_args(ImportSource), which excludes "all"), so this is a
            # concrete harness — narrow off the request's ImportSource | "all".
            source = cast(ImportSource, source)
            existing = await asyncio.to_thread(
                conversation_store.find_imported_conversation,
                source,
                external_session_id,
            )
            if existing is not None:
                already_imported += 1
                continue
            try:
                items = [ImportItemInput.model_validate(raw).to_item() for raw in raw_items]
                workspace = session.get("workspace")
                native_title = session.get("title")
                session_id, title = await _persist_import(
                    source=source,
                    external_session_id=external_session_id,
                    items=items,
                    workspace=workspace if isinstance(workspace, str) else None,
                    user_id=user_id,
                    native_title=native_title if isinstance(native_title, str) else None,
                )
            except (OmnigentError, ValueError):
                failed += 1
                continue
            imported += 1
            sessions.append(ImportedSessionRef(session_id=session_id, title=title))
        # Fold in sessions the host enumerated but couldn't read, so the counts
        # account for every target the user asked to import.
        failed += stats.get("host_failed", 0)
        return LocalImportResponse(
            imported=imported,
            already_imported=already_imported,
            failed=failed,
            sessions=sessions,
        )

    return router
