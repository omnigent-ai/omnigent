"""API route for importing normalized local harness transcripts."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, field_validator

from omnigent.db.utils import builtin_agent_id
from omnigent.entities import NewConversationItem, parse_item_data
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.native_coding_agents import native_coding_agent_for_harness
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.session_import import (
    IMPORT_EXTERNAL_SESSION_ID_LABEL_KEY,
    IMPORT_SOURCE_LABEL_KEY,
    ImportSource,
    title_from_items,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.conversation_store import ConversationAlreadyExistsError
from omnigent.stores.permission_store import PermissionStore


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
) -> APIRouter:
    """Create the local-session import router."""
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

    return router
