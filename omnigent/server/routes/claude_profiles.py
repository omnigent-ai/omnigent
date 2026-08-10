"""Read-only route for discovering Claude Code account profiles.

Exposes ``GET /v1/claude-profiles`` so the new-session picker can list
the operator-configured ``claude_profiles`` entries from
``~/.omnigent/config.yaml`` (see ``omnigent/onboarding/claude_profiles.py``).

Returns only profile names and display labels — never secrets, tokens,
or config-dir paths. The runner resolves a chosen name to a config
directory on the host where the Claude Code CLI actually runs; the
server has no need for the path and must not leak it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from omnigent.onboarding.claude_profiles import claude_profiles_list
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


def create_claude_profiles_router(
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the ``GET /v1/claude-profiles`` router.

    When ``auth_provider`` is set (multi-user mode), the handler
    requires a valid identity header. In single-user mode
    (``auth_provider=None``), the endpoint is open.

    :param auth_provider: Auth provider used to identify the
        requesting user. ``None`` in single-user mode.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    @router.get("/claude-profiles")
    async def list_claude_profiles(request: Request) -> dict[str, Any]:
        """List configured Claude Code account profiles.

        Returns ``{"object": "list", "data": [{name, display}, ...]}``
        read from the operator's ``claude_profiles`` config block.
        Names are the values the client sends back as
        ``claude_profile`` on ``POST /v1/sessions``. No secrets are
        exposed.

        :param request: The incoming request, used to extract the
            user identity for authentication.
        :returns: ``{"object": "list", "data": [...]}``.
        """
        require_user(request, auth_provider)
        return {"object": "list", "data": claude_profiles_list()}

    return router
