"""GitLab OAuth connection and project-discovery routes."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from httpx import HTTPError

from omnigent.connections.gitlab import GitlabConnectionStore
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.gitlab_app import GitLabAppConfig, GitLabAppError, build_authorize_url
from omnigent.server.gitlab_app_client import GitLabAppClient
from omnigent.server.gitlab_identity import resolve_access_token
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes.connections_base import (
    ConnectionError,
    ConnectStart,
    create_connection_router,
)

_logger = logging.getLogger(__name__)
_STATE_KEY_INFO = b"omnigent.connections.gitlab.oauth-state.v1"
_PROJECT_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


def _derive_state_signing_key(client_secret: str) -> bytes:
    return hmac.new(client_secret.encode(), _STATE_KEY_INFO, hashlib.sha256).digest()


def _valid_project_path(value: str) -> bool:
    parts = value.split("/")
    return bool(parts) and all(
        _PROJECT_SEGMENT.fullmatch(part) and ".." not in part for part in parts
    )


class GitlabConnectionHooks:
    provider = "gitlab"

    def __init__(
        self,
        config: GitLabAppConfig,
        store: GitlabConnectionStore,
        client: GitLabAppClient | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.api = client or GitLabAppClient(config)

    def signing_key(self) -> bytes:
        return _derive_state_signing_key(self.config.client_secret)

    def status_fields(self, connection: Any | None) -> dict[str, Any]:
        return {
            "login": connection.gitlab_login if connection else None,
            "host": connection.gitlab_host if connection else self.config.host,
            "scopes": connection.scopes if connection else None,
        }

    def begin(self, request: Request, build_state: Any) -> ConnectStart | None:
        del request
        return ConnectStart(build_authorize_url(self.config, state=build_state({})))

    async def complete(self, user_id: str, code: str, claims: dict[str, Any]) -> None:
        del claims
        try:
            tokens = await self.api.exchange_code(code)
            login, gitlab_user_id = await self.api.fetch_login(tokens.access_token)
        except (GitLabAppError, HTTPError, ValueError) as exc:
            raise ConnectionError(str(exc)) from exc
        await asyncio.to_thread(
            self.store.upsert,
            user_id,
            gitlab_login=login,
            gitlab_user_id=gitlab_user_id,
            gitlab_host=self.config.host,
            tokens=tokens,
        )
        _logger.info("GitLab account %s connected for %s", login, user_id)


def create_connections_gitlab_router(
    config: GitLabAppConfig,
    store: GitlabConnectionStore,
    *,
    auth_provider: AuthProvider | None = None,
    client: GitLabAppClient | None = None,
):
    """Build the standard connection flow plus GitLab project/branch picker endpoints."""
    hooks = GitlabConnectionHooks(config, store, client)
    api = hooks.api

    def _current_user(request: Request) -> str:
        return require_user(request, auth_provider) or RESERVED_USER_LOCAL

    def _project_routes(router: APIRouter) -> None:
        @router.get("/connections/gitlab/repos")
        async def repos(request: Request) -> dict[str, object]:
            user_id = _current_user(request)
            token = await resolve_access_token(user_id, store=store, client=api)
            if token is None:
                return {"connected": False, "repos": [], "truncated": False}
            try:
                repo_list, truncated = await api.list_repos(token)
            except (GitLabAppError, HTTPError, ValueError) as exc:
                _logger.warning("GitLab project list failed for %s: %s", user_id, exc)
                raise HTTPException(
                    status_code=502, detail="Failed to list GitLab projects"
                ) from exc
            return {"connected": True, "repos": repo_list, "truncated": truncated}

        @router.get("/connections/gitlab/repos/{project_path:path}/branches")
        async def branches(request: Request, project_path: str) -> dict[str, object]:
            if not _valid_project_path(project_path):
                raise HTTPException(status_code=400, detail="Invalid project path")
            token = await resolve_access_token(_current_user(request), store=store, client=api)
            if token is None:
                return {"connected": False, "branches": []}
            try:
                return {
                    "connected": True,
                    "branches": await api.list_branches(token, project_path),
                }
            except (GitLabAppError, HTTPError, ValueError) as exc:
                _logger.warning("GitLab branch list failed for %s: %s", project_path, exc)
                raise HTTPException(
                    status_code=502, detail="Failed to list GitLab branches"
                ) from exc

    return create_connection_router(
        hooks, auth_provider=auth_provider, extra_routes=_project_routes
    )
