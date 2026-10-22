"""Owner-gated messages using existing preferences, inputs and delivery."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import jwt
from fastapi import Request
from sqlalchemy.exc import IntegrityError

from omnigent.db.account_authority import account_generation, require_active_account
from omnigent.db.db_models import SqlPreference, current_workspace_id, normalize_uuid
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker
from omnigent.entities import Conversation, NewConversationItem
from omnigent.entities.conversation import ResourceEventData
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, AuthProvider
from omnigent.server.credential_purpose import CredentialPurpose
from omnigent.server.permissions import check_session_access
from omnigent.server.schemas import SessionEventInput
from omnigent.session_authority import (
    INPUT_AUTHORITY_FIELD,
    INPUT_PROOF_FIELD,
    REQUEST_AUTHORITY_HEADER,
    SOURCE_SESSION_HEADER,
    RequestAuthority,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store import PermissionStore

_SETTING_KEY = "session_messaging.v1"


def refused(message: str) -> OmnigentError:
    return OmnigentError(message, code=ErrorCode.FORBIDDEN)


class SessionMessaging:
    def __init__(
        self,
        conversations: SqlAlchemyConversationStore,
        permissions: PermissionStore,
        auth: AuthProvider,
    ) -> None:
        self.conversations, self.permissions, self.auth = conversations, permissions, auth
        key = auth.message_authority_key()
        if key is None:
            raise ValueError("Session messaging requires Accounts/OIDC credential provenance")
        self.key = key
        self.session = make_named_managed_session_maker(
            get_or_create_engine(permissions.storage_location),
            query_name_prefix="omnigent.session_messaging",
            immediate=True,
        )

    def owner_client(self, request: Request) -> str:
        user = self.auth.get_user_id(request)
        if (
            not user
            or self.auth.get_credential_purpose(request)
            not in (
                CredentialPurpose.SESSION,
                CredentialPurpose.CLIENT,
            )
            or request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER)
        ):
            raise refused("Use owner UI or CLI credentials to change session communication")
        return user

    def setting(
        self, user: str, *, enabled: bool | None = None, expected_version: str | None = None
    ) -> dict[str, Any]:
        try:
            with self.session("read_setting" if enabled is None else "change_setting") as session:
                require_active_account(session, user)
                row = session.get(
                    SqlPreference,
                    (current_workspace_id(), user, _SETTING_KEY),
                    with_for_update=enabled is not None,
                )
                current = json.loads(row.value) if row else {"enabled": False, "version": None}
                if enabled is None:
                    return current
                if current["version"] != expected_version:
                    raise OmnigentError(
                        "Setting changed; reload before trying again", code=ErrorCode.CONFLICT
                    )
                value = {"enabled": enabled, "version": uuid4().hex}
                if row is None:
                    session.add(
                        SqlPreference(
                            workspace_id=current_workspace_id(),
                            user_id=user,
                            key=_SETTING_KEY,
                            value=json.dumps(value),
                        )
                    )
                else:
                    row.value = json.dumps(value)
                return value
        except IntegrityError as exc:
            raise OmnigentError(
                "Setting changed; reload before trying again", code=ErrorCode.CONFLICT
            ) from exc

    def endpoint(self, identifier: str) -> tuple[Conversation, str]:
        session_id = normalize_uuid(identifier)
        conversation = self.conversations.get_conversation(session_id) if session_id else None
        if conversation is None or conversation.archived:
            raise refused("Session is not available")
        root, seen = conversation, set()
        while root.parent_conversation_id:
            if root.id in seen or len(seen) >= 32:
                raise refused("Invalid session ancestry")
            seen.add(root.id)
            parent = self.conversations.get_conversation(root.parent_conversation_id)
            if parent is None or parent.archived:
                raise refused("Session is not available")
            root = parent
        return conversation, root.id

    def require_runner(self, request: Request, source_id: str) -> Conversation:
        source, root = self.endpoint(source_id)
        user = self.auth.get_user_id(request)
        token = request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER, "").strip()
        purpose = self.auth.get_credential_purpose(request)
        grant = self.permissions.get(user, root) if user else None
        if (
            not token
            or not source.runner_id
            or token_bound_runner_id(token) != source.runner_id
            or (
                purpose not in (CredentialPurpose.CLIENT, CredentialPurpose.RUNNER)
                or (
                    purpose == CredentialPurpose.RUNNER
                    and self.auth.get_runner_id(request) != source.runner_id
                )
                or grant is None
                or grant.level != LEVEL_OWNER
            )
        ):
            raise refused("Messaging requires the source session's authenticated runner")
        return source

    def sign(self, data: dict[str, Any], purpose: str = "input") -> str:
        return jwt.encode(
            {
                **data,
                "aud": "session-messaging-" + purpose,
                "workspace_id": current_workspace_id(),
                "version": 1,
            },
            self.key,
            algorithm="HS256",
        )

    def verify(self, proof: str, purpose: str = "input") -> dict[str, Any]:
        try:
            claims = jwt.decode(
                proof, self.key, algorithms=["HS256"], audience="session-messaging-" + purpose
            )
            if claims.get("workspace_id") != current_workspace_id() or claims.get("version") != 1:
                raise ValueError("Wrong message scope")
            return claims
        except (jwt.InvalidTokenError, ValueError) as exc:
            raise refused(
                "Message requester could not be verified; send fresh authenticated input"
            ) from exc

    def resolve(
        self, request: Request, source_id: str, authority: RequestAuthority | None = None
    ) -> dict[str, Any]:
        source = self.require_runner(request, source_id)
        if authority is None:
            raw = request.headers.get(REQUEST_AUTHORITY_HEADER, "")
            if len(raw) > 65536 or request.headers.get(SOURCE_SESSION_HEADER) != source.id:
                raise refused("Invalid message requester context")
            try:
                authority = RequestAuthority.from_wire(json.loads(raw))
            except ValueError as exc:
                raise refused("Missing message requester context") from exc
        if (
            not authority.verified
            or not authority.input_ids
            or len(authority.input_ids) != len(authority.proofs)
        ):
            raise refused("No verified requester for this turn")
        inputs = [self.verify(proof) for proof in authority.proofs]
        for input_id, claims in zip(authority.input_ids, inputs, strict=True):
            if (
                claims.get("input_id") != input_id
                or claims.get("session_id") != source.id
                or claims.get("runner_id") != source.runner_id
            ):
                raise refused("Message authority belongs to another input or runner")
        if len({(entry["requester_id"], entry.get("generation")) for entry in inputs}) != 1:
            raise refused("This turn consumed input from different users; send a fresh request")
        identity = {**inputs[0], "hop_count": max(entry["hop_count"] for entry in inputs)}
        with self.session("verify_requester") as session:
            require_active_account(
                session, identity["requester_id"], generation=identity.get("generation")
            )
        return identity

    def check_send(self, source_id: str, target_id: str, identity: dict[str, Any]) -> None:
        if source_id == target_id or identity["hop_count"] >= 8:
            raise refused("Message destination or chain limit is invalid")
        for identifier in (source_id, target_id):
            _, root = self.endpoint(identifier)
            grant = self.permissions.get(identity["requester_id"], root)
            if grant is None or grant.level != LEVEL_OWNER:
                raise refused("The original requester must currently own both sessions")
        if not self.setting(identity["requester_id"])["enabled"]:
            raise refused("Session communication is Off; enable it in session settings")

    def prepare_send(self, request: Request, target_id: str, body: SessionEventInput) -> None:
        source_id = request.headers.get(SOURCE_SESSION_HEADER, "")
        identity = self.resolve(request, source_id)
        self.check_send(source_id, target_id, identity)
        message = body.data.get("message")
        if (
            not isinstance(message, str)
            or not message.strip()
            or len(message) > 8000
            or body.model_override is not None
            or body.tools is not None
            or body.created_by is not None
        ):
            raise OmnigentError(
                "Provide a message of 1–8000 characters", code=ErrorCode.INVALID_INPUT
            )
        body.type = "message"
        body.data = {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        f"[Message from session {source_id}; reply with an explicit send]\n\n"
                        f"{message}"
                    ),
                }
            ],
        }
        body._trusted_transport["peer_identity"] = {
            **identity,
            "hop_count": identity["hop_count"] + 1,
        }
        body._trusted_transport["peer_source"] = source_id

    def capture_input(self, request: Request, target_id: str, body: SessionEventInput) -> None:
        if body.type != "message":
            return
        body._trusted_transport[INPUT_AUTHORITY_FIELD] = None
        body._trusted_transport[INPUT_PROOF_FIELD] = None
        target, _ = self.endpoint(target_id)
        identity = body._trusted_transport.get("peer_identity")
        source_id = body._trusted_transport.get("peer_source")
        if identity is not None:
            assert isinstance(source_id, str)
            self.check_send(
                source_id, target_id, {**identity, "hop_count": identity["hop_count"] - 1}
            )
        elif request.headers.get(REQUEST_AUTHORITY_HEADER):
            source_id = request.headers.get(SOURCE_SESSION_HEADER, "")
            identity = self.resolve(request, source_id)
            if target.id != source_id and target.parent_conversation_id != source_id:
                raise refused("Use sys_session_message for independent-session messages")
        elif request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) or self.auth.get_credential_purpose(
            request
        ) not in (CredentialPurpose.SESSION, CredentialPurpose.CLIENT):
            return
        else:
            requester = self.auth.get_user_id(request)
            if not requester:
                return
            identity = {
                "requester_id": requester,
                "generation": account_generation(requester),
                "origin_input_id": uuid4().hex,
                "hop_count": 0,
            }
        if target.runner_id:
            input_id = uuid4().hex
            body._trusted_transport[INPUT_AUTHORITY_FIELD] = input_id
            body._trusted_transport[INPUT_PROOF_FIELD] = self.sign(
                {
                    **identity,
                    "input_id": input_id,
                    "session_id": target.id,
                    "runner_id": target.runner_id,
                }
            )

    def record_activity(self, target_id: str, body: SessionEventInput) -> None:
        source_id = body._trusted_transport.get("peer_source")
        if not source_id:
            return
        identity = body._trusted_transport["peer_identity"]
        event_id = uuid4().hex
        activity = {
            "source_id": source_id,
            "target_id": target_id,
            "origin_input_id": identity["origin_input_id"],
            "requester_id": identity["requester_id"],
            "hop_count": identity["hop_count"],
        }
        self.conversations.append(
            source_id,
            [
                NewConversationItem(
                    type="resource_event",
                    response_id=event_id,
                    data=ResourceEventData(
                        event_type="session.message.sent",
                        resource_id=event_id,
                        resource_type="session_message",
                        resource={"proof": self.sign(activity, "activity")},
                    ),
                )
            ],
        )

    def activity(self, source_id: str, user: str) -> list[dict[str, Any]]:
        if not check_session_access(
            user, source_id, LEVEL_READ, self.permissions, self.conversations
        ):
            raise refused("Session is not available")
        page = self.conversations.list_items(
            source_id, limit=100, order="desc", type="resource_event"
        )
        result = []
        for item in page.data:
            data = item.data
            if (
                not isinstance(data, ResourceEventData)
                or data.event_type != "session.message.sent"
                or not data.resource
            ):
                continue
            try:
                activity = self.verify(data.resource.get("proof", ""), "activity")
                if activity["source_id"] != source_id or not check_session_access(
                    user, activity["target_id"], LEVEL_READ, self.permissions, self.conversations
                ):
                    continue
                result.append(
                    {
                        "source_id": source_id,
                        "target_id": activity["target_id"],
                        "id": item.id,
                        "source_title": self.endpoint(source_id)[0].title,
                        "target_title": self.endpoint(activity["target_id"])[0].title,
                        "last_sent_at": item.created_at,
                    }
                )
            except OmnigentError:
                continue
        return result


def messaging_service(request: Request) -> SessionMessaging:
    service = getattr(request.app.state, "session_messaging", None)
    if not isinstance(service, SessionMessaging):
        raise refused("Session messaging is not available on this server")
    return service
