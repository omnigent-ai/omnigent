"""Owner-private project files: documents, screenshots and evidence kept with a project.

Project files share the storage scheme of ticket image attachments: a
``FileStore`` row with no session, scoped by ``source_metadata`` under
:data:`PROJECT_METADATA_KEY`, and bytes in the ``ArtifactStore``. Ticket images
therefore appear in the project's file list too, and the two routes read the
same rows.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, Form, Request, Response, UploadFile
from pydantic import BaseModel, Field

from omnigent.db.enum_codecs import COMMENT_STATUS
from omnigent.entities import Conversation, StoredFile
from omnigent.entities.project import PROJECT_IDENTITY_LABEL_KEY
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.auth import AuthProvider
from omnigent.server.persona_mentions import person_display_name
from omnigent.server.routes._auth_helpers import attribution_user, require_user
from omnigent.server.routes.comments import AddCommentRequest, UpdateCommentRequest
from omnigent.server.routes.project_ticket_attachments import PROJECT_METADATA_KEY
from omnigent.server.session_orchestration import require_private_bound_source
from omnigent.stores import ConversationStore, ProjectStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.comment_store import CommentStore
from omnigent.stores.file_store import FileStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_ticket_store import ProjectTicketStore
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes

MAX_PROJECT_FILE_BYTES = 50 * 1024 * 1024
# Types a browser may render inline. Anything else, notably HTML and SVG, downloads.
_INLINE_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "application/pdf",
        "text/plain",
        "video/mp4",
        "video/webm",
    }
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
        "demo": bool(metadata.get("demo")),
        "url": project_file_content_path(project_id, stored.id),
    }


def create_project_files_router(
    store: ProjectTicketStore,
    file_store: FileStore,
    artifact_store: ArtifactStore,
    *,
    comment_store: CommentStore | None = None,
    auth_provider: AuthProvider | None = None,
    conversation_store: ConversationStore | None = None,
    permission_store: PermissionStore | None = None,
    project_store: ProjectStore | None = None,
) -> APIRouter:
    """Project file routes. A file's review comments use the file ID as their conversation key."""
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
        demo: bool | None = Form(default=None),
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
        # A saved video is a demo unless the saver says otherwise.
        if demo if demo is not None else _content_type(file).startswith("video/"):
            metadata["demo"] = True
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
    async def list_project_files(
        request: Request,
        project_id: str,
        since: float | None = None,
        ticket_id: str | None = None,
        kind: Literal["artifact", "demo", "ticket_attachment"] | None = None,
    ) -> dict[str, Any]:
        """The project's files, newest first, optionally filtered by time, ticket or kind."""
        project = await require_project(request, project_id)
        rows = await asyncio.to_thread(file_store.list_by_metadata, PROJECT_METADATA_KEY, project)
        files = [_describe(project, row) for row in rows]
        wanted_ticket = _hex(ticket_id, "Ticket") if ticket_id else None
        files = [
            item
            for item in files
            if (since is None or item["created_at"] >= since)
            and (wanted_ticket is None or item["ticket_id"] == wanted_ticket)
            and (
                kind is None
                or (item["demo"] if kind == "demo" else item["kind"] == kind)
            )
        ]
        return {"data": files}

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
        if comment_store is not None:
            await asyncio.to_thread(comment_store.remove_conversation, stored.id)
        blob_key = stored.blob_key or stored.id
        if await asyncio.to_thread(file_store.is_blob_key_orphaned, blob_key):
            await asyncio.to_thread(artifact_store.delete, blob_key)
        return Response(status_code=204)

    if comment_store is not None:
        agents = _CommentAgents(auth_provider, conversation_store, permission_store, project_store)
        _add_comment_routes(
            router, comment_store, require_project, project_file, auth_provider, agents, store
        )
    return router


class AddressFileComments(BaseModel):
    """Which open comments to send; omit to send all of the file's open comments."""

    comment_ids: list[str] | None = Field(default=None, max_length=200)


def _address_message(stored: StoredFile, rows: list[Any], user_id: str | None) -> str:
    """The turn that hands a file's open comments to an agent."""
    title = (stored.source_metadata or {}).get("title") or stored.filename
    who = person_display_name(user_id) if user_id else "Someone"
    lines = [
        f'{who} asked you to address {len(rows)} open comment(s) on the project file "{title}" '
        f"(file_id {stored.id}).",
        "",
    ]
    for number, row in enumerate(rows, 1):
        anchor = (row.anchor_content or "").strip().replace("\n", " ")[:200]
        author = person_display_name(row.created_by) if row.created_by else "someone"
        lines.append(f'{number}. On "{anchor}", {author} wrote: {row.body.strip()}')
    lines += [
        "",
        "For each one, decide what to do and act on it where you can. Then use "
        "sys_project_file_comments to reply with what you did or decided, and resolve it "
        "(comment_ids come from its list action). Project files can't be edited in place, "
        "so don't save a second copy of this file; if the text itself needs to change, say "
        "so in your reply.",
    ]
    return "\n".join(lines)


class _CommentAgents:
    """Who an agent's file-comment call acts as: its own session, in the file's project."""

    def __init__(
        self,
        auth_provider: AuthProvider | None,
        conversations: ConversationStore | None,
        permissions: PermissionStore | None,
        projects: ProjectStore | None,
    ) -> None:
        self.auth_provider = auth_provider
        self.conversations = conversations
        self.permissions = permissions
        self.projects = projects

    async def source(self, request: Request, project: str) -> Conversation | None:
        """The calling agent's session, or ``None`` for a person.

        An agent call must come from its own bound runner, in this file's project;
        otherwise it's refused rather than treated as the person it runs for.
        """
        if ORCHESTRATION_SOURCE_HEADER not in request.headers:
            return None
        if self.conversations is None or self.permissions is None:
            raise OmnigentError(
                "Agents can't use project file comments here", code=ErrorCode.FORBIDDEN
            )
        user_id = require_user(request, self.auth_provider)
        source = await asyncio.to_thread(
            require_private_bound_source, request, user_id, self.conversations, self.permissions
        )
        if source.archived or source.project_id != project:
            raise OmnigentError(
                "Agents can only use comments on their own project's files",
                code=ErrorCode.FORBIDDEN,
            )
        return source

    async def recipients(
        self, project: str, stored: StoredFile, tickets: ProjectTicketStore, user_id: str | None
    ) -> list[tuple[str, str]]:
        """Who a file's comments go to, in order: (name, session ID) pairs.

        The linked ticket's assignee, then the session that saved the file, then the Lead.
        """
        if self.projects is None:
            return []
        entity = await asyncio.to_thread(self.projects.get, project, user_id=user_id)
        if entity is None:
            return []
        identities = await asyncio.to_thread(
            self.projects.list_identities, project, user_id=user_id
        )
        found: list[tuple[str, str]] = []
        metadata = stored.source_metadata or {}
        ticket_id = metadata.get("ticket_id")
        if ticket_id:
            try:
                ticket = await asyncio.to_thread(tickets.get, project, ticket_id, user_id=user_id)
            except OmnigentError:
                ticket = None
            assignee = next(
                (
                    label.get("value")
                    for label in (ticket or {}).get("labels") or []
                    if label.get("key") == "assignee"
                ),
                None,
            )
            identity = next(
                (
                    item
                    for item in identities
                    if isinstance(assignee, str)
                    and item.name.casefold() == assignee.strip().casefold()
                ),
                None,
            )
            if identity is not None and identity.active_session_id:
                found.append((identity.name, identity.active_session_id))
        source = metadata.get("source_session_id")
        if isinstance(source, str) and source:
            found.append(("the agent that saved it", source))
        if entity.main_session_id:
            found.append(("Lead", entity.main_session_id))
        return list(dict.fromkeys(found))

    async def reachable(self, session_id: str, project: str) -> bool:
        """Whether a recipient session still exists, unarchived, in this project."""
        if self.conversations is None:
            return False
        conversation = await asyncio.to_thread(self.conversations.get_conversation, session_id)
        return (
            conversation is not None
            and not conversation.archived
            and conversation.project_id == project
        )

    async def author(self, source: Conversation, user_id: str | None) -> str:
        """How an agent's comment is signed: its role, marked as an agent."""
        name = "Agent"
        project = (
            await asyncio.to_thread(self.projects.get, source.project_id, user_id=user_id)
            if self.projects is not None and source.project_id
            else None
        )
        identity_id = (source.labels or {}).get(PROJECT_IDENTITY_LABEL_KEY)
        if project is not None and identity_id and self.projects is not None:
            identity = await asyncio.to_thread(
                self.projects.get_identity, project.id, identity_id, user_id=user_id
            )
            name = identity.name if identity is not None else name
        elif project is not None and source.id in {
            project.main_session_id,
            *project.retired_main_session_ids,
        }:
            name = "Lead"
        return f"{name} (agent)"


def _add_comment_routes(
    router: APIRouter,
    comments: CommentStore,
    require_project: Any,
    project_file: Any,
    auth_provider: AuthProvider | None,
    agents: _CommentAgents,
    tickets: ProjectTicketStore,
) -> None:
    """Review comments on a project file, in the session comments wire format.

    Agents may read, add and resolve comments, signed as themselves; only people
    may rewrite or delete one.
    """

    async def file_and_agent(
        request: Request, project_id: str, file_id: str
    ) -> tuple[str, Conversation | None]:
        project = await require_project(request, project_id)
        source = await agents.source(request, project)
        return (await project_file(project, file_id)).id, source

    async def owned_file(request: Request, project_id: str, file_id: str) -> str:
        return (await file_and_agent(request, project_id, file_id))[0]

    @router.get("/{file_id}/comments")
    async def list_project_file_comments(
        request: Request, project_id: str, file_id: str, path: str | None = None
    ) -> dict[str, Any]:
        key = await owned_file(request, project_id, file_id)
        rows = await asyncio.to_thread(comments.list_for_conversation, key, path=path)
        return {"comments": [asdict(row) for row in rows], "next_cursor": None}

    @router.post("/{file_id}/comments")
    async def add_project_file_comment(
        request: Request, project_id: str, file_id: str, body: AddCommentRequest
    ) -> dict[str, Any]:
        key, source = await file_and_agent(request, project_id, file_id)
        if body.anchor_metadata is not None:
            raise OmnigentError(
                "Project files take text comments only", code=ErrorCode.INVALID_INPUT
            )
        user_id = require_user(request, auth_provider)
        author = (
            await agents.author(source, user_id)
            if source is not None
            else attribution_user(user_id)
        )
        comment = await asyncio.to_thread(
            comments.add,
            conversation_id=key,
            path=body.path,
            body=body.body,
            start_index=body.start_index,
            end_index=body.end_index,
            anchor_content=body.anchor_content,
            created_by=author,
        )
        return asdict(comment)

    @router.patch("/{file_id}/comments/{comment_id}")
    async def update_project_file_comment(
        request: Request,
        project_id: str,
        file_id: str,
        comment_id: str,
        body: UpdateCommentRequest,
    ) -> dict[str, Any]:
        key, source = await file_and_agent(request, project_id, file_id)
        if source is not None and body.body is not None:
            raise OmnigentError(
                "Agents can resolve or reopen a comment but not rewrite it; reply instead",
                code=ErrorCode.FORBIDDEN,
            )
        if body.status is not None and body.status not in COMMENT_STATUS:
            raise OmnigentError(
                f"status must be one of {sorted(COMMENT_STATUS)}", code=ErrorCode.INVALID_INPUT
            )
        updated = await asyncio.to_thread(
            comments.update_comment, comment_id, key, status=body.status, body=body.body
        )
        if updated is None:
            raise OmnigentError("Comment not found", code=ErrorCode.NOT_FOUND)
        return asdict(updated)

    @router.post("/{file_id}/comments/address")
    async def address_project_file_comments(
        request: Request, project_id: str, file_id: str, body: AddressFileComments
    ) -> dict[str, Any]:
        """Send a file's open comments to the agent who should handle them.

        That's the linked ticket's assignee, else the session that saved the file,
        else the project's Lead. Only a person can send them.
        """
        project = await require_project(request, project_id)
        if await agents.source(request, project) is not None:
            raise OmnigentError("Only a person can send comments", code=ErrorCode.FORBIDDEN)
        stored = await project_file(project, file_id)
        user_id = require_user(request, auth_provider)
        rows = await asyncio.to_thread(comments.list_for_conversation, stored.id)
        wanted = set(body.comment_ids or [])
        open_rows = [
            row for row in rows if row.status == "draft" and (not wanted or row.id in wanted)
        ]
        if not open_rows:
            raise OmnigentError("There are no open comments to send", code=ErrorCode.INVALID_INPUT)
        recipients = await agents.recipients(project, stored, tickets, user_id)
        text = _address_message(stored, open_rows, user_id)
        stable_id = hashlib.md5(
            "|".join(
                [stored.id, *sorted(f"{row.id}:{row.updated_at}" for row in open_rows)]
            ).encode()
        ).hexdigest()
        notifications = getattr(request.app.state, "project_ticket_notifications", None)
        if notifications is None or agents.projects is None:
            raise OmnigentError("Agents can't be reached here", code=ErrorCode.RUNNER_UNAVAILABLE)
        project_entity = await asyncio.to_thread(agents.projects.get, project, user_id=user_id)
        for name, session_id in recipients:
            if not await agents.reachable(session_id, project):
                continue
            # The first existing recipient keeps the turn even while it's offline, so a
            # second agent doesn't answer the same comments when the first one returns.
            delivered = await notifications.deliver_to_session(
                request.app, project_entity, session_id, stable_id=stable_id, text=text
            )
            return {
                "sent": len(open_rows),
                "session_id": session_id,
                "recipient": name,
                "queued": not delivered,
            }
        raise OmnigentError(
            "No agent is available to take these comments; open the Lead and try again",
            code=ErrorCode.CONFLICT,
        )

    @router.delete("/{file_id}/comments/{comment_id}", status_code=204)
    async def delete_project_file_comment(
        request: Request, project_id: str, file_id: str, comment_id: str
    ) -> Response:
        key, source = await file_and_agent(request, project_id, file_id)
        if source is not None:
            raise OmnigentError(
                "Only a person can delete a comment; resolve it instead",
                code=ErrorCode.FORBIDDEN,
            )
        if await asyncio.to_thread(comments.delete, comment_id, key) is None:
            raise OmnigentError("Comment not found", code=ErrorCode.NOT_FOUND)
        return Response(status_code=204)
