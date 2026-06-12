"""Routes for per-session review comments.

Comments can be sent to the agent as a formatted message via the
``/comments/send`` endpoint.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, model_validator

from omnigent.entities import Comment
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_access
from omnigent.stores import ConversationStore
from omnigent.stores.comment_store import CommentStore
from omnigent.stores.permission_store import PermissionStore


def _format_message(comments: list[Comment]) -> str:
    """Format a list of comments into a human-readable message for the agent.

    Groups comments by file path (alphabetical) and sorts within each group
    by ``start_index`` ascending. Each bullet shows the anchor_content
    snippet when available plus the character range (start–end), so the
    agent can locate the relevant section without needing pre-computed line
    numbers.

    :param comments: The comments to format.
    :returns: A multi-line string suitable for posting to the agent.
    """
    by_path: dict[str, list[Comment]] = {}
    for c in comments:
        by_path.setdefault(c.path, []).append(c)

    lines = ["Please address the following review comments."]
    for path in sorted(by_path):
        lines.append("")
        lines.append(f"File: {path}")
        for c in sorted(by_path[path], key=lambda c: c.start_index):
            anchor = f'"{c.anchor_content.strip()}" ' if c.anchor_content else ""
            lines.append(f"• {anchor}(offset {c.start_index}–{c.end_index}): {c.body}")

    return "\n".join(lines)


# ── Request models ─────────────────────────────────────────────────────────────


class AddCommentRequest(BaseModel):
    """Request body for ``POST /sessions/{id}/comments``.

    :param path: File path relative to workspace root,
        e.g. ``"src/App.tsx"``.
    :param body: The comment text.
    :param start_index: 0-based absolute character offset (inclusive)
        within the file where the anchor range begins.
    :param end_index: 0-based absolute character offset (exclusive)
        within the file where the anchor range ends.
    :param anchor_content: Plain-text snapshot of the selected range, used
        to re-anchor the comment after file edits. ``None`` if not provided.
    """

    path: str
    body: str
    start_index: int
    end_index: int
    anchor_content: str | None = None

    @model_validator(mode="after")
    def _validate_range(self) -> AddCommentRequest:
        """Reject semantically invalid range field combinations.

        :returns: The validated request unchanged.
        :raises ValueError: If any range field is out of bounds or inconsistent.
        """
        if self.start_index < 0:
            raise ValueError("start_index must be >= 0")
        if self.end_index < self.start_index:
            raise ValueError("end_index must be >= start_index")
        return self


class UpdateCommentRequest(BaseModel):
    """Request body for ``PATCH /sessions/{id}/comments/{comment_id}``.

    :param status: New status, e.g. ``"addressed"``. ``None`` leaves
        it unchanged.
    :param body: New comment body. ``None`` leaves it unchanged.
    """

    status: str | None = None
    body: str | None = None


class SendCommentsRequest(BaseModel):
    """Request body for ``POST .../comments/send``.

    :param comment_ids: IDs of comments to send.
    :param instruction: Optional custom instruction prefix; defaults
        to the standard "Please address the following file review
        comments." header.
    """

    comment_ids: list[str]
    instruction: str | None = None


# ── Router factory ─────────────────────────────────────────────────────────────


def create_comments_router(
    store: CommentStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    conversation_store: ConversationStore | None = None,
) -> APIRouter:
    """Build the comments router.

    All routes are scoped to ``/sessions/{session_id}/comments``.

    When both ``permission_store`` and ``conversation_store`` are provided
    (multi-user mode), every handler enforces session-level access:
    read endpoints require ``LEVEL_READ``, mutating endpoints require
    ``LEVEL_EDIT``.

    :param store: The shared :class:`CommentStore` instance.
    :param auth_provider: Auth provider used to identify the requesting
        user. ``None`` in single-user mode (no attribution stored).
    :param permission_store: Permission store used to check session-level
        access grants. ``None`` disables permission enforcement.
    :param conversation_store: Conversation store used by the permission
        checker for sub-agent session delegation. Must be provided when
        ``permission_store`` is not ``None``.
    :returns: A configured :class:`APIRouter`.
    :raises ValueError: If ``permission_store`` is provided without
        ``conversation_store``.
    """
    if permission_store is not None and conversation_store is None:
        raise ValueError("conversation_store is required when permission_store is provided")
    router = APIRouter()

    async def _require_comment_author(
        user_id: str | None, comment_id: str, session_id: str
    ) -> None:
        """Enforce that the caller authored the comment they are mutating.

        Used to gate the author-only operations — editing a comment's
        ``body`` and deleting a comment — on top of the session-level
        ``LEVEL_EDIT`` gate, which callers MUST run first. A session
        collaborator with edit access can still mark *anyone's* comment
        addressed (a shared review-workflow action), but cannot rewrite or
        delete another user's comment.

        Comments with no recorded author (``created_by is None`` — legacy
        comments created before per-user attribution, or single-user mode)
        remain editable/deletable by any editor, since there is no author to
        protect. This helper is only invoked when permission enforcement is
        active (``permission_store`` set), so single-user mode never reaches
        it regardless.

        The synchronous store read is dispatched to a worker thread to keep
        the event loop unblocked, matching :func:`require_access`.

        :param user_id: The authenticated caller, e.g. ``"bob@example.com"``.
        :param comment_id: The comment being mutated, e.g. ``"a1b2c3d4-..."``.
        :param session_id: The owning session, e.g. ``"conv_abc123"``.
        :raises OmnigentError: 404 if the comment is not found in this
            session; 403 if the caller is not the comment's author.
        """
        comment = await asyncio.to_thread(store.get, comment_id, session_id)
        if comment is None:
            raise OmnigentError("Comment not found", code=ErrorCode.NOT_FOUND)
        if comment.created_by is not None and comment.created_by != user_id:
            raise OmnigentError(
                "Only the comment author can edit or delete this comment",
                code=ErrorCode.FORBIDDEN,
            )

    @router.post("/sessions/{session_id}/comments")
    async def add_comment(
        request: Request,
        session_id: str,
        body: AddCommentRequest,
    ) -> dict[str, Any]:
        """Create a new review comment.

        Requires ``LEVEL_EDIT`` on the session in multi-user mode.

        :param request: The incoming request, used to extract the user identity.
        :param session_id: The owning session, e.g. ``"conv_abc123"``.
        :param body: Comment payload including path, body text, and the
            two range fields (start_index, end_index).
        :returns: The created comment as a serialized dict.
        :raises OmnigentError: 401/403/404 if the user lacks edit permission.
        """
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None and conversation_store is not None:
            await require_access(
                user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
            )
        comment = store.add(
            conversation_id=session_id,
            path=body.path,
            body=body.body,
            start_index=body.start_index,
            end_index=body.end_index,
            anchor_content=body.anchor_content,
            created_by=user_id,
        )
        return asdict(comment)

    @router.get("/sessions/{session_id}/comments")
    async def list_comments(
        request: Request,
        session_id: str,
        path: str | None = None,
    ) -> list[dict[str, Any]]:
        """List comments for a session, optionally filtered by file.

        Requires ``LEVEL_READ`` on the session in multi-user mode.

        :param request: The incoming request, used to extract the user identity.
        :param session_id: The session to query, e.g. ``"conv_abc123"``.
        :param path: When provided, only return comments for this file,
            e.g. ``"src/App.tsx"``.
        :returns: List of serialized comment dicts.
        :raises OmnigentError: 401/403/404 if the user lacks read permission.
        """
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None and conversation_store is not None:
            await require_access(
                user_id, session_id, LEVEL_READ, permission_store, conversation_store
            )
        comments = store.list_for_conversation(session_id, path=path)
        return [asdict(c) for c in comments]

    @router.patch("/sessions/{session_id}/comments/{comment_id}")
    async def update_comment(
        request: Request,
        session_id: str,
        comment_id: str,
        body: UpdateCommentRequest,
    ) -> dict[str, Any]:
        """Update a comment's status and/or body text.

        Requires ``LEVEL_EDIT`` on the session in multi-user mode.
        Editing the ``body`` additionally requires the caller to be the
        comment's author: rewriting another user's comment is forbidden,
        while changing only the ``status`` (e.g. marking it ``"addressed"``)
        stays open to any editor as a shared review-workflow action.

        :param request: The incoming request, used to extract the user identity.
        :param session_id: The owning session, e.g. ``"conv_abc123"``.
        :param comment_id: The comment to update, e.g. ``"a1b2c3d4-..."``.
        :param body: Fields to update; ``None`` fields are left unchanged.
        :returns: The updated serialized comment.
        :raises OmnigentError: 401/403/404 if the user lacks edit permission,
             403 if a body edit is attempted on another user's comment,
            or 404 if the comment is not found.
        """
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None and conversation_store is not None:
            await require_access(
                user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
            )
            # Rewriting comment text is author-only; a status-only change is a
            # shared review-workflow action that any editor (and the agent's
            # update_comment tool) may perform.
            if body.body is not None:
                await _require_comment_author(user_id, comment_id, session_id)
        comment = store.update_comment(comment_id, session_id, status=body.status, body=body.body)
        if comment is None:
            raise OmnigentError("Comment not found", code=ErrorCode.NOT_FOUND)
        return asdict(comment)

    @router.delete("/sessions/{session_id}/comments/{comment_id}")
    async def delete_comment(
        request: Request,
        session_id: str,
        comment_id: str,
    ) -> dict[str, Any]:
        """Delete a comment.

        Requires ``LEVEL_EDIT`` on the session in multi-user mode, and
        additionally that the caller is the comment's author — one
        collaborator may not delete another user's comment.

        :param request: The incoming request, used to extract the user identity.
        :param session_id: The owning session, e.g. ``"conv_abc123"``.
        :param comment_id: The comment to delete, e.g. ``"a1b2c3d4-..."``.
        :returns: ``{"deleted": true}``.
        :raises OmnigentError: 401/403/404 if the user lacks edit permission,
            403 if the caller is not the comment's author, or 404 if the
            comment is not found or does not belong to this session.
        """
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None and conversation_store is not None:
            await require_access(
                user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
            )
            await _require_comment_author(user_id, comment_id, session_id)
        deleted = store.delete(comment_id, session_id)
        if deleted is None:
            raise OmnigentError("Comment not found", code=ErrorCode.NOT_FOUND)
        return {"deleted": True}

    @router.post("/sessions/{session_id}/comments/send")
    async def send_to_agent(
        request: Request,
        session_id: str,
        body: SendCommentsRequest,
    ) -> dict[str, Any]:
        """Mark comments as addressed and format them into an agent message.

        Fetches each requested comment, marks it ``addressed``,
        and formats the full set into a grouped, sorted message string
        suitable for pasting into the chat composer.

        Requires ``LEVEL_EDIT`` on the session in multi-user mode because
        it transitions comment status from ``draft`` to ``addressed``.

        :param request: The incoming request, used to extract the user identity.
        :param session_id: The owning session, e.g. ``"conv_abc123"``.
        :param body: List of comment IDs to send, with an optional
            custom instruction prefix.
        :returns: ``{"formatted_message": str, "sent_comment_ids": list[str]}``.
        :raises OmnigentError: 401/403/404 if the user lacks edit permission,
            or 404 if any requested comment is not found or does not belong to
            this session.
        """
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None and conversation_store is not None:
            await require_access(
                user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
            )

        # Fetch + mark-addressed for every requested comment runs N sync
        # DB gets + N sync updates. Do the whole batch in one worker-thread
        # hop so it never blocks the single-worker event loop (and can't
        # serialize concurrent requests behind it).
        def _fetch_and_mark() -> list[Comment]:
            """Resolve every comment id, then mark each addressed."""
            fetched: list[Comment] = []
            for cid in body.comment_ids:
                comment = store.get(cid, session_id)
                if comment is None:
                    raise OmnigentError(f"Comment not found: {cid}", code=ErrorCode.NOT_FOUND)
                fetched.append(comment)
            for comment in fetched:
                store.update_comment(comment.id, session_id, status="addressed")
            return fetched

        to_send = await asyncio.to_thread(_fetch_and_mark)

        formatted = _format_message(to_send)
        return {
            "formatted_message": formatted,
            "sent_comment_ids": [c.id for c in to_send],
        }

    return router
