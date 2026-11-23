"""Explicit shared references; Google content and authorization remain at Google."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, AuthProvider
from omnigent.server.feature_flags import Feature
from omnigent.server.routes._auth_helpers import require_access_and_level, require_user
from omnigent.session_documents import SessionDocumentEntry, validate_google_docs_url
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.session_document_store import (
    SessionDocumentLimitError,
    SessionDocumentStore,
    UnreadableSessionDocumentsError,
)


class _NoStoreRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def no_store(request: Request) -> Response:
            # Keep validation and authorization errors out of intermediary caches too.
            try:
                response = await handler(request)
            except Exception as exc:
                error_handler = next(
                    (
                        request.app.exception_handlers[cls]
                        for cls in type(exc).__mro__
                        if cls in request.app.exception_handlers and cls is not Exception
                    ),
                    None,
                )
                if error_handler is None:
                    raise
                response = await error_handler(request, exc)
            response.headers["Cache-Control"] = "no-store"
            return response

        return no_store


class SessionDocumentAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=4096)


class SessionDocumentResponse(BaseModel):
    session_id: str
    entries: list[SessionDocumentEntry]


def create_session_documents_router(
    conversations: ConversationStore,
    documents: SessionDocumentStore | None,
    auth_provider: AuthProvider | None,
    permissions: PermissionStore | None,
) -> APIRouter:
    router = APIRouter(route_class=_NoStoreRoute)

    async def authorize(request: Request, session_id: str, level: int) -> str:
        if not request.app.state.feature_flags.enabled(Feature.GOOGLE_DOCS):
            raise OmnigentError("Google Docs is disabled", code=ErrorCode.FORBIDDEN)
        user_id = require_user(request, auth_provider)
        if any(
            header in request.headers
            for header in (ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER)
        ):
            raise OmnigentError("User access is required", code=ErrorCode.FORBIDDEN)
        access = await require_access_and_level(
            user_id, session_id, level, permissions, conversations
        )
        conv = access.conversation or await asyncio.to_thread(
            conversations.get_conversation, session_id
        )
        if conv is None:
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
        if documents is None:
            raise OmnigentError(
                "Session document storage is unavailable", code=ErrorCode.INTERNAL_ERROR
            )
        return conv.id

    async def apply(
        operation: Callable[..., list[SessionDocumentEntry] | None], canonical_id: str, *args: str
    ) -> SessionDocumentResponse:
        try:
            entries = await asyncio.to_thread(operation, canonical_id, *args)
        except SessionDocumentLimitError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.CONFLICT) from exc
        except UnreadableSessionDocumentsError as exc:
            raise OmnigentError(
                "Session document storage is unreadable", code=ErrorCode.INTERNAL_ERROR
            ) from exc
        if entries is None:
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
        return SessionDocumentResponse(session_id=canonical_id, entries=entries)

    @router.get("/sessions/{session_id}/session-documents")
    async def read_documents(request: Request, session_id: str) -> SessionDocumentResponse:
        canonical_id = await authorize(request, session_id, LEVEL_READ)
        assert documents is not None
        return await apply(documents.read, canonical_id)

    @router.post("/sessions/{session_id}/session-documents")
    async def add_document(
        request: Request, session_id: str, body: SessionDocumentAdd
    ) -> SessionDocumentResponse:
        canonical_id = await authorize(request, session_id, LEVEL_EDIT)
        assert documents is not None
        try:
            validate_google_docs_url(body.url)
            SessionDocumentEntry(document_url=body.url.strip(), added_at=1)
        except ValueError as exc:
            raise OmnigentError(
                "Expected a supported Google Docs URL of at most 2048 characters",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        return await apply(documents.add, canonical_id, body.url)

    @router.delete("/sessions/{session_id}/session-documents/{document_id}")
    async def remove_document(
        request: Request, session_id: str, document_id: str
    ) -> SessionDocumentResponse:
        canonical_id = await authorize(request, session_id, LEVEL_EDIT)
        assert documents is not None
        try:
            reference = validate_google_docs_url(
                f"https://docs.google.com/document/d/{document_id}/edit"
            )
            if reference.document_id != document_id:
                raise ValueError("Invalid document identity")
        except ValueError as exc:
            raise OmnigentError("Invalid document identity", code=ErrorCode.INVALID_INPUT) from exc
        return await apply(documents.remove, canonical_id, document_id)

    return router
