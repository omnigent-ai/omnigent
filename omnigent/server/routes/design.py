"""Design page deck index: list artifacts across sessions and reconcile a session."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response

from omnigent.server.auth import LEVEL_EDIT, AuthProvider
from omnigent.server.feature_flags import Feature, FeatureFlags, resolve_feature_flags
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.schemas import (
    DesignArtifactEntry,
    DesignArtifactList,
    ReplaceDesignArtifactsRequest,
)
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore


def create_design_router(
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
    *,
    auth_provider: AuthProvider | None,
    feature_flags: FeatureFlags | None = None,
) -> APIRouter:
    """
    Create the Design page index router (mounted under ``/v1``).

    Both routes 404 unless the ``design`` feature is enabled. File contents
    are still read through the session file API, so file access is unchanged.

    :param conversation_store: Store holding the index and the sessions.
    :param permission_store: Session grants, or ``None`` when auth is off.
    :param auth_provider: Auth provider for user identity, or ``None``.
    :param feature_flags: Release-feature snapshot; resolved when omitted.
    :returns: The configured router.
    """
    flags = feature_flags or resolve_feature_flags()
    router = APIRouter()

    def _require_enabled() -> None:
        if not flags.enabled(Feature.DESIGN):
            raise HTTPException(status_code=404, detail="not found")

    @router.get("/design/artifacts", response_model=DesignArtifactList)
    async def list_design_artifacts(
        request: Request,
        kind: Literal["deck", "wireframe"] | None = Query(default=None),
    ) -> DesignArtifactList:
        """
        List indexed decks and wireframes in sessions the caller can see.

        Visibility is the session list's: the rows are filtered through
        ``list_conversations`` with the caller as ``accessible_by``, which
        also drops archived sessions and sub-agents.
        """
        _require_enabled()
        user_id = require_user(request, auth_provider)
        rows = await asyncio.to_thread(conversation_store.list_design_artifacts, kind)
        session_ids = list(dict.fromkeys(row.session_id for row in rows))
        if not session_ids:
            return DesignArtifactList(data=[])
        # ponytail: one unpaged read of every visible session; page when lists grow large
        page = await asyncio.to_thread(
            conversation_store.list_conversations,
            limit=len(session_ids),
            accessible_by=user_id,
            conversation_ids=session_ids,
        )
        sessions = {conv.id: conv for conv in page.data}
        return DesignArtifactList(
            data=[
                DesignArtifactEntry(
                    session_id=row.session_id,
                    path=row.path,
                    kind=row.kind,  # type: ignore[arg-type]
                    updated_at=row.updated_at,
                    session_title=conv.title,
                    workspace=conv.workspace,
                )
                for row in rows
                if (conv := sessions.get(row.session_id)) is not None
            ]
        )

    @router.put("/sessions/{session_id}/design-artifacts", status_code=204)
    async def replace_design_artifacts(
        session_id: str,
        body: ReplaceDesignArtifactsRequest,
        request: Request,
    ) -> Response:
        """Replace a session's indexed artifacts with a scan's full path set."""
        _require_enabled()
        user_id = require_user(request, auth_provider)
        await require_access(user_id, session_id, LEVEL_EDIT, permission_store, conversation_store)
        await asyncio.to_thread(
            conversation_store.replace_design_artifacts, session_id, body.paths
        )
        return Response(status_code=204)

    return router
