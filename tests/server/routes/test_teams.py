"""Team lookups stay scoped to the authenticated user and fail closed."""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes.teams import create_teams_router
from omnigent.team_providers import Team, TeamProvider

_USER = "alice@example.com"
_TEAM = Team(id="00g-engineering", name="Engineering")


def _client(
    provider: TeamProvider | None,
    *,
    user: str | None = _USER,
    auth_enabled: bool = True,
) -> httpx.AsyncClient:
    app = FastAPI()
    auth = (
        UnifiedAuthProvider(source="header", header_name="x-test-user", local_single_user=False)
        if auth_enabled
        else None
    )
    app.include_router(create_teams_router(provider=provider, auth_provider=auth), prefix="/v1")

    @app.exception_handler(OmnigentError)
    async def handle_error(request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"detail": exc.message})

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"x-test-user": user} if user else {},
    )


async def test_self_lookup_uses_stable_ids_and_refreshes_membership() -> None:
    provider = Mock(spec=TeamProvider)
    provider.list_teams.return_value = [_TEAM]
    async with _client(provider) as client:
        listed = await client.get("/v1/me/teams", params={"user_id": "someone-else@example.com"})
        assert listed.status_code == 200
        assert listed.json() == {"teams": [{"id": _TEAM.id, "name": _TEAM.name}]}
        assert listed.headers["cache-control"] == "private, no-store"

        for team_id, expected in [(_TEAM.id, True), (_TEAM.name, False)]:
            checked = await client.get(
                "/v1/me/team-membership",
                params={"team_id": team_id, "user_id": "someone-else@example.com"},
            )
            assert checked.status_code == 200
            assert checked.json() == {"team_id": team_id, "member": expected}
            assert checked.headers["cache-control"] == "private, no-store"

        provider.list_teams.return_value = []
        refreshed = await client.get("/v1/me/team-membership", params={"team_id": _TEAM.id})
        assert refreshed.json() == {"team_id": _TEAM.id, "member": False}
        assert (await client.get("/v1/me/teams")).json() == {"teams": []}

    assert provider.list_teams.await_count == 5
    assert all(call.args == (_USER,) for call in provider.list_teams.await_args_list)


@pytest.mark.parametrize("user,auth_enabled", [(_USER, False), (None, True), ("local", True)])
async def test_lookup_requires_a_real_authenticated_user(
    user: str | None, auth_enabled: bool
) -> None:
    provider = Mock(spec=TeamProvider)
    async with _client(provider, user=user, auth_enabled=auth_enabled) as client:
        for url in ("/v1/me/teams", "/v1/me/team-membership?team_id=engineering"):
            assert (await client.get(url)).status_code == 401
    provider.list_teams.assert_not_called()


@pytest.mark.parametrize("failure", ["unconfigured", "exception", "timeout", "malformed"])
async def test_lookup_failure_is_unavailable_and_never_nonmembership(
    failure: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    provider = Mock(spec=TeamProvider)
    if failure == "exception":
        provider.list_teams.side_effect = RuntimeError("private-provider-token")
    elif failure == "malformed":
        provider.list_teams.return_value = [{"id": 123, "name": "private-provider-token"}]
    elif failure == "timeout":
        monkeypatch.setattr("omnigent.server.routes.teams._LOOKUP_TIMEOUT_SECONDS", 0.01)

        async def hang(user_id: str) -> list[Team]:
            await asyncio.Event().wait()
            return []

        provider.list_teams.side_effect = hang

    async with _client(None if failure == "unconfigured" else provider) as client:
        for url in ("/v1/me/teams", "/v1/me/team-membership?team_id=engineering"):
            response = await client.get(url)
            assert response.status_code == 503
            assert set(response.json()) == {"detail"}
            assert "private-provider-token" not in response.text
    assert "private-provider-token" not in caplog.text


async def test_membership_requires_a_nonempty_team_id() -> None:
    provider = Mock(spec=TeamProvider)
    async with _client(provider) as client:
        for url in ("/v1/me/team-membership", "/v1/me/team-membership?team_id="):
            assert (await client.get(url)).status_code == 422
    provider.list_teams.assert_not_called()


async def test_create_app_loads_provider_once_and_mounts_authenticated_routes(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = Mock(spec=TeamProvider)
    provider.list_teams.return_value = [_TEAM]
    loader = Mock(return_value=provider)
    monkeypatch.setattr("omnigent.team_providers.load_team_provider", loader)
    monkeypatch.setenv("OMNIGENT_AUTH_HEADER", "X-Forwarded-Email")
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "false")
    app = request.getfixturevalue("auth_app")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/me/teams")).status_code == 401
        for url in ("/v1/me/teams", f"/v1/me/team-membership?team_id={_TEAM.id}"):
            response = await client.get(url, headers={"X-Forwarded-Email": _USER})
            assert response.status_code == 200
            assert response.headers["cache-control"] == "private, no-store"

    loader.assert_called_once_with()
    assert provider.list_teams.await_count == 2
    provider.list_teams.assert_awaited_with(_USER)
