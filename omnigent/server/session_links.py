"""Server authorization and delivery for owner-scoped session communication."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import httpx
from fastapi import Request
from starlette._utils import get_route_path
from starlette.exceptions import WebSocketException
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from omnigent.db.account_authority import account_authority_scope, account_generation
from omnigent.db.db_models import normalize_uuid
from omnigent.entities import Conversation, NewConversationItem
from omnigent.entities.conversation import MessageData
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.runner.routing import RunnerRouter
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, AuthProvider
from omnigent.server.credential_purpose import CredentialPurpose
from omnigent.server.permissions import check_session_access
from omnigent.server.schemas import SessionEventInput
from omnigent.session_authority import (
    INPUT_AUTHORITY_FIELD,
    LINKED_REQUEST_FIELD,
    REQUEST_AUTHORITY_HEADER,
    SOURCE_SESSION_HEADER,
    RequestAuthority,
    orchestration_scope,
    requester_authority_active,
)
from omnigent.stores import AgentStore, ArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.session_link_store import (
    Endpoint,
    InputIdentity,
    SessionLinkStore,
    payload_digest,
    payload_key,
    receipt_id,
)


class SessionOrchestrationMiddleware:
    def __init__(self, app: ASGIApp, *, enabled: bool) -> None:
        self.app = app
        self.enabled = enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        with orchestration_scope(self.enabled):
            await self.app(scope, receive, send)


def require_bound_runner(request: Request, endpoint: Endpoint) -> None:
    service = link_service(request)
    user = service.auth.get_user_id(request)
    purpose = service.auth.get_credential_purpose(request)
    token = request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER, "").strip()
    signed_runner = service.auth.get_runner_id(request)
    if (
        not token
        or not endpoint.runner_id
        or token_bound_runner_id(token) != endpoint.runner_id
        or purpose not in (CredentialPurpose.RUNNER, CredentialPurpose.CLIENT)
        or (purpose == CredentialPurpose.RUNNER and signed_runner != endpoint.runner_id)
        or user is None
        or (grant := service.permissions.get(user, endpoint.permission_id)) is None
        or grant.level != LEVEL_OWNER
    ):
        raise OmnigentError(
            "This operation requires the session's authenticated runner", code=ErrorCode.FORBIDDEN
        )


def link_service(request: Request) -> SessionLinkService:
    service = getattr(request.app.state, "session_links", None)
    if not isinstance(service, SessionLinkService):
        raise OmnigentError("Session links are not enabled", code=ErrorCode.NOT_FOUND)
    return service


async def bind_requester(connection: HTTPConnection) -> AsyncIterator[None]:
    """Resolve runner-attested consumed inputs before any route checks permissions."""
    request = Request(connection.scope) if connection.scope["type"] == "http" else None
    if request is None:
        if (
            REQUEST_AUTHORITY_HEADER in connection.headers
            or SOURCE_SESSION_HEADER in connection.headers
        ):
            raise WebSocketException(code=1008, reason="Requester context requires HTTP")
        yield
        return
    encoded = request.headers.get(REQUEST_AUTHORITY_HEADER)
    source_id = request.headers.get(SOURCE_SESSION_HEADER)
    if encoded is None and source_id is None:
        yield
        return
    if encoded is None or source_id is None or len(encoded) > 4096:
        raise OmnigentError("Invalid requester context", code=ErrorCode.FORBIDDEN)
    service = link_service(request)
    _, endpoint = await asyncio.to_thread(service.endpoint, source_id)
    require_bound_runner(request, endpoint)
    try:
        authority = RequestAuthority.from_wire(json.loads(encoded))
    except ValueError as exc:
        raise OmnigentError("Invalid requester context", code=ErrorCode.FORBIDDEN) from exc
    identity = await asyncio.to_thread(service.store.resolve_inputs, endpoint, authority)
    target_id = request.path_params.get("session_id")
    if (
        request.method not in ("GET", "HEAD", "OPTIONS")
        and target_id is not None
        and normalize_uuid(target_id) != endpoint.session_id
    ):
        target, _ = await asyncio.to_thread(service.endpoint, target_id)
        if target.parent_conversation_id != endpoint.session_id:
            raise OmnigentError(
                "Use the linked-request endpoint to send new work to an independent session",
                code=ErrorCode.FORBIDDEN,
            )
    # These routes all use the shared principal helpers. An unsupported endpoint
    # must not silently fall back to the runner owner's credentials.
    if not get_route_path(request.scope).startswith(
        (
            "/v1/sessions/",
            "/v1/session-links/",
            "/v1/hosts/",
            "/v1/scheduled-tasks/",
            "/v1/agents/",
            "/v1/mcp/",
        )
    ) and get_route_path(request.scope) not in (
        "/v1/sessions",
        "/v1/hosts",
        "/v1/scheduled-tasks",
        "/v1/agents",
    ):
        raise OmnigentError(
            "This endpoint does not support delegated identity", code=ErrorCode.FORBIDDEN
        )
    request.state.link_identity = identity
    request.state.link_source_id = endpoint.session_id
    request.state.link_authority = authority
    token = requester_authority_active.set(True)
    try:
        with account_authority_scope(identity.requester, identity.account_generation):
            yield
    finally:
        requester_authority_active.reset(token)


class SessionLinkService:
    def __init__(
        self,
        conversations: SqlAlchemyConversationStore,
        permissions: PermissionStore,
        artifacts: ArtifactStore,
        agents: AgentStore,
        runners: RunnerRouter,
        auth: AuthProvider,
    ) -> None:
        self.conversations = conversations
        self.permissions = permissions
        self.artifacts = artifacts
        self.agents = agents
        self.runners = runners
        self.auth = auth
        self.store = SessionLinkStore(permissions.storage_location)

    def endpoint(
        self, session_id: str, *, allow_archived: bool = False
    ) -> tuple[Conversation, Endpoint]:
        normalized = normalize_uuid(session_id)
        if normalized is None:
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
        conversation = self.conversations.get_conversation(normalized)
        if conversation is None or (conversation.archived and not allow_archived):
            raise OmnigentError("Session not available", code=ErrorCode.NOT_FOUND)
        root = conversation
        seen: set[str] = set()
        while root.parent_conversation_id:
            if root.id in seen or len(seen) >= 32:
                raise OmnigentError("Invalid session ancestry", code=ErrorCode.FORBIDDEN)
            seen.add(root.id)
            parent = self.conversations.get_conversation(root.parent_conversation_id)
            if parent is None or (parent.archived and not allow_archived):
                raise OmnigentError("Session not available", code=ErrorCode.NOT_FOUND)
            root = parent
        return conversation, Endpoint(conversation.id, root.id, conversation.runner_id)

    def owner_client(self, request: Request) -> str:
        user = self.auth.get_user_id(request)
        if user is None:
            raise OmnigentError(
                "Sign in to manage session communication", code=ErrorCode.UNAUTHORIZED
            )
        if (
            self.auth.get_credential_purpose(request)
            not in (CredentialPurpose.SESSION, CredentialPurpose.CLIENT)
            or request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER)
            or getattr(request.state, "link_identity", None) is not None
        ):
            raise OmnigentError(
                "Use owner UI or CLI credentials to change this setting", code=ErrorCode.FORBIDDEN
            )
        return user

    @staticmethod
    def require_child_creation(request: Request, parent_id: str | None) -> None:
        source = getattr(request.state, "link_source_id", None)
        if source is not None and normalize_uuid(parent_id) != source:
            raise OmnigentError(
                "Delegated tools can only create children of their source session",
                code=ErrorCode.FORBIDDEN,
            )

    def runtime(self, request: Request, source_id: str) -> InputIdentity:
        identity = getattr(request.state, "link_identity", None)
        if not isinstance(identity, InputIdentity) or getattr(
            request.state, "link_source_id", None
        ) != normalize_uuid(source_id):
            raise OmnigentError("No verified requester for this session", code=ErrorCode.FORBIDDEN)
        return identity

    def capture_input(self, request: Request, session_id: str, body: SessionEventInput) -> None:
        if body.type != "message":
            return
        body._trusted_transport[INPUT_AUTHORITY_FIELD] = None
        _, endpoint = self.endpoint(session_id)
        identity = getattr(request.state, "link_identity", None)
        if isinstance(identity, InputIdentity):
            # Existing child sends inherit the original human as well.
            if len(identity.parent_request_ids) > 1:
                raise OmnigentError("Ambiguous request ancestry", code=ErrorCode.FORBIDDEN)
            requester = identity.requester
            generation = identity.account_generation
            parent = next(iter(identity.parent_request_ids), None)
        else:
            if request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) or self.auth.get_credential_purpose(
                request
            ) not in (CredentialPurpose.SESSION, CredentialPurpose.CLIENT):
                return
            requester = self.auth.get_user_id(request)
            generation = account_generation(requester) if requester else None
            parent = None
        if requester is not None and endpoint.runner_id:
            body._trusted_transport[INPUT_AUTHORITY_FIELD] = self.store.capture_input(
                endpoint, requester, generation, parent_request_id=parent
            )

    def list_activity(
        self, source_id: str, user: str, *, incoming: bool, after: str | None, limit: int
    ) -> dict[str, Any]:
        conversation, _ = self.endpoint(source_id, allow_archived=True)
        if not check_session_access(
            user, conversation.id, LEVEL_READ, self.permissions, self.conversations
        ):
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
        rows, cursor = self.store.list_activity(
            conversation.id, incoming=incoming, after=after, limit=limit
        )
        visible = []
        for row in rows:
            try:
                source, _ = self.endpoint(row["source_id"], allow_archived=True)
                target, _ = self.endpoint(row["target_id"], allow_archived=True)
            except OmnigentError:
                continue
            if all(
                check_session_access(
                    user, endpoint.id, LEVEL_READ, self.permissions, self.conversations
                )
                for endpoint in (source, target)
            ):
                visible.append({**row, "source_title": source.title, "target_title": target.title})
        return {"data": visible, "next_cursor": cursor}

    async def delegate(
        self,
        source_id: str,
        target_id: str,
        identity: InputIdentity,
        operation_id: str,
        message: str,
    ) -> dict[str, Any]:
        _, source = await asyncio.to_thread(self.endpoint, source_id)
        _, target = await asyncio.to_thread(self.endpoint, target_id)
        digest = payload_digest(message)
        identifier = receipt_id(source.session_id, operation_id)
        await asyncio.to_thread(self.store.check_link, source, target, identity)
        await asyncio.to_thread(
            self.artifacts.put, payload_key("requests", identifier, digest), message.encode()
        )
        receipt, _ = await asyncio.to_thread(
            self.store.create_request, source, target, identity, operation_id, digest
        )
        if await asyncio.to_thread(self.store.begin_handoff, receipt["id"]):
            try:
                routed = await asyncio.to_thread(
                    self.runners.client_for_existing_conversation, target.session_id
                )
                if routed is None:
                    await asyncio.to_thread(self.store.settle_handoff, receipt["id"])
                else:
                    response = await routed.client.post(
                        f"/v1/sessions/{target.session_id}/events",
                        json={"type": "linked_request", LINKED_REQUEST_FIELD: receipt["id"]},
                        timeout=45,
                    )
                    if response.is_error:
                        await asyncio.to_thread(self.store.settle_handoff, receipt["id"])
            except (httpx.HTTPError, OmnigentError):
                await asyncio.to_thread(self.store.settle_handoff, receipt["id"])
            await asyncio.to_thread(self.store.settle_handoff, receipt["id"])
            receipt = await asyncio.to_thread(self.store.get_request, receipt["id"])
        assert receipt is not None
        return self.public_receipt(receipt)

    async def accept(self, request: Request, request_id: str, target_id: str) -> dict[str, Any]:
        receipt = await asyncio.to_thread(self.store.get_request, request_id)
        if receipt is None or normalize_uuid(target_id) != receipt["target_id"]:
            raise OmnigentError("Request not available", code=ErrorCode.NOT_FOUND)
        _, source = await asyncio.to_thread(self.endpoint, receipt["source_id"])
        conversation, target = await asyncio.to_thread(self.endpoint, receipt["target_id"])
        require_bound_runner(request, target)
        input_id = await asyncio.to_thread(self.store.accept_request, request_id, source, target)
        if input_id is None:
            raise OmnigentError("Request was already claimed or expired", code=ErrorCode.CONFLICT)
        accepted = False
        try:
            payload = await asyncio.to_thread(self.artifacts.get, receipt["payload_key"])
            message = (
                f"Linked request {request_id} from session {source.session_id} "
                f"on behalf of {receipt['requester']}:\n\n{payload.decode()}\n\n"
                f'Use sys_session_reply(request_id="{request_id}", message=...) '
                "to send an addressed reply. Ordinary output stays in this session."
            )
            event = SessionEventInput(
                type="message",
                data={
                    "role": "user",
                    "content": [{"type": "input_text", "text": message}],
                    "stable_id": request_id,
                },
            )
            event._trusted_transport[INPUT_AUTHORITY_FIELD] = input_id
            event._trusted_transport[LINKED_REQUEST_FIELD] = request_id
            from omnigent.server.routes._sessions.orchestration import (
                _dispatch_session_event_to_runner,
                _load_agent_spec_for_session,
            )

            routed = await asyncio.to_thread(
                self.runners.client_for_existing_conversation, target.session_id
            )
            if routed is None:
                raise OmnigentError("Worker runner is unavailable", code=ErrorCode.CONFLICT)
            agent = (
                await asyncio.to_thread(self.agents.get, conversation.agent_id)
                if conversation.agent_id
                else None
            )
            spec = await asyncio.to_thread(_load_agent_spec_for_session, conversation, self.agents)
            with account_authority_scope(receipt["requester"], receipt["account_generation"]):
                await _dispatch_session_event_to_runner(
                    target.session_id,
                    conversation,
                    event,
                    self.conversations,
                    routed.client,
                    file_store=None,
                    artifact_store=self.artifacts,
                    agent_name=agent.name if agent else None,
                    has_mcp_servers=bool(spec and spec.mcp_servers),
                    created_by=receipt["requester"],
                    runner_router=self.runners,
                    # This path requires an existing live worker; it never wakes a host.
                    native_terminal_ready=True,
                )
            accepted = True
        finally:
            await asyncio.to_thread(self.store.confirm_handoff, request_id, accepted=accepted)
        return {"accepted": True, "request_id": request_id}

    async def reply(
        self,
        worker_id: str,
        request_id: str,
        identity: InputIdentity,
        operation_id: str,
        message: str,
    ) -> dict[str, Any]:
        receipt = await asyncio.to_thread(self.store.get_request, request_id)
        if receipt is None or receipt["target_id"] != normalize_uuid(worker_id):
            raise OmnigentError("Request not available", code=ErrorCode.NOT_FOUND)
        _, source = await asyncio.to_thread(self.endpoint, receipt["source_id"])
        worker, target = await asyncio.to_thread(self.endpoint, worker_id)
        digest = payload_digest(message)
        reply_id = receipt_id(request_id, operation_id)
        await asyncio.to_thread(
            self.artifacts.put, payload_key("replies", reply_id, digest), message.encode()
        )
        item = NewConversationItem(
            type="message",
            response_id=request_id,
            stable_id=reply_id,
            data=MessageData(
                role="assistant",
                agent=worker.title or worker.id,
                content=[
                    {
                        "type": "output_text",
                        "text": (
                            f"Reply from session {worker.id} · request {request_id}\n\n{message}"
                        ),
                    }
                ],
            ),
        )
        write = await asyncio.to_thread(
            self.conversations.prepare_append, source.session_id, [item]
        )
        persisted = []

        def publish(transaction):
            persisted.extend(self.conversations.append_under_authority(transaction, write))

        result = await asyncio.to_thread(
            self.store.publish_reply,
            request_id,
            source,
            target,
            identity,
            operation_id,
            digest,
            publish,
        )
        from omnigent.server.routes._sessions.helpers import _publish_external_conversation_item

        for copied in persisted:
            _publish_external_conversation_item(source.session_id, copied)
        routed = await asyncio.to_thread(
            self.runners.client_for_existing_conversation, source.session_id
        )
        if routed is not None:
            # The durable copy and poll survive an offline manager.
            with suppress(httpx.HTTPError):
                await routed.client.post(
                    f"/v1/sessions/{source.session_id}/events",
                    json={
                        "type": "linked_reply",
                        "reply_id": result["reply_id"],
                        "request_id": request_id,
                        "worker_id": worker.id,
                        "message": message,
                    },
                    timeout=5,
                )
        return result

    async def poll(
        self,
        source_id: str,
        request_id: str,
        identity: InputIdentity,
        *,
        after: str | None,
        limit: int,
    ) -> dict[str, Any]:
        receipt = await asyncio.to_thread(self.store.get_request, request_id)
        if receipt is None or normalize_uuid(source_id) != receipt["source_id"]:
            raise OmnigentError("Request not available", code=ErrorCode.NOT_FOUND)
        _, source = await asyncio.to_thread(self.endpoint, source_id)
        worker, target = await asyncio.to_thread(self.endpoint, receipt["target_id"])
        receipt = await asyncio.to_thread(
            self.store.check_request, request_id, source, target, identity
        )
        replies, cursor = await asyncio.to_thread(
            self.store.list_replies, request_id, after=after, limit=limit
        )
        data = []
        for reply in replies:
            payload = await asyncio.to_thread(self.artifacts.get, reply["payload_key"])
            data.append(
                {
                    "reply_id": reply["id"],
                    "created_at": reply["created_at"],
                    "message": payload.decode(),
                }
            )
        return {
            **self.public_receipt(receipt),
            "replies": data,
            "next_cursor": cursor,
            "worker_status": worker.live_status,
        }

    @staticmethod
    def public_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
        return {
            key: receipt[key]
            for key in (
                "id",
                "source_id",
                "target_id",
                "delivery_state",
                "created_at",
                "accepted_at",
            )
        }
