"""Harness-neutral skill catalog and trust-setting routes."""

from __future__ import annotations

import urllib.parse
from typing import TYPE_CHECKING, Any, Literal, cast

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_access
from omnigent.server.skill_settings import read_skill_trust, write_skill_trust
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore

if TYPE_CHECKING:
    from omnigent.runner.routing import RunnerRouter


class SkillTrustRequest(BaseModel):
    """Body for updating the default discovery trust boundary."""

    value: Literal["current", "all-host"]


def create_skills_router(
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the skill registry API router."""
    router = APIRouter()

    async def _runner_payload(
        request: Request,
        session_id: str,
        path: str,
        *,
        include_other_tools: bool | None,
        extra_params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Authorize and proxy one catalog request to the bound runner."""
        user_id = get_user_id(request, auth_provider)
        await require_access(
            user_id,
            session_id,
            LEVEL_READ,
            permission_store,
            conversation_store,
        )
        if runner_router is not None:
            routed = runner_router.client_for_session_resources(session_id)
            runner_client = routed.client
        else:
            from omnigent.runtime import get_runner_client

            runner_client = cast("httpx.AsyncClient | None", get_runner_client())
        if runner_client is None:
            raise OmnigentError(
                f"No runner is available for session {session_id!r}",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )
        merged: dict[str, str] = {} if extra_params is None else dict(extra_params)
        if include_other_tools is not None:
            merged["include_other_tools"] = str(include_other_tools).lower()
        # Preserve the "no query string" contract (params=None) when there is
        # nothing to send, so existing catalog/detail proxying is byte-identical.
        params = merged or None
        try:
            response = await runner_client.get(path, params=params, timeout=10.0)
            payload = response.json()
        except (httpx.HTTPError, ConnectionError, ValueError) as exc:
            raise OmnigentError(
                f"Runner failed to resolve skills for session {session_id!r}: {exc}",
                code=ErrorCode.INTERNAL_ERROR,
            ) from exc
        if not isinstance(payload, dict):
            raise OmnigentError(
                f"Runner returned malformed skills for session {session_id!r}",
                code=ErrorCode.INTERNAL_ERROR,
            )
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail=payload.get("detail", "Skill not found"))
        # Preserve the runner's client-error status (e.g. a 400 path-traversal
        # refusal or a 413 oversized-file rejection from the file endpoints)
        # rather than masking every non-200 as a 500 — the client needs the real
        # reason to render the right non-preview / error state.
        if 400 <= response.status_code < 500:
            raise HTTPException(
                status_code=response.status_code,
                detail=payload.get("detail", "Skill request rejected"),
            )
        if response.status_code != 200:
            raise OmnigentError(
                f"Runner failed to resolve skills for session {session_id!r}: "
                f"HTTP {response.status_code}",
                code=ErrorCode.INTERNAL_ERROR,
            )
        return payload

    @router.get("/skills")
    async def list_skills(
        request: Request,
        session_id: str = Query(),
        include_other_tools: bool | None = Query(default=None),
    ) -> dict[str, Any]:
        quoted_session_id = urllib.parse.quote(session_id, safe="")
        return await _runner_payload(
            request,
            session_id,
            f"/v1/sessions/{quoted_session_id}/skills/catalog",
            include_other_tools=include_other_tools,
        )

    @router.get("/skills/trust")
    async def get_skill_trust() -> dict[str, Any]:
        value = read_skill_trust()
        return {"value": value, "include_other_tools": value == "all-host"}

    @router.put("/skills/trust")
    async def set_skill_trust(body: SkillTrustRequest) -> dict[str, Any]:
        write_skill_trust(body.value)
        return {"value": body.value, "include_other_tools": body.value == "all-host"}

    @router.get("/skills/{skill_id}")
    async def get_skill(
        request: Request,
        skill_id: str,
        session_id: str = Query(),
        include_other_tools: bool | None = Query(default=None),
    ) -> dict[str, Any]:
        quoted_session_id = urllib.parse.quote(session_id, safe="")
        quoted_skill_id = urllib.parse.quote(skill_id, safe="")
        return await _runner_payload(
            request,
            session_id,
            f"/v1/sessions/{quoted_session_id}/skills/catalog/{quoted_skill_id}",
            include_other_tools=include_other_tools,
        )

    @router.get("/skills/{skill_id}/files")
    async def list_skill_files(
        request: Request,
        skill_id: str,
        session_id: str = Query(),
        include_other_tools: bool | None = Query(default=None),
    ) -> dict[str, Any]:
        """Proxy the winner skill's resource-tree listing from the bound runner."""
        quoted_session_id = urllib.parse.quote(session_id, safe="")
        quoted_skill_id = urllib.parse.quote(skill_id, safe="")
        return await _runner_payload(
            request,
            session_id,
            f"/v1/sessions/{quoted_session_id}/skills/catalog/{quoted_skill_id}/files",
            include_other_tools=include_other_tools,
        )

    @router.get("/skills/{skill_id}/file")
    async def read_skill_file_route(
        request: Request,
        skill_id: str,
        path: str = Query(),
        session_id: str = Query(),
        include_other_tools: bool | None = Query(default=None),
    ) -> dict[str, Any]:
        """Proxy a single safe skill-file read from the bound runner."""
        quoted_session_id = urllib.parse.quote(session_id, safe="")
        quoted_skill_id = urllib.parse.quote(skill_id, safe="")
        extra = {"path": path}
        return await _runner_payload(
            request,
            session_id,
            f"/v1/sessions/{quoted_session_id}/skills/catalog/{quoted_skill_id}/file",
            include_other_tools=include_other_tools,
            extra_params=extra,
        )

    return router
