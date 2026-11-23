"""Owner-private image attachments for project ticket comments and descriptions."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request, Response, UploadFile

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.file_store import FileStore
from omnigent.stores.project_ticket_store import ProjectTicketStore
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes

TICKET_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
MAX_TICKET_IMAGE_BYTES = 10 * 1024 * 1024
# Rows carry no session; this metadata key scopes them to their project.
PROJECT_METADATA_KEY = "ticket_project_id"


def ticket_attachment_content_path(project_id: str, file_id: str) -> str:
    return f"/v1/projects/{project_id}/ticket-attachments/{file_id}/content"


def create_project_ticket_attachments_router(
    store: ProjectTicketStore,
    file_store: FileStore,
    artifact_store: ArtifactStore,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/projects/{project_id}/ticket-attachments")

    async def require_project(request: Request, project_id: str) -> str:
        user_id = require_user(request, auth_provider)
        await asyncio.to_thread(store.require_project, project_id, user_id=user_id)
        return uuid_to_bytes(project_id).hex()

    @router.post("", status_code=201)
    async def upload_ticket_image(
        request: Request, project_id: str, file: UploadFile
    ) -> dict[str, Any]:
        project = await require_project(request, project_id)
        content_type = (file.content_type or "").split(";", 1)[0].strip().lower()
        if content_type not in TICKET_IMAGE_TYPES:
            raise OmnigentError(
                "Ticket attachments must be PNG, JPEG, GIF or WebP images",
                code=ErrorCode.INVALID_INPUT,
            )
        content = await file.read(MAX_TICKET_IMAGE_BYTES + 1)
        if len(content) > MAX_TICKET_IMAGE_BYTES:
            raise OmnigentError(
                f"Images must be at most {MAX_TICKET_IMAGE_BYTES // (1024 * 1024)} MB",
                code=ErrorCode.INVALID_INPUT,
            )
        if not content:
            raise OmnigentError("The image is empty", code=ErrorCode.INVALID_INPUT)
        filename = (file.filename or "image").strip()[:200] or "image"
        stored = await asyncio.to_thread(
            file_store.create,
            filename=filename,
            bytes=len(content),
            content_type=content_type,
            source_metadata={PROJECT_METADATA_KEY: project},
        )
        try:
            await asyncio.to_thread(artifact_store.put, stored.id, content)
        except Exception:
            await asyncio.to_thread(file_store.delete, stored.id)
            raise
        return {
            "id": stored.id,
            "filename": stored.filename,
            "bytes": stored.bytes,
            "content_type": content_type,
            "url": ticket_attachment_content_path(project, stored.id),
        }

    @router.get("/{file_id}/content")
    async def ticket_image_content(request: Request, project_id: str, file_id: str) -> Response:
        project = await require_project(request, project_id)
        try:
            file_id = uuid_to_bytes(file_id).hex()
        except InvalidUuidError as exc:
            raise OmnigentError("Attachment not found", code=ErrorCode.NOT_FOUND) from exc
        stored = await asyncio.to_thread(file_store.get, file_id)
        metadata = (stored.source_metadata or {}) if stored is not None else {}
        if (
            stored is None
            or stored.session_id is not None
            or metadata.get(PROJECT_METADATA_KEY) != project
            or stored.content_type not in TICKET_IMAGE_TYPES
        ):
            raise OmnigentError("Attachment not found", code=ErrorCode.NOT_FOUND)
        content = await asyncio.to_thread(artifact_store.get, stored.blob_key or stored.id)
        return Response(
            content,
            media_type=stored.content_type,
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router
