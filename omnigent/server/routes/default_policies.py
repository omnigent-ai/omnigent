"""Routes for server-wide default policy CRUD.

Default policies are managed via
``POST/GET/PATCH/DELETE /v1/policies[/{policy_id}]``.

Unlike session policies, default policies are not scoped to a single
session — they apply server-wide and are managed by admins. In
multi-user mode, all mutating endpoints require admin privileges;
read endpoints require authentication.

Default policies are stored in the same ``policies`` table as
session policies, with ``session_id IS NULL``.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy.exc import IntegrityError

from omnigent.entities import Policy
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.policies.registry import is_registered_handler, validate_factory_params
from omnigent.runtime.policies.builder import invalidate_default_policy_specs_cache
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.server.schemas import (
    _DOTTED_PATH_RE,
    CreateDefaultPolicyRequest,
    UpdateDefaultPolicyRequest,
)
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.policy_store import PolicyStore


def _generate_default_policy_id() -> str:
    """Generate a unique default policy identifier.

    :returns: A string of the form ``"pol_<32-char hex>"``,
        e.g. ``"pol_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"``.
    """
    return f"pol_{uuid.uuid4().hex}"


def _entity_to_response(policy: Policy) -> dict[str, Any]:
    """Convert a :class:`Policy` entity to a default policy response dict.

    :param policy: The entity to convert.
    :returns: Dict matching :class:`DefaultPolicyObject` shape.
    """
    result: dict[str, Any] = {
        "id": policy.id,
        "object": "default_policy",
        "name": policy.name,
        "type": policy.type,
        "handler": policy.handler,
        "enabled": policy.enabled,
        "created_at": policy.created_at,
        "updated_at": policy.updated_at,
        "created_by": policy.created_by,
    }
    if policy.factory_params is not None:
        result["factory_params"] = policy.factory_params
    return result


async def _require_admin(
    request: Request,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> str | None:
    """Extract user identity and verify admin status.

    In single-user mode (no auth provider), returns ``None``
    and skips the check. In multi-user mode, raises 401 if
    unauthenticated or 403 if not an admin.

    :param request: The incoming FastAPI request.
    :param auth_provider: Auth provider, or ``None`` in
        single-user mode.
    :param permission_store: Permission store for admin checks,
        or ``None`` to skip.
    :returns: User ID string, or ``None`` in single-user mode.
    :raises OmnigentError: 401 if unauthenticated, 403 if
        not an admin.
    """
    user_id = get_user_id(request, auth_provider)
    if permission_store is None:
        return user_id
    if user_id is None:
        raise OmnigentError(
            "Authentication required",
            code=ErrorCode.UNAUTHORIZED,
        )
    is_admin = await asyncio.to_thread(permission_store.is_admin, user_id)
    if not is_admin:
        raise OmnigentError(
            "Admin privileges required to manage default policies",
            code=ErrorCode.FORBIDDEN,
        )
    return user_id


def create_default_policies_router(
    store: PolicyStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the default policies router.

    All routes are scoped to ``/policies[/{policy_id}]``.

    In multi-user mode (both ``auth_provider`` and
    ``permission_store`` provided), mutating endpoints require
    admin privileges. Read endpoints require authentication.

    :param store: The shared :class:`PolicyStore` instance.
    :param auth_provider: Auth provider used to identify the
        requesting user. ``None`` in single-user mode.
    :param permission_store: Permission store used to check
        admin status. ``None`` disables permission enforcement.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    @router.post("/policies")
    async def create_policy(
        request: Request,
        body: CreateDefaultPolicyRequest,
    ) -> dict[str, Any]:
        """Create a new default policy.

        Requires admin privileges in multi-user mode.

        :param request: The incoming request, used to extract the
            user identity.
        :param body: Policy payload including name, type, and
            handler.
        :returns: The created policy as a serialized dict.
        :raises OmnigentError: 401/403 if the user lacks admin
            privileges, or 409 if a policy with the same name
            already exists.
        """
        user_id = await _require_admin(request, auth_provider, permission_store)
        if body.type != "python":
            raise OmnigentError(
                f"Default policies only support type='python'; type={body.type!r} "
                f"cannot be evaluated. URL policy evaluation is a future extension.",
                code=ErrorCode.INVALID_INPUT,
            )
        # Restrict handlers to the registry allowlist.
        # Admins are not exempt: a custom handler must be added via
        # the ``policy_modules`` config so it appears in the registry,
        # rather than being named ad hoc here. This keeps a single
        # allowlist and blocks arbitrary callable injection.
        if not is_registered_handler(body.handler):
            raise OmnigentError(
                f"Policy handler '{body.handler}' is not registered. Add the "
                f"module that declares it to the server's 'policy_modules' "
                f"config so it appears in the policy registry.",
                code=ErrorCode.INVALID_INPUT,
            )
        # Validate factory_params against the registry schema.
        validation_error = validate_factory_params(body.handler, body.factory_params)
        if validation_error:
            raise OmnigentError(validation_error, code=ErrorCode.INVALID_INPUT)
        policy_id = _generate_default_policy_id()
        try:
            policy = store.create_default(
                policy_id=policy_id,
                name=body.name,
                type=body.type,
                handler=body.handler,
                factory_params=body.factory_params,
                created_by=user_id,
            )
        except IntegrityError as exc:
            raise OmnigentError(
                f"Default policy with name '{body.name}' already exists",
                code=ErrorCode.CONFLICT,
            ) from exc
        invalidate_default_policy_specs_cache()
        return _entity_to_response(policy)

    @router.get("/policies")
    async def list_policies(
        request: Request,
    ) -> dict[str, Any]:
        """List all default policies.

        Requires authentication in multi-user mode.

        :param request: The incoming request, used to extract the
            user identity.
        :returns: ``{"object": "list", "data": [...]}``.
        :raises OmnigentError: 401 if unauthenticated in
            multi-user mode.
        """
        # Read-only: authenticate but don't require admin.
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None and user_id is None:
            raise OmnigentError(
                "Authentication required",
                code=ErrorCode.UNAUTHORIZED,
            )
        policies = store.list_defaults()
        return {"object": "list", "data": [_entity_to_response(p) for p in policies]}

    @router.get("/policies/{policy_id}")
    async def get_policy(
        request: Request,
        policy_id: str,
    ) -> dict[str, Any]:
        """Get a single default policy.

        Requires authentication in multi-user mode.

        :param request: The incoming request, used to extract the
            user identity.
        :param policy_id: The policy to retrieve, e.g.
            ``"pol_abc123"``.
        :returns: The policy as a serialized dict.
        :raises OmnigentError: 401 if unauthenticated, or
            404 if the policy is not found.
        """
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None and user_id is None:
            raise OmnigentError(
                "Authentication required",
                code=ErrorCode.UNAUTHORIZED,
            )
        policy = store.get_default(policy_id)
        if policy is None:
            raise OmnigentError("Policy not found", code=ErrorCode.NOT_FOUND)
        return _entity_to_response(policy)

    @router.patch("/policies/{policy_id}")
    async def update_policy(
        request: Request,
        policy_id: str,
        body: UpdateDefaultPolicyRequest,
    ) -> dict[str, Any]:
        """Update a default policy's mutable fields.

        ``type`` is immutable — the caller must delete and
        re-create to change it. Requires admin privileges.

        :param request: The incoming request, used to extract the
            user identity.
        :param policy_id: The policy to update, e.g.
            ``"pol_abc123"``.
        :param body: Fields to update; ``None`` fields are left
            unchanged.
        :returns: The updated policy as a serialized dict.
        :raises OmnigentError: 401/403 if the user lacks admin
            privileges, or 404 if the policy is not found.
        """
        await _require_admin(request, auth_provider, permission_store)
        # Validate handler against the existing policy's type.
        if body.handler is not None:
            existing = store.get_default(policy_id)
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
                # Same registry allowlist as create.
                if not is_registered_handler(body.handler):
                    raise OmnigentError(
                        f"Policy handler '{body.handler}' is not registered. Add the "
                        f"module that declares it to the server's 'policy_modules' "
                        f"config so it appears in the policy registry.",
                        code=ErrorCode.INVALID_INPUT,
                    )
        try:
            policy = store.update_default(
                policy_id,
                name=body.name,
                handler=body.handler,
                enabled=body.enabled,
            )
        except IntegrityError as exc:
            raise OmnigentError(
                f"Default policy with name '{body.name}' already exists",
                code=ErrorCode.CONFLICT,
            ) from exc
        if policy is None:
            raise OmnigentError("Policy not found", code=ErrorCode.NOT_FOUND)
        invalidate_default_policy_specs_cache()
        return _entity_to_response(policy)

    @router.delete("/policies/{policy_id}")
    async def delete_policy(
        request: Request,
        policy_id: str,
    ) -> dict[str, Any]:
        """Delete a default policy.

        Idempotent — deleting a missing policy returns 204.
        Requires admin privileges.

        :param request: The incoming request, used to extract the
            user identity.
        :param policy_id: The policy to delete, e.g.
            ``"pol_abc123"``.
        :returns: ``{"deleted": true}``.
        :raises OmnigentError: 401/403 if the user lacks admin
            privileges.
        """
        await _require_admin(request, auth_provider, permission_store)
        store.delete_default(policy_id)
        invalidate_default_policy_specs_cache()
        return {"deleted": True}

    return router
