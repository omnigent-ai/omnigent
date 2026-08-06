"""Resolve a user's GitHub access token for the credential broker.

Bridges the connection store and the GitHub App client: reads the stored
connection and transparently refreshes an expired access token, so the broker
always vends a currently-valid token. See ``designs/CREDENTIAL_STORE.md``.
"""

from __future__ import annotations

import logging

from omnigent.connections.github import GithubConnectionStore
from omnigent.db.utils import now_epoch
from omnigent.server.github_app import GitHubAppError
from omnigent.server.github_app_client import GitHubAppClient

_logger = logging.getLogger(__name__)

# Refresh a token that expires within this margin so it does not lapse
# mid-launch (or shortly after) inside the sandbox.
_REFRESH_MARGIN_S = 300


async def resolve_access_token(
    user_id: str,
    *,
    store: GithubConnectionStore,
    client: GitHubAppClient,
) -> str | None:
    """Resolve a valid user access token for *user_id*, or ``None``.

    Reads the stored connection and transparently refreshes a token that is
    at/near expiry (persisting the refresh). Best-effort: any failure (no
    connection, no refresh token, refresh rejected) returns ``None``.

    :param user_id: The user whose token to resolve.
    :param store: The connection store (also used to persist a refresh).
    :param client: The GitHub App client.
    :returns: A usable access token, or ``None``.
    """
    connection = await _run_sync(store.get, user_id, with_tokens=True)
    if connection is None or not connection.access_token:
        return None
    access_token = connection.access_token
    expires_at = connection.token_expires_at
    if expires_at is not None and expires_at <= now_epoch() + _REFRESH_MARGIN_S:
        if not connection.refresh_token:
            return None
        try:
            refreshed = await client.refresh_token(connection.refresh_token)
        except GitHubAppError as exc:
            _logger.warning("GitHub token refresh failed for %s: %s", user_id, exc)
            return None
        await _run_sync(store.update_tokens, user_id, refreshed)
        access_token = refreshed.access_token
    return access_token


async def _run_sync(func, /, *args, **kwargs):
    """Run a synchronous store call off the event loop."""
    import asyncio

    return await asyncio.to_thread(lambda: func(*args, **kwargs))
