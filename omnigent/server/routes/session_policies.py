"""Routes for per-session policy CRUD.

Session policies are managed via
``POST/GET/PATCH/DELETE /v1/sessions/{session_id}/policies[/{policy_id}]``.

The List endpoint merges store-persisted (``source="session"``) and
spec-declared (``source="spec"``) policies so the UI shows a unified
view. Spec policies have ``id=None`` and cannot be patched or deleted.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy.exc import IntegrityError

from omnigent.entities import Policy
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.policies.registry import (
    get_entry,
    is_registered_handler,
    validate_factory_params,
)
from omnigent.runtime import get_caps
from omnigent.runtime.policies.builder import invalidate_session_policy_specs_cache
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_access
from omnigent.server.schemas import (
    _DOTTED_PATH_RE,
    CreateSessionPolicyRequest,
    UpdateSessionPolicyRequest,
)
from omnigent.spec.types import FunctionPolicySpec, PolicySpec
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.policy_store import PolicyStore


def _generate_policy_id() -> str:
    """Generate a unique policy identifier.

    :returns: A string of the form ``"pol_<32-char hex>"``,
        e.g. ``"pol_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"``.
    """
    return f"pol_{uuid.uuid4().hex}"


def _entity_to_response(policy: Policy) -> dict[str, Any]:
    """Convert a :class:`Policy` entity to a session policy response dict.

    :param policy: The entity to convert.
    :returns: Dict matching :class:`SessionPolicyObject` shape.
    """
    result: dict[str, Any] = {
        "id": policy.id,
        "object": "session.policy",
        "name": policy.name,
        "type": policy.type,
        "handler": policy.handler,
        "enabled": policy.enabled,
        "source": "session",
        "created_at": policy.created_at,
        "updated_at": policy.updated_at,
    }
    if policy.factory_params is not None:
        result["factory_params"] = policy.factory_params
    return result


def _spec_to_response(spec: PolicySpec, source: str) -> dict[str, Any]:
    """Convert a :class:`PolicySpec` to a policy list response dict.

    Used to surface admin (server-wide) policies in the list
    endpoint alongside session policies.

    :param spec: The policy spec from ``RuntimeCaps.default_policies``.
    :param source: Origin label, e.g. ``"admin"``.
    :returns: Dict matching the session policy response shape.
    """
    handler: str | None = None
    policy_type = "unknown"
    if isinstance(spec, FunctionPolicySpec) and spec.function:
        handler = spec.function.path
        policy_type = "function"

    # Look up registry for a description.
    description = None
    if handler:
        entry = get_entry(handler)
        description = entry.description if entry else handler

    return {
        "id": None,
        "object": "session.policy",
        "name": spec.name,
        "type": policy_type,
        "handler": handler,
        "enabled": True,
        "source": source,
        "description": description,
        "created_at": 0,
        "updated_at": None,
    }


def create_session_policies_router(
    store: PolicyStore,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the session policies router.

    All routes are scoped to
    ``/sessions/{session_id}/policies[/{policy_id}]``.

    When both ``permission_store`` and ``conversation_store``
    are provided (multi-user mode), every handler enforces
    session-level access: read endpoints require ``LEVEL_READ``,
    mutating endpoints require ``LEVEL_EDIT``.

    :param store: The shared :class:`PolicyStore` instance.
    :param conversation_store: Conversation store used to verify
        the session exists and by the permission checker for
        sub-agent session delegation.
    :param auth_provider: Auth provider used to identify the
        requesting user. ``None`` in single-user mode.
    :param permission_store: Permission store used to check
        session-level access grants. ``None`` disables
        permission enforcement.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    def _require_session_exists(session_id: str) -> None:
        """Raise 404 if the session does not exist.

        :param session_id: The session to check, e.g.
            ``"conv_abc123"``.
        :raises OmnigentError: 404 if the session is not found.
        """
        conv = conversation_store.get_conversation(session_id)
        if conv is None:
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)

    @router.post("/sessions/{session_id}/policies")
    async def create_policy(
        request: Request,
        session_id: str,
        body: CreateSessionPolicyRequest,
    ) -> dict[str, Any]:
        """Create a new session policy.

        Requires ``LEVEL_EDIT`` on the session in multi-user mode.

        :param request: The incoming request, used to extract the
            user identity.
        :param session_id: The owning session, e.g.
            ``"conv_abc123"``.
        :param body: Policy payload including name, type, and
            handler.
        :returns: The created policy as a serialized dict.
        :raises OmnigentError: 401/403 if the user lacks edit
            permission, 404 if the session is not found, or 409
            if a policy with the same name already exists.
        """
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None:
            await require_access(
                user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
            )
        _require_session_exists(session_id)
        if body.type == "python":
            # Restrict handlers to the registry allowlist.
            # An arbitrary dotted path is imported and called by the
            # policy engine, so accepting unregistered handlers is RCE.
            # Custom handlers must be added by a server admin via the
            # ``policy_modules`` config so they appear in the registry.
            if not is_registered_handler(body.handler):
                raise OmnigentError(
                    f"Policy handler '{body.handler}' is not registered. Only "
                    f"handlers from the policy registry (browse "
                    f"GET /v1/policy-registry) may be attached; a server admin "
                    f"must add custom handlers via the 'policy_modules' config.",
                    code=ErrorCode.INVALID_INPUT,
                )
            # Validate factory_params against the registry schema.
            validation_error = validate_factory_params(body.handler, body.factory_params)
            if validation_error:
                raise OmnigentError(validation_error, code=ErrorCode.INVALID_INPUT)
        policy_id = _generate_policy_id()
        try:
            policy = store.create(
                policy_id=policy_id,
                session_id=session_id,
                name=body.name,
                type=body.type,
                handler=body.handler,
                factory_params=body.factory_params,
            )
        except IntegrityError as exc:
            raise OmnigentError(
                f"Policy with name '{body.name}' already exists in this session",
                code=ErrorCode.CONFLICT,
            ) from exc
        invalidate_session_policy_specs_cache(session_id)
        return _entity_to_response(policy)

    @router.get("/sessions/{session_id}/policies")
    async def list_policies(
        request: Request,
        session_id: str,
    ) -> dict[str, Any]:
        """List all policies for a session.

        Returns both store-persisted (``source="session"``) and
        spec-declared (``source="spec"``) policies. Spec policies
        have ``id=None`` and cannot be patched or deleted.

        Requires ``LEVEL_READ`` on the session in multi-user mode.

        :param request: The incoming request, used to extract the
            user identity.
        :param session_id: The session to query, e.g.
            ``"conv_abc123"``.
        :returns: ``{"object": "list", "data": [...]}``.
        :raises OmnigentError: 401/403 if the user lacks read
            permission, 404 if the session is not found.
        """
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None:
            await require_access(
                user_id, session_id, LEVEL_READ, permission_store, conversation_store
            )
        _require_session_exists(session_id)
        # Admin (server-wide) policies from the --config YAML.
        admin_specs = get_caps().default_policies
        admin_data = [_spec_to_response(s, "admin") for s in admin_specs]
        # Session policies from the store.
        session_policies = store.list_for_session(session_id)
        session_data = [_entity_to_response(p) for p in session_policies]
        return {"object": "list", "data": admin_data + session_data}

    @router.get("/sessions/{session_id}/policies/{policy_id}")
    async def get_policy(
        request: Request,
        session_id: str,
        policy_id: str,
    ) -> dict[str, Any]:
        """Get a single session policy.

        Requires ``LEVEL_READ`` on the session in multi-user mode.

        :param request: The incoming request, used to extract the
            user identity.
        :param session_id: The owning session, e.g.
            ``"conv_abc123"``.
        :param policy_id: The policy to retrieve, e.g.
            ``"pol_abc123"``.
        :returns: The policy as a serialized dict.
        :raises OmnigentError: 401/403 if the user lacks read
            permission, or 404 if the policy is not found.
        """
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None:
            await require_access(
                user_id, session_id, LEVEL_READ, permission_store, conversation_store
            )
        policy = store.get(policy_id, session_id)
        if policy is None:
            raise OmnigentError("Policy not found", code=ErrorCode.NOT_FOUND)
        return _entity_to_response(policy)

    @router.patch("/sessions/{session_id}/policies/{policy_id}")
    async def update_policy(
        request: Request,
        session_id: str,
        policy_id: str,
        body: UpdateSessionPolicyRequest,
    ) -> dict[str, Any]:
        """Update a session policy's mutable fields.

        ``type`` is immutable — the caller must delete and
        re-create to change it. Requires ``LEVEL_EDIT``.

        :param request: The incoming request, used to extract the
            user identity.
        :param session_id: The owning session, e.g.
            ``"conv_abc123"``.
        :param policy_id: The policy to update, e.g.
            ``"pol_abc123"``.
        :param body: Fields to update; ``None`` fields are left
            unchanged.
        :returns: The updated policy as a serialized dict.
        :raises OmnigentError: 401/403 if the user lacks edit
            permission, or 404 if the policy is not found.
        """
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None:
            await require_access(
                user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
            )
        # Validate handler against the existing policy's type.
        if body.handler is not None:
            existing = store.get(policy_id, session_id)
            if existing is None:
                raise OmnigentError("Policy not found", code=ErrorCode.NOT_FOUND)
            if existing.type == "url" and not body.handler.startswith("https://"):
                raise OmnigentError(
                    "handler must be an https:// URL for type 'url'",
                    code=ErrorCode.INVALID_INPUT,
                )
            if existing.type == "python":
                if not re.match(_DOTTED_PATH_RE, body.handler):
                    raise OmnigentError(
                        "handler must be a valid dotted import path for type 'python'",
                        code=ErrorCode.INVALID_INPUT,
                    )
                # Same registry allowlist as create: a PATCH
                # must not be a back door to point a policy at an
                # arbitrary, unregistered callable.
                if not is_registered_handler(body.handler):
                    raise OmnigentError(
                        f"Policy handler '{body.handler}' is not registered. Only "
                        f"handlers from the policy registry (browse "
                        f"GET /v1/policy-registry) may be attached; a server admin "
                        f"must add custom handlers via the 'policy_modules' config.",
                        code=ErrorCode.INVALID_INPUT,
                    )
        policy = store.update(
            policy_id,
            session_id,
            name=body.name,
            handler=body.handler,
            enabled=body.enabled,
        )
        if policy is None:
            raise OmnigentError("Policy not found", code=ErrorCode.NOT_FOUND)
        invalidate_session_policy_specs_cache(session_id)
        return _entity_to_response(policy)

    @router.delete("/sessions/{session_id}/policies/{policy_id}")
    async def delete_policy(
        request: Request,
        session_id: str,
        policy_id: str,
    ) -> dict[str, Any]:
        """Delete a session policy.

        Idempotent — deleting a missing policy returns 204.
        Requires ``LEVEL_EDIT``.

        :param request: The incoming request, used to extract the
            user identity.
        :param session_id: The owning session, e.g.
            ``"conv_abc123"``.
        :param policy_id: The policy to delete, e.g.
            ``"pol_abc123"``.
        :returns: ``{"deleted": true}``.
        :raises OmnigentError: 401/403 if the user lacks edit
            permission.
        """
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None:
            await require_access(
                user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
            )
        store.delete(policy_id, session_id)
        invalidate_session_policy_specs_cache(session_id)
        return {"deleted": True}

    return router
