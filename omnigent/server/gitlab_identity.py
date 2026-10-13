"""Resolve and refresh brokered GitLab OAuth credentials."""

from __future__ import annotations

import asyncio
import logging

import httpx

from omnigent.connections.gitlab import GitlabConnectionStore
from omnigent.db.utils import now_epoch
from omnigent.server.gitlab_app import GitLabAppError
from omnigent.server.gitlab_app_client import GitLabAppClient

_logger = logging.getLogger(__name__)
_REFRESH_MARGIN_S = 300
_GIT_TOKEN_USERNAME = "oauth2"


async def resolve_access_token(
    user_id: str, *, store: GitlabConnectionStore, client: GitLabAppClient
) -> str | None:
    """Return a valid token, refreshing it before expiry when possible."""
    connection = await asyncio.to_thread(store.get, user_id, with_tokens=True)
    if connection is None or not connection.access_token:
        return None
    if (
        connection.token_expires_at is None
        or connection.token_expires_at > now_epoch() + _REFRESH_MARGIN_S
    ):
        return connection.access_token
    if connection.refresh_token:
        try:
            refreshed = await client.refresh_token(connection.refresh_token)
            await asyncio.to_thread(store.update_tokens, user_id, refreshed)
            return refreshed.access_token
        except (GitLabAppError, httpx.HTTPError, ValueError) as exc:
            _logger.warning("GitLab token refresh failed for %s: %s", user_id, exc)
    return connection.access_token if connection.token_expires_at > now_epoch() else None


async def resolve_gitlab_credential(
    user_id: str, *, store: GitlabConnectionStore, client: GitLabAppClient
) -> dict[str, object] | None:
    """Provider adapter for the generic host credential broker."""
    token = await resolve_access_token(user_id, store=store, client=client)
    if token is None:
        return None
    connection = await asyncio.to_thread(store.get, user_id)
    return {
        "username": _GIT_TOKEN_USERNAME,
        "token": token,
        "login": connection.gitlab_login if connection else None,
        "host": connection.gitlab_host if connection else None,
    }
