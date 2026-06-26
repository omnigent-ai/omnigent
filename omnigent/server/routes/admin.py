"""Admin routes: user list + an admin's view of any user's sessions.

These power the OIDC/SSO admin surface — where the accounts-mode
``Members`` page is not rendered, an operator still needs to see who
has accounts and browse their sessions. Every route here is gated on
the caller's ``is_admin`` flag (the same boolean the rest of the
server uses); this is intentionally *not* a role system.

Admins already hold owner-level access to any individual session
(``check_session_access`` short-circuits for admins), so once a
session id is listed here the existing session routes let the admin
open and act on it. These routes only add *discovery*: enumerate
users, and enumerate a chosen user's sessions.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query, Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)


def create_admin_router(
    permission_store: PermissionStore,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the admin router (mounted under ``/v1``).

    :param permission_store: Backs the admin check and the user list.
    :param conversation_store: Backs the per-user session listing.
    :param auth_provider: Resolves the caller identity from the
        request. ``None`` in single-user mode (admin routes are then
        effectively unreachable — there is no multi-user surface).
    :returns: An :class:`APIRouter` with the admin discovery routes.
    """
    router = APIRouter()

    async def _require_admin(request: Request) -> str:
        """Authn + authz: resolve the caller and require ``is_admin``.

        :raises OmnigentError: 401 if unauthenticated, 403 if the
            authenticated user is not an admin.
        """
        user_id = get_user_id(request, auth_provider)
        if user_id is None:
            raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
        if not await asyncio.to_thread(permission_store.is_admin, user_id):
            raise OmnigentError(
                "Admin privileges required",
                code=ErrorCode.FORBIDDEN,
            )
        return user_id

    @router.get("/admin/users")
    async def list_users(request: Request) -> dict[str, list[dict[str, object]]]:
        """List all users (admin only), each with a usage rollup.

        ``cost_usd`` / ``total_tokens`` sum the user's top-level
        sessions — the same set ``/admin/users/{id}/sessions`` returns,
        so a row's rollup equals the sum of that user's session rows.

        :returns: ``{"users": [{"user_id", "is_admin", "cost_usd",
            "total_tokens", "session_count"}, ...]}``.
        """
        await _require_admin(request)

        def _build() -> list[dict[str, object]]:
            out: list[dict[str, object]] = []
            for u in permission_store.list_users():
                totals = conversation_store.usage_totals_for_user(u.user_id)
                out.append(
                    {
                        "user_id": u.user_id,
                        "is_admin": u.is_admin,
                        "cost_usd": totals.cost_usd,
                        "total_tokens": totals.total_tokens,
                        "session_count": totals.session_count,
                    }
                )
            return out

        return {"users": await asyncio.to_thread(_build)}

    @router.get("/admin/users/{user_id}/sessions")
    async def list_user_sessions(
        request: Request,
        user_id: str,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, object]:
        """List the sessions a given user can access (admin only).

        Uses the same ``accessible_by`` filter the user's own session
        list uses, so an admin sees exactly what that user would —
        top-level (``kind="default"``) sessions only.

        :param user_id: The user whose sessions to list, e.g.
            ``"alice@example.com"``.
        :param limit: Maximum sessions to return (1–500).
        :returns: ``{"user_id", "totals": {...}, "sessions": [{"id",
            "title", "created_at", "updated_at", "cost_usd",
            "total_tokens"}, ...]}``. Per-session cost/tokens come from
            each conversation's ``session_usage``; ``totals`` is the
            user rollup (sums every session, not just this page).
        """
        await _require_admin(request)
        paged, totals = await asyncio.to_thread(
            lambda: (
                conversation_store.list_conversations(accessible_by=user_id, limit=limit),
                conversation_store.usage_totals_for_user(user_id),
            )
        )
        return {
            "user_id": user_id,
            "totals": {
                "cost_usd": totals.cost_usd,
                "total_tokens": totals.total_tokens,
                "session_count": totals.session_count,
            },
            "sessions": [
                {
                    "id": c.id,
                    "title": c.title,
                    "created_at": c.created_at,
                    "updated_at": c.updated_at,
                    "cost_usd": float(c.session_usage.get("total_cost_usd") or 0.0),
                    "total_tokens": int(c.session_usage.get("total_tokens") or 0),
                }
                for c in paged.data
            ],
        }

    return router
