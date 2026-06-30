"""Read-only route for the policy registry.

Exposes ``GET /v1/policy-registry`` so authenticated users can browse
available built-in policy functions and their parameter schemas before
attaching them to sessions.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request

from omnigent.policies.registry import get_registry
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


def create_policy_registry_router(
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the policy registry router.

    When ``auth_provider`` is set (multi-user mode), the handler
    requires a valid identity header. In single-user mode
    (``auth_provider=None``), the endpoint is open.

    :param auth_provider: Auth provider used to identify the
        requesting user. ``None`` in single-user mode.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    @router.get("/policy-registry")
    async def list_registry(request: Request) -> dict[str, Any]:
        """List all registered policy functions.

        Returns the full catalog of built-in policy callables
        with their handler paths, descriptions, and parameter
        schemas.

        :param request: The incoming request, used to extract
            the user identity for authentication.
        :returns: ``{"object": "list", "data": [...]}``.
        """
        # Authenticate — rejects unauthenticated requests in
        # multi-user mode (401 via require_user; get_user_id would
        # return None and let the request through). No permission
        # check needed since the registry is not session-scoped.
        require_user(request, auth_provider)
        entries = get_registry()
        # Filter out internal-only policies (e.g., subagent_cost_budget)
        # that are for internal use only and should not appear in the UI
        public_entries = [e for e in entries if not e.internal_only]
        return {
            "object": "list",
            "data": [asdict(e) for e in public_entries],
        }

    return router
