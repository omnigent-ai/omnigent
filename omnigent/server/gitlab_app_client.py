"""Async OAuth and REST client for a configured GitLab instance."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from omnigent.server.gitlab_app import (
    GitLabAppConfig,
    GitLabAppError,
    GitLabTokenSet,
    token_set_from_payload,
)

_HTTP_TIMEOUT_S = 15.0
_PER_PAGE = 100
_MAX_PAGES = 3


class GitLabAppClient:
    """Small stateless client for OAuth, project discovery, and branch discovery."""

    def __init__(
        self, config: GitLabAppConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._config = config
        self._transport = transport

    def _http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, transport=self._transport)

    @property
    def host(self) -> str:
        return self._config.host

    async def exchange_code(self, code: str) -> GitLabTokenSet:
        return await self._token_request(self._config.code_exchange_fields(code))

    async def refresh_token(self, refresh_token: str) -> GitLabTokenSet:
        return await self._token_request(self._config.token_refresh_fields(refresh_token))

    async def fetch_login(self, access_token: str) -> tuple[str, int]:
        data = await self._get_json("/api/v4/user", access_token)
        username, user_id = data.get("username"), data.get("id")
        if not username or not isinstance(user_id, (str, int)):
            raise GitLabAppError("GitLab /user response missing username/id")
        try:
            return str(username), int(user_id)
        except ValueError as exc:
            raise GitLabAppError("GitLab /user response has invalid id") from exc

    async def list_repos(self, access_token: str) -> tuple[list[dict[str, object]], bool]:
        projects, truncated = await self._paged(
            "/api/v4/projects",
            access_token,
            {"membership": "true", "order_by": "last_activity_at", "sort": "desc"},
        )
        return [
            {
                "full_name": project["path_with_namespace"],
                "clone_url": project.get("http_url_to_repo"),
                "default_branch": project.get("default_branch"),
                "private": project.get("visibility") != "public",
                "pushed_at": project.get("last_activity_at"),
            }
            for project in projects
            if isinstance(project, dict) and project.get("path_with_namespace")
        ], truncated

    async def list_branches(self, access_token: str, project_path: str) -> list[str]:
        encoded = quote(project_path, safe="")
        branches, _ = await self._paged(
            f"/api/v4/projects/{encoded}/repository/branches", access_token, {}
        )
        return [
            str(branch["name"])
            for branch in branches
            if isinstance(branch, dict) and branch.get("name")
        ]

    async def _token_request(self, fields: dict[str, str]) -> GitLabTokenSet:
        async with self._http_client() as client:
            response = await client.post(
                f"{self.host}/oauth/token", data=fields, headers={"Accept": "application/json"}
            )
        if response.status_code != 200:
            raise GitLabAppError(f"GitLab token endpoint returned {response.status_code}")
        return token_set_from_payload(response.json())

    async def _get_json(self, path: str, access_token: str) -> dict[str, object]:
        async with self._http_client() as client:
            response = await client.get(
                f"{self.host}{path}", headers={"Authorization": f"Bearer {access_token}"}
            )
        if response.status_code != 200:
            raise GitLabAppError(f"GitLab {path} returned {response.status_code}")
        data = response.json()
        if not isinstance(data, dict):
            raise GitLabAppError(f"GitLab {path} returned an invalid response")
        return data

    async def _paged(
        self, path: str, access_token: str, params: dict[str, str]
    ) -> tuple[list[object], bool]:
        values: list[object] = []
        truncated = False
        async with self._http_client() as client:
            for page in range(1, _MAX_PAGES + 1):
                response = await client.get(
                    f"{self.host}{path}",
                    params={**params, "per_page": _PER_PAGE, "page": page},
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if response.status_code != 200:
                    raise GitLabAppError(f"GitLab {path} returned {response.status_code}")
                batch = response.json()
                if not isinstance(batch, list) or not batch:
                    break
                values.extend(batch)
                if len(batch) < _PER_PAGE:
                    break
            else:
                truncated = True
        return values, truncated
