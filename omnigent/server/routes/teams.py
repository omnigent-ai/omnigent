"""Self-service membership queries against the configured external directory."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, TypeAdapter

from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.team_providers import Team, TeamProvider

_logger = logging.getLogger(__name__)
_LOOKUP_TIMEOUT_SECONDS = 15
_TEAMS = TypeAdapter(list[Team])


class TeamsResponse(BaseModel):
    teams: list[Team]


class TeamMembershipResponse(BaseModel):
    team_id: str
    member: bool


def create_teams_router(
    *, provider: TeamProvider | None, auth_provider: AuthProvider | None
) -> APIRouter:
    router = APIRouter()

    async def lookup(request: Request, response: Response) -> list[Team]:
        user_id = require_user(request, auth_provider)
        if user_id is None or user_id == RESERVED_USER_LOCAL:
            raise HTTPException(
                status_code=401, detail="Team lookup requires an authenticated user"
            )
        if provider is None:
            raise HTTPException(
                status_code=503, detail="No team membership provider is configured"
            )
        response.headers["Cache-Control"] = "private, no-store"
        try:
            async with asyncio.timeout(_LOOKUP_TIMEOUT_SECONDS):
                return _TEAMS.validate_python(await provider.list_teams(user_id))
        except Exception as exc:  # noqa: BLE001 — never interpret lookup failures as non-membership
            _logger.warning("Team membership lookup failed (%s)", type(exc).__name__)
            raise HTTPException(
                status_code=503, detail="Team membership provider is unavailable"
            ) from None

    @router.get("/me/teams")
    async def list_my_teams(request: Request, response: Response) -> TeamsResponse:
        return TeamsResponse(teams=await lookup(request, response))

    @router.get("/me/team-membership")
    async def check_my_membership(
        request: Request,
        response: Response,
        team_id: str = Query(min_length=1, max_length=512),
    ) -> TeamMembershipResponse:
        teams = await lookup(request, response)
        return TeamMembershipResponse(team_id=team_id, member=any(t.id == team_id for t in teams))

    return router
