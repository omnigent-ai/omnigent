"""Read-only route for installed harness catalog metadata."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from omnigent.harness_plugins import harness_catalog, harness_setup_steps_by_spelling
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


def create_harnesses_router(*, auth_provider: AuthProvider | None = None) -> APIRouter:
    """Build the router for ``GET /v1/harnesses``."""
    router = APIRouter()

    @router.get("/harnesses")
    async def list_harnesses(request: Request) -> dict[str, Any]:
        require_user(request, auth_provider)
        # ``data`` is the picker catalog (keyed by picker id). ``setup_steps``
        # is a separate map keyed by EVERY harness spelling a session may
        # declare — native wrappers (``codex-native``) and installable ids that
        # aren't picker rows (``opencode``/``qwen``) — so the setup dialog can
        # resolve steps by the harness it actually holds without the picker
        # list gaining non-pickable rows.
        return {
            "data": harness_catalog(),
            "setup_steps": harness_setup_steps_by_spelling(),
        }

    return router
