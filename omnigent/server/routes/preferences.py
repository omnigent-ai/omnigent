"""Per-user sidebar preference routes.

``GET /v1/preferences`` returns every stored preference for the caller;
``PUT /v1/preferences/{key}`` upserts one. These back the sidebar's pin set and
its section collapse/expand sets so they follow the account rather than living
in one browser's ``localStorage`` — the browser copy is demoted to a fast local
cache and offline fallback.

Rows are keyed by the caller's identity, so the router is only mounted when the
server has a permission store (an identity to key on). The ``key`` is
constrained to a small allow-list and the value is size-capped, so the endpoint
is not an open per-user blob store. When the caller is unauthenticated there is
no row to key on: the endpoint 401s and the web app stays on its localStorage
cache.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.preference_store import PreferenceStore

# The only keys a client may write. Each is a JSON array of short strings the
# sidebar owns (pinned session ids, collapsed section titles, expanded project
# names). Adding a future preference means extending this set — the storage
# layer is already generic.
_ALLOWED_KEYS = frozenset(
    {
        "pinned_conversation_ids",
        "collapsed_sidebar_sections",
        "expanded_project_sections",
    }
)

# Cap the serialized value so the column can't be stuffed. A few thousand short
# ids/titles fit comfortably; anything larger is a client bug or abuse.
_MAX_VALUE_BYTES = 64 * 1024


class PutPreferenceRequest(BaseModel):
    """Body for ``PUT /v1/preferences/{key}``.

    :param value: The preference value — a JSON array of strings the sidebar
        owns, e.g. ``["conv_a", "conv_b"]``.
    """

    value: list[str]


def create_preferences_router(
    preference_store: PreferenceStore,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the per-user preference router (mounted under ``/v1``)."""
    router = APIRouter()

    @router.get("/preferences")
    async def get_preferences(request: Request) -> dict[str, Any]:
        """Return every stored preference for the caller, JSON-decoded."""
        user_id = require_user(request, auth_provider)
        if user_id is None:
            # No auth provider at all: the router shouldn't have been mounted,
            # but stay safe and expose nothing rather than a shared blob.
            raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
        raw = await asyncio.to_thread(preference_store.get_all, user_id)
        preferences: dict[str, Any] = {}
        for key, value in raw.items():
            try:
                preferences[key] = json.loads(value)
            except json.JSONDecodeError:
                # A corrupt row shouldn't blank the whole sidebar — skip it and
                # let the client fall back to its local cache for that key.
                continue
        return {"object": "preferences", "preferences": preferences}

    @router.put("/preferences/{key}")
    async def put_preference(
        request: Request, key: str, body: PutPreferenceRequest
    ) -> dict[str, Any]:
        """Upsert one preference for the caller.

        Rejects an unknown key (400) so the endpoint stays an allow-list, and a
        value beyond the size cap (400) so the column can't be stuffed.
        """
        user_id = require_user(request, auth_provider)
        if user_id is None:
            raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
        if key not in _ALLOWED_KEYS:
            raise OmnigentError(
                f"Unknown preference key {key!r}. Expected one of: "
                + ", ".join(sorted(_ALLOWED_KEYS))
                + ".",
                code=ErrorCode.INVALID_INPUT,
            )
        serialized = json.dumps(body.value, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > _MAX_VALUE_BYTES:
            raise OmnigentError(
                "Preference value is too large.",
                code=ErrorCode.INVALID_INPUT,
            )
        await asyncio.to_thread(preference_store.set, user_id, key, serialized)
        return {"object": "preference", "key": key, "value": body.value}

    return router
