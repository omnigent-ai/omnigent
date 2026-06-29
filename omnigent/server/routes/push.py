"""REST routes for Web Push subscriptions (#8).

- ``GET  /push/vapid-public-key`` — the ``applicationServerKey`` the browser
  needs to subscribe (503 when push isn't configured).
- ``POST /push/subscriptions`` — register a browser ``PushSubscription``.
- ``DELETE /push/subscriptions`` — unregister one (by endpoint).

Subscriptions are user-scoped; in single-user mode (no auth provider) they key
off the reserved ``"local"`` identity.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from omnigent.db.utils import generate_push_subscription_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.push_subscription_store import PushSubscriptionStore


class SubscribeBody(BaseModel):
    """A browser ``PushSubscription.toJSON()`` payload."""

    endpoint: str
    keys: dict[str, str]  # {"p256dh": ..., "auth": ...}


class UnsubscribeBody(BaseModel):
    """Identifies the subscription to remove."""

    endpoint: str


def _caller(
    request: Request,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> str:
    """Resolve the owning user; require auth in multi-user mode.

    :returns: The user id, or the reserved ``"local"`` identity in single-user
        mode (so subscriptions still have a stable owner key).
    """
    user_id = get_user_id(request, auth_provider)
    if user_id is None:
        if permission_store is not None:
            raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
        return RESERVED_USER_LOCAL
    return user_id


def create_push_router(
    store: PushSubscriptionStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the Web Push router (mounted at ``/push``)."""
    router = APIRouter()

    @router.get("/push/vapid-public-key")
    async def vapid_public_key() -> dict[str, str]:
        """Return the server's VAPID ``applicationServerKey`` for subscribing."""
        from omnigent.runtime import get_caps
        from omnigent.server.vapid_keys import vapid_application_server_key

        key = get_caps().vapid_private_key
        if key is None:
            raise OmnigentError(
                "Web Push is not configured on this server", code=ErrorCode.NOT_FOUND
            )
        return {"key": vapid_application_server_key(key)}

    @router.post("/push/subscriptions")
    async def subscribe(request: Request, body: SubscribeBody) -> dict[str, Any]:
        """Register (or refresh) a browser push subscription."""
        user_id = _caller(request, auth_provider, permission_store)
        p256dh = body.keys.get("p256dh")
        auth = body.keys.get("auth")
        if not body.endpoint or not p256dh or not auth:
            raise OmnigentError(
                "endpoint and keys.{p256dh,auth} are required", code=ErrorCode.INVALID_INPUT
            )
        sub = store.upsert(generate_push_subscription_id(), user_id, body.endpoint, p256dh, auth)
        return {"id": sub.id, "object": "push_subscription"}

    @router.delete("/push/subscriptions")
    async def unsubscribe(request: Request, body: UnsubscribeBody) -> dict[str, bool]:
        """Unregister a browser push subscription by endpoint. Idempotent."""
        _caller(request, auth_provider, permission_store)
        return {"deleted": store.delete_by_endpoint(body.endpoint)}

    return router
