"""Read-only directory adapters; deployment operators supply credentials.

Okta: https://developer.okta.com/docs/api/openapi/okta-management/management/tag/UserResources/
Google: https://developers.google.com/workspace/admin/directory/reference/rest/v1/groups/list
"""

from __future__ import annotations

import os
from urllib.parse import quote, urlsplit

import httpx

from omnigent.team_providers import Team, TeamProvider


class OktaTeamProvider(TeamProvider):
    """Look up groups using the authenticated identity as an exact Okta login or ID."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._origin = os.environ["OMNIGENT_OKTA_ORG_URL"].strip().rstrip("/")
        url = urlsplit(self._origin)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path
            or url.query
            or url.fragment
        ):
            raise ValueError("OMNIGENT_OKTA_ORG_URL must be an HTTPS origin")
        self._authorization = "SSWS " + os.environ["OMNIGENT_OKTA_API_TOKEN"].strip()
        if self._authorization == "SSWS ":
            raise ValueError("OMNIGENT_OKTA_API_TOKEN must not be empty")
        self._transport = transport

    async def list_teams(self, user_id: str) -> list[Team]:
        """Read the complete group list; no identity mapping or synchronization."""
        if user_id in {".", ".."} or "/" in user_id:
            raise ValueError("Okta requires a login without slashes or an immutable user ID")
        async with httpx.AsyncClient(
            timeout=10, follow_redirects=False, transport=self._transport
        ) as client:
            response = await client.get(
                f"{self._origin}/api/v1/users/{quote(user_id, safe='')}/groups",
                headers={"Authorization": self._authorization},
            )
            response.raise_for_status()
            if response.links.get("next"):
                raise ValueError("Unexpected pagination in Okta user groups response")
            groups = response.json()
            if not isinstance(groups, list):
                raise ValueError("Expected an Okta group list")
            return [Team(id=group["id"], name=group["profile"]["name"]) for group in groups]


class GoogleTeamProvider(TeamProvider):
    """Read Google Workspace group membership using an externally managed access token."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._authorization = "Bearer " + os.environ["OMNIGENT_GOOGLE_GROUPS_ACCESS_TOKEN"].strip()
        if self._authorization == "Bearer ":
            raise ValueError("OMNIGENT_GOOGLE_GROUPS_ACCESS_TOKEN must not be empty")
        self._transport = transport

    async def list_teams(self, user_id: str) -> list[Team]:
        """List groups for an email or immutable user ID, following all returned pages."""
        params = {"userKey": user_id, "maxResults": "200"}
        teams: list[Team] = []
        async with httpx.AsyncClient(
            timeout=10, follow_redirects=False, transport=self._transport
        ) as client:
            # ponytail: cap directory reads at 5,000 groups; use a paged contract if needed.
            for _ in range(25):
                response = await client.get(
                    "https://admin.googleapis.com/admin/directory/v1/groups",
                    params=params,
                    headers={"Authorization": self._authorization},
                )
                response.raise_for_status()
                payload = response.json()
                groups = payload.get("groups", [])
                if not isinstance(groups, list):
                    raise ValueError("Expected a Google group list")
                teams.extend(
                    Team(id=group["id"], name=group.get("name") or group["email"])
                    for group in groups
                )
                next_page = payload.get("nextPageToken")
                if next_page is None or next_page == "":
                    return teams
                if not isinstance(next_page, str):
                    raise ValueError("Expected a Google page token")
                params["pageToken"] = next_page
        raise ValueError("Google group list exceeded the pagination limit")
