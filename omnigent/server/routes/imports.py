"""API route for importing normalized local harness transcripts."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field, field_validator

from omnigent.db.utils import builtin_agent_id
from omnigent.entities import NewConversationItem, parse_item_data
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.native_coding_agents import native_coding_agent_for_harness
from omnigent.server.auth import LEVEL_OWNER, AuthProvider, local_single_user_enabled
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.session_import import (
    IMPORT_EXTERNAL_SESSION_ID_LABEL_KEY,
    IMPORT_SOURCE_LABEL_KEY,
    ImportSource,
    SessionImportNotFoundError,
    title_from_items,
)
from omnigent.session_import.local import list_recent_local_sessions, load_local_session
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.conversation_store import ConversationAlreadyExistsError
from omnigent.stores.permission_store import PermissionStore

# Ceiling on one discovery pass. Each candidate is parsed to read its title and
# item count, so an unbounded limit would walk every transcript on disk.
_MAX_LOCAL_SESSION_LIST_LIMIT = 50


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


class ImportLocalSessionRequest(BaseModel):
    """Request body for importing one transcript from the server's own disk."""

    source: ImportSource
    external_session_id: str = Field(min_length=1, max_length=128)
    force: bool = False

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


class LocalSessionSummaryResponse(BaseModel):
    """One local transcript offered to the import picker."""

    session_id: str
    title: str | None
    workspace: str | None
    item_count: int
    # POSIX seconds, like the timestamps on every other API object.
    modified_at: float | None


class LocalSessionListResponse(BaseModel):
    """Recent local transcripts for one harness, newest first."""

    sessions: list[LocalSessionSummaryResponse]


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
    """Serialize concurrent imports for one source identity in this server."""
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
    """Hold the source-identity lock for the duration of one import request."""
    async with _source_import_lock(body.source, body.external_session_id):
        yield


def _require_local_transcript_access() -> None:
    """Refuse to read the server's own transcripts on a shared server.

    The local endpoints expose whatever harness history sits in the server
    process's home directory. That is the caller's own machine only on a
    single-user local runtime; on a deployed server it belongs to the
    operator, so importing another machine's chats has to go through that
    host rather than the server's disk.
    """
    if not local_single_user_enabled():
        raise OmnigentError(
            "Importing local chats is only available on a single-user local server",
            code=ErrorCode.FORBIDDEN,
        )


def create_imports_router(
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Create the local-session import router."""
    router = APIRouter()

    async def store_imported_session(
        source: ImportSource,
        external_session_id: str,
        *,
        workspace: str | None,
        force: bool,
        items: list[NewConversationItem],
        user_id: str | None,
    ) -> ImportSessionResponse:
        """Persist one normalized transcript as an owned native session."""
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
        result = await store_imported_session(
            body.source,
            body.external_session_id,
            workspace=body.workspace,
            force=body.force,
            items=items,
            user_id=user_id,
        )
        response.status_code = 201
        return result

    @router.get("/imports/local/sessions", response_model=LocalSessionListResponse)
    async def list_local_sessions(
        request: Request,
        source: ImportSource,
        limit: int = Query(default=20, ge=1, le=_MAX_LOCAL_SESSION_LIST_LIMIT),
    ) -> LocalSessionListResponse:
        """List recent transcripts the given harness left on the server's disk."""
        require_user(request, auth_provider)
        _require_local_transcript_access()
        try:
            summaries = await asyncio.to_thread(list_recent_local_sessions, source, limit=limit)
        except SessionImportNotFoundError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.NOT_FOUND) from exc
        return LocalSessionListResponse(
            sessions=[
                LocalSessionSummaryResponse(
                    session_id=summary.session_id,
                    title=summary.title,
                    workspace=summary.workspace,
                    item_count=summary.item_count,
                    modified_at=summary.modified_at,
                )
                for summary in summaries
            ]
        )

    @router.post(
        "/imports/local",
        response_model=ImportSessionResponse,
        dependencies=[Depends(require_json_content_type)],
    )
    async def import_local_session(
        body: ImportLocalSessionRequest,
        request: Request,
        response: Response,
    ) -> ImportSessionResponse:
        """Read one transcript off the server's disk and import it."""
        user_id = require_user(request, auth_provider)
        _require_local_transcript_access()
        try:
            imported = await asyncio.to_thread(
                load_local_session,
                body.source,
                body.external_session_id,
            )
        except SessionImportNotFoundError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.NOT_FOUND) from exc
        except (OSError, TypeError, ValueError) as exc:
            raise OmnigentError(
                f"Could not read the {body.source} session: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        async with _source_import_lock(body.source, body.external_session_id):
            result = await store_imported_session(
                body.source,
                body.external_session_id,
                workspace=imported.workspace,
                force=body.force,
                items=list(imported.items),
                user_id=user_id,
            )
        response.status_code = 201
        return result

    return router
