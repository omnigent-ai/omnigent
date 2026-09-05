"""Resolve a user's GitHub access token for the credential broker.

Bridges the connection store and the GitHub App client: reads the stored
connection and transparently refreshes an expired access token, so the broker
always vends a currently-valid token. See ``designs/CREDENTIAL_STORE.md``.
"""

from __future__ import annotations

import logging

import httpx

from omnigent.connections.github import GithubConnectionStore
from omnigent.db.utils import now_epoch
from omnigent.server.github_app import GitHubAppError
from omnigent.server.github_app_client import GitHubAppClient

_logger = logging.getLogger(__name__)

# Refresh a token that expires within this margin so it does not lapse
# mid-launch (or shortly after) inside the sandbox.
_REFRESH_MARGIN_S = 300

# The username git uses with a token over HTTPS (``https://<user>:<token>@…``);
# GitHub ignores the value but requires a non-empty one.
_GIT_TOKEN_USERNAME = "x-access-token"


async def resolve_access_token(
    user_id: str,
    *,
    store: GithubConnectionStore,
    client: GitHubAppClient,
) -> str | None:
    """Resolve a valid user access token for *user_id*, or ``None``.

    Reads the stored connection and, when the token is at/near expiry, refreshes
    it (persisting the new token). Best-effort and **non-raising**: a transient
    refresh failure (network/timeout, a rejected refresh, a malformed response)
    never discards a token that is still valid and never propagates — the broker
    degrades to ``{"connected": false}`` rather than a 500.

    :param user_id: The user whose token to resolve.
    :param store: The connection store (also used to persist a refresh).
    :param client: The GitHub App client.
    :returns: A usable access token, or ``None``.
    """
    connection = await _run_sync(store.get, user_id, with_tokens=True)
    if connection is None or not connection.access_token:
        return None
    expires_at = connection.token_expires_at
    # Non-expiring, or comfortably ahead of the margin: use as-is.
    if expires_at is None or expires_at > now_epoch() + _REFRESH_MARGIN_S:
        return connection.access_token
    refreshed = await _try_refresh(user_id, connection.refresh_token, store=store, client=client)
    if refreshed is not None:
        return refreshed
    # Refresh could not produce a new token; the current one is still usable
    # until it actually lapses (up to the margin remains), so prefer it and only
    # give up once it has truly expired.
    if expires_at > now_epoch():
        return connection.access_token
    return None


async def _try_refresh(
    user_id: str,
    refresh_token: str | None,
    *,
    store: GithubConnectionStore,
    client: GitHubAppClient,
) -> str | None:
    """Refresh and persist the user's token; ``None`` on any failure.

    Catches every expected failure so the caller never sees an exception: no
    refresh token, a non-200 (:class:`GitHubAppError`), a transient
    network/timeout (:class:`httpx.HTTPError`), or a malformed token payload
    (:class:`ValueError` from parsing ``expires_in``). A persist failure keeps
    the freshly minted token rather than dropping it.
    """
    if not refresh_token:
        return None
    try:
        refreshed = await client.refresh_token(refresh_token)
    except (GitHubAppError, httpx.HTTPError, ValueError) as exc:
        _logger.warning("GitHub token refresh failed for %s: %s", user_id, exc)
        return None
    try:
        await _run_sync(store.update_tokens, user_id, refreshed)
    except Exception as exc:  # noqa: BLE001 - a persist error must not drop a minted token
        _logger.warning("GitHub token refresh could not be persisted for %s: %s", user_id, exc)
    return refreshed.access_token


async def resolve_github_credential(
    user_id: str,
    *,
    store: GithubConnectionStore,
    client: GitHubAppClient,
) -> dict[str, object] | None:
    """Resolve the GitHub broker payload for *user_id*, or ``None``.

    The provider adapter the generic credential broker
    (:mod:`omnigent.server.routes.host_credentials`) calls: returns the vended
    token plus the attribution metadata git needs (``username``/``login``), or
    ``None`` when the owner has not linked GitHub. The ``owner``/``login`` let
    the host attribute commits to the human, decoupled from the push credential.
    """
    token = await resolve_access_token(user_id, store=store, client=client)
    if token is None:
        return None
    connection = await _run_sync(store.get, user_id)
    return {
        "username": _GIT_TOKEN_USERNAME,
        "token": token,
        "login": connection.github_login if connection is not None else None,
    }


async def _run_sync(func, /, *args, **kwargs):
    """Run a synchronous store call off the event loop."""
    import asyncio

    return await asyncio.to_thread(lambda: func(*args, **kwargs))
