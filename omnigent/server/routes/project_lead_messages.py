"""A project's Lead messages another project's Lead, and that Lead replies.

Sending uses the intake rule: the target project is owned by or shared with the
person whose agent calls. The message becomes a turn on the target's router,
marked with the sending project; the sender learns nothing else about the target.
A reply goes back to the sending project's current router through the thread the
first message opened, so the sender's project needn't be shared back.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnigent.entities.project import Project
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.auth import AuthProvider
from omnigent.server.persona_mentions import MentionRateLimiter
from omnigent.server.role_side_chat import refuse_role_side_chat
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.session_orchestration import require_named_source_private
from omnigent.stores import ConversationStore, PermissionStore, ProjectStore
from omnigent.tools.builtins.session_message import SESSION_MESSAGE_MAX_CHARS
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes

# Messages one project may send another per rolling hour, counted per direction.
LEAD_MESSAGES_PER_HOUR = 10
# How long the receiving Lead may reply to a message.
THREAD_TTL_SECONDS = 7 * 24 * 3600
_MAX_THREADS = 10_000


class LeadMessage(BaseModel):
    """A message from one project's Lead to another's, or a reply to one."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=SESSION_MESSAGE_MAX_CHARS)

    @field_validator("message")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


@dataclass(frozen=True)
class LeadThread:
    """Who may answer a delivered message, and where the answer goes."""

    sender_project_id: str
    sender_owner_id: str
    recipient_project_id: str
    created_at: float


class LeadThreads:
    """Messages still open for a reply, oldest dropped first past the cap or TTL."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._threads: OrderedDict[str, LeadThread] = OrderedDict()

    def open(self, sender: Project, recipient_project_id: str) -> str:
        message_id = uuid.uuid4().hex
        self._threads[message_id] = LeadThread(
            sender.id, sender.user_id, recipient_project_id, self._clock()
        )
        while len(self._threads) > _MAX_THREADS:
            self._threads.popitem(last=False)
        return message_id

    def get(self, message_id: str) -> LeadThread | None:
        thread = self._threads.get(message_id)
        if thread is None:
            return None
        if self._clock() - thread.created_at >= THREAD_TTL_SECONDS:
            self._threads.pop(message_id, None)
            return None
        return thread

    def take(self, message_id: str, recipient_project_id: str) -> LeadThread | None:
        """Claim a message for its one reply; only the project it went to may claim it."""
        thread = self.get(message_id)
        if thread is None or thread.recipient_project_id != recipient_project_id:
            return None
        return self._threads.pop(message_id)

    def restore(self, message_id: str, thread: LeadThread) -> None:
        """Reopen a message whose reply couldn't be sent."""
        self._threads[message_id] = thread


_threads = LeadThreads()
_limiter = MentionRateLimiter(limit=LEAD_MESSAGES_PER_HOUR, window_seconds=3600)


def lead_message_text(
    sender: Project, sender_session_id: str, message_id: str, message: str, *, reply: bool
) -> str:
    """The turn the receiving Lead sees: who sent it, the words, and how to answer."""
    kind = "Reply" if reply else "Message"
    return (
        f"[{kind} from the {sender.name} project's Lead (session {sender_session_id})]\n"
        f"{message}\n\n"
        f'Answer once with sys_project_lead_message(reply_to="{message_id}", message=...). '
        f"This gives you no access to {sender.name}'s board or sessions, and its Lead none to "
        "yours. Your own project's rules apply: answer, file a ticket on your own board, or "
        "decline. For work it wants done, it can file an intake ticket instead."
    )


def create_project_lead_messages_router(
    conversations: ConversationStore,
    projects: ProjectStore,
    *,
    auth_provider: AuthProvider | None = None,
    permissions: PermissionStore | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/projects/{project_id}/lead-messages")

    async def calling_lead(request: Request, action: str) -> tuple[str, Any, Project]:
        """The caller's user, session and project; it must be that project's router."""
        from omnigent.server.project_router import router_session_ids

        user_id = require_user(request, auth_provider)
        raw_source = request.headers.get(ORCHESTRATION_SOURCE_HEADER)
        if raw_source is None:
            raise OmnigentError(
                "Lead messages come from a project's Lead; people use the board or chat",
                code=ErrorCode.FORBIDDEN,
            )
        await asyncio.to_thread(
            refuse_role_side_chat,
            request,
            action=action,
            user_id=user_id,
            conversations=conversations,
            projects=projects,
        )
        await asyncio.to_thread(
            require_named_source_private, request, user_id, conversations, permissions
        )
        try:
            source_id = uuid_to_bytes(raw_source).hex()
        except InvalidUuidError:
            raise OmnigentError("Invalid calling session", code=ErrorCode.FORBIDDEN) from None
        source = await asyncio.to_thread(conversations.get_conversation, source_id)
        if source is None or source.archived or not source.project_id:
            raise OmnigentError(
                "Only a project's Lead can message another project's Lead",
                code=ErrorCode.FORBIDDEN,
            )
        project = await asyncio.to_thread(projects.get, source.project_id, user_id=user_id)
        if project is None or source.id not in await router_session_ids(request.app, project):
            raise OmnigentError(
                "Only a project's Lead can message another project's Lead",
                code=ErrorCode.FORBIDDEN,
            )
        return user_id, source, project

    async def deliver(
        request: Request,
        sender: Project,
        sender_session_id: str,
        recipient: Project,
        message: str,
        *,
        reply: bool,
    ) -> dict[str, Any]:
        from omnigent.server.project_router import router_session_id

        if not _limiter.allow(sender.id, recipient.id):
            raise OmnigentError(
                f"{sender.name} already sent {LEAD_MESSAGES_PER_HOUR} messages to this "
                "project's Lead in the last hour; try again later or file an intake ticket",
                code=ErrorCode.QUEUE_FULL,
            )
        # Never restore the other project's sessions with this caller's identity.
        target = await router_session_id(request.app, recipient, restore=False)
        notifications = getattr(request.app.state, "project_ticket_notifications", None)
        if target is None or notifications is None:
            raise OmnigentError(
                "That project has no Lead session to receive messages yet",
                code=ErrorCode.NOT_FOUND,
            )
        message_id = _threads.open(sender, recipient.id)
        delivered = await notifications.deliver_to_session(
            request.app,
            recipient,
            target,
            stable_id=message_id,
            text=lead_message_text(sender, sender_session_id, message_id, message, reply=reply),
        )
        return {
            "object": "project.lead_message",
            "id": message_id,
            "project_id": recipient.id,
            "delivered": delivered,
        }

    @router.post("", status_code=201)
    async def send_lead_message(
        request: Request, project_id: str, body: LeadMessage
    ) -> dict[str, Any]:
        """Send a message from the calling project's Lead to this project's Lead.

        For a project owned by or shared with the person whose Lead calls. The
        message is delivered to this project's router marked with the sending
        project, and the response returns only its ID, for matching a reply. Each
        project may send :data:`LEAD_MESSAGES_PER_HOUR` per hour to one project.
        """
        user_id, source, sender = await calling_lead(request, "Messaging another project")
        try:
            target_id = uuid_to_bytes(project_id).hex()
        except InvalidUuidError:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND) from None
        if target_id == sender.id:
            raise OmnigentError(
                "This is your own project; use sys_session_message", code=ErrorCode.INVALID_INPUT
            )
        recipient = await asyncio.to_thread(projects.get, target_id, user_id=user_id)
        if recipient is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return await deliver(request, sender, source.id, recipient, body.message, reply=False)

    @router.post("/{message_id}/reply", status_code=201)
    async def reply_to_lead_message(
        request: Request, project_id: str, message_id: str, body: LeadMessage
    ) -> dict[str, Any]:
        """Reply, as this project's Lead, to a message another project's Lead sent it.

        The reply goes to the sending project's current Lead, whether or not that
        project is shared with this caller. Each message takes one reply, which can
        itself be replied to.
        """
        _, source, own = await calling_lead(request, "Replying to another project")
        try:
            path_project_id = uuid_to_bytes(project_id).hex()
            thread_id = uuid_to_bytes(message_id).hex()
        except InvalidUuidError:
            raise OmnigentError("Message not found", code=ErrorCode.NOT_FOUND) from None
        if path_project_id != own.id:
            raise OmnigentError(
                "Reply from the project the message was sent to", code=ErrorCode.FORBIDDEN
            )
        thread = _threads.take(thread_id, own.id)
        if thread is None:
            raise OmnigentError(
                "No message to reply to: it was already answered, expired or went to another "
                "project. Send a new message, or file an intake ticket",
                code=ErrorCode.NOT_FOUND,
            )
        try:
            recipient = await asyncio.to_thread(
                projects.get, thread.sender_project_id, user_id=thread.sender_owner_id
            )
            if recipient is None:
                raise OmnigentError(
                    "The sending project no longer exists", code=ErrorCode.NOT_FOUND
                )
            return await deliver(request, own, source.id, recipient, body.message, reply=True)
        except OmnigentError:
            _threads.restore(thread_id, thread)
            raise

    return router
