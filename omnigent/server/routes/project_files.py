"""Owner-private project files: documents, screenshots and evidence kept with a project.

Project files share the storage scheme of ticket image attachments: a
``FileStore`` row with no session, scoped by ``source_metadata`` under
:data:`PROJECT_METADATA_KEY`, and bytes in the ``ArtifactStore``. Ticket images
therefore appear in the project's file list too, and the two routes read the
same rows.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Form, Request, Response, UploadFile

from omnigent.entities import StoredFile
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes.project_ticket_attachments import PROJECT_METADATA_KEY
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.file_store import FileStore
from omnigent.stores.project_ticket_store import ProjectTicketStore
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes

MAX_PROJECT_FILE_BYTES = 50 * 1024 * 1024
# Types a browser may render inline. Anything else, notably HTML and SVG, downloads.
_INLINE_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf", "text/plain"}
)
# Text formats served as plain text so they preview without executing.
_TEXT_TYPES = frozenset({"text/markdown", "text/csv", "application/json", "text/x-log"})


def project_file_content_path(project_id: str, file_id: str) -> str:
    """Relative URL of a project file's bytes, also used as an ``artifact`` ticket output."""
    return f"/v1/projects/{project_id}/files/{file_id}/content"


def _hex(value: str, what: str) -> str:
    try:
        return uuid_to_bytes(value).hex()
    except InvalidUuidError as exc:
        raise OmnigentError(f"{what} not found", code=ErrorCode.NOT_FOUND) from exc


def _content_type(upload: UploadFile) -> str:
    content_type = (upload.content_type or "").split(";", 1)[0].strip().lower()
    filename = (upload.filename or "").lower()
    if not content_type or content_type == "application/octet-stream":
        if filename.endswith(".md"):
            return "text/markdown"
        if filename.endswith((".txt", ".log")):
            return "text/plain"
    return content_type or "application/octet-stream"


def _describe(project_id: str, stored: StoredFile) -> dict[str, Any]:
    metadata = stored.source_metadata or {}
    return {
        "id": stored.id,
        "filename": stored.filename,
        "title": metadata.get("title") or stored.filename,
        "bytes": stored.bytes,
        "content_type": stored.content_type,
        "created_at": stored.created_at,
        "kind": metadata.get("kind", "ticket_attachment"),
        "ticket_id": metadata.get("ticket_id"),
        "source_session_id": metadata.get("source_session_id"),
        "url": project_file_content_path(project_id, stored.id),
    }


def create_project_files_router(
    store: ProjectTicketStore,
    file_store: FileStore,
    artifact_store: ArtifactStore,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/projects/{project_id}/files")

    async def require_project(request: Request, project_id: str) -> str:
        user_id = require_user(request, auth_provider)
        await asyncio.to_thread(store.require_project, project_id, user_id=user_id)
        return uuid_to_bytes(project_id).hex()

    async def project_file(project: str, file_id: str) -> StoredFile:
        stored = await asyncio.to_thread(file_store.get, _hex(file_id, "File"))
        metadata = (stored.source_metadata or {}) if stored is not None else {}
        if stored is None or stored.session_id is not None:
            raise OmnigentError("File not found", code=ErrorCode.NOT_FOUND)
        if metadata.get(PROJECT_METADATA_KEY) != project:
            raise OmnigentError("File not found", code=ErrorCode.NOT_FOUND)
        return stored

    @router.post("", status_code=201)
    async def upload_project_file(
        request: Request,
        project_id: str,
        file: UploadFile,
        title: str | None = Form(default=None),
        ticket_id: str | None = Form(default=None),
    ) -> dict[str, Any]:
        project = await require_project(request, project_id)
        content = await file.read(MAX_PROJECT_FILE_BYTES + 1)
        if len(content) > MAX_PROJECT_FILE_BYTES:
            raise OmnigentError(
                f"Project files must be at most {MAX_PROJECT_FILE_BYTES // (1024 * 1024)} MB",
                code=ErrorCode.INVALID_INPUT,
            )
        if not content:
            raise OmnigentError("The file is empty", code=ErrorCode.INVALID_INPUT)
        metadata: dict[str, Any] = {PROJECT_METADATA_KEY: project, "kind": "artifact"}
        if title and title.strip():
            metadata["title"] = title.strip()[:200]
        if ticket_id:
            metadata["ticket_id"] = _hex(ticket_id, "Ticket")
        source = request.headers.get(ORCHESTRATION_SOURCE_HEADER)
        if source:
            metadata["source_session_id"] = _hex(source, "Session")
        filename = (file.filename or "file").strip()[:200] or "file"
        stored = await asyncio.to_thread(
            file_store.create,
            filename=filename,
            bytes=len(content),
            content_type=_content_type(file),
            source_metadata=metadata,
        )
        try:
            await asyncio.to_thread(artifact_store.put, stored.id, content)
        except Exception:
            await asyncio.to_thread(file_store.delete, stored.id)
            raise
        return _describe(project, stored)

    @router.get("")
    async def list_project_files(request: Request, project_id: str) -> dict[str, Any]:
        project = await require_project(request, project_id)
        rows = await asyncio.to_thread(file_store.list_by_metadata, PROJECT_METADATA_KEY, project)
        return {"data": [_describe(project, row) for row in rows]}

    @router.get("/{file_id}")
    async def get_project_file(request: Request, project_id: str, file_id: str) -> dict[str, Any]:
        project = await require_project(request, project_id)
        return _describe(project, await project_file(project, file_id))

    @router.get("/{file_id}/content")
    async def project_file_content(request: Request, project_id: str, file_id: str) -> Response:
        project = await require_project(request, project_id)
        stored = await project_file(project, file_id)
        content = await asyncio.to_thread(artifact_store.get, stored.blob_key or stored.id)
        media_type = stored.content_type or "application/octet-stream"
        if media_type in _TEXT_TYPES:
            media_type = "text/plain"
        disposition = "inline" if media_type in _INLINE_TYPES else "attachment"
        safe_name = stored.filename.replace('"', "").replace("\\", "")
        return Response(
            content,
            media_type=f"{media_type}; charset=utf-8"
            if media_type == "text/plain"
            else media_type,
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
                "Content-Disposition": f'{disposition}; filename="{safe_name}"',
                "Content-Security-Policy": "sandbox",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.delete("/{file_id}", status_code=204)
    async def delete_project_file(request: Request, project_id: str, file_id: str) -> Response:
        project = await require_project(request, project_id)
        stored = await project_file(project, file_id)
        await asyncio.to_thread(file_store.delete, stored.id)
        blob_key = stored.blob_key or stored.id
        if await asyncio.to_thread(file_store.is_blob_key_orphaned, blob_key):
            await asyncio.to_thread(artifact_store.delete, blob_key)
        return Response(status_code=204)

    return router
