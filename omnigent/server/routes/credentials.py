"""REST routes for the per-user secret vault (#5).

Every operation is scoped to the *acting* user — a caller can only ever
store/list/delete their own secrets, and listings return metadata only (never
the secret value). Secrets are encrypted with the server vault key before they
touch the store.

- ``GET    /credentials`` — list the caller's stored credential names.
- ``PUT    /credentials/{name}`` — store/overwrite the caller's secret.
- ``DELETE /credentials/{name}`` — remove the caller's secret.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from omnigent.db.utils import generate_user_credential_id
from omnigent.entities.user_credential import UserCredential
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.user_credential_store import UserCredentialStore


class StoreSecretBody(BaseModel):
    """Request body for ``PUT /credentials/{name}``."""

    secret: str


def _metadata(c: UserCredential) -> dict[str, Any]:
    """Serialize credential metadata — deliberately omits the secret value."""
    return {
        "object": "credential",
        "id": c.id,
        "name": c.name,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


def _caller(
    request: Request,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> str:
    """Resolve the acting user; require auth in multi-user mode.

    :returns: The acting user id, or the reserved ``"local"`` identity in
        single-user mode (so the vault still has a stable owner key).
    """
    user_id = get_user_id(request, auth_provider)
    if user_id is None:
        if permission_store is not None:
            raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
        return RESERVED_USER_LOCAL
    return user_id


def _vault_key() -> bytes:
    """Return the server vault key, or 503 if the vault isn't configured."""
    from omnigent.runtime import get_caps

    key = get_caps().vault_key
    if key is None:
        raise OmnigentError(
            "The secret vault is not configured on this server",
            code=ErrorCode.NOT_FOUND,
        )
    return key


def create_credentials_router(
    store: UserCredentialStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the per-user secret vault router (mounted at ``/credentials``)."""
    router = APIRouter()

    @router.get("/credentials")
    async def list_credentials(request: Request) -> dict[str, Any]:
        """List the caller's stored credential names (no secret values)."""
        user_id = _caller(request, auth_provider, permission_store)
        return {"object": "list", "data": [_metadata(c) for c in store.list_for_user(user_id)]}

    @router.put("/credentials/{name}")
    async def store_credential(
        request: Request, name: str, body: StoreSecretBody
    ) -> dict[str, Any]:
        """Store or overwrite the caller's secret under ``name`` (encrypted)."""
        user_id = _caller(request, auth_provider, permission_store)
        if not name or not body.secret:
            raise OmnigentError("name and secret are required", code=ErrorCode.INVALID_INPUT)
        from omnigent.server.secret_vault import encrypt_secret

        encrypted = encrypt_secret(_vault_key(), body.secret)
        cred = store.upsert(generate_user_credential_id(), user_id, name, encrypted)
        return _metadata(cred)

    @router.delete("/credentials/{name}")
    async def delete_credential(request: Request, name: str) -> dict[str, bool]:
        """Delete the caller's secret named ``name``. Idempotent."""
        user_id = _caller(request, auth_provider, permission_store)
        return {"deleted": store.delete(user_id, name)}

    return router
