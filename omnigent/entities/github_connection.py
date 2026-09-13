"""Per-user GitHub App connection entity.

Plain dataclass returned from :class:`GithubConnectionStore`. Token
material is carried encrypted in the store row; the two token fields
here hold the *decrypted* values and are only ever populated on the
server-side launch path, never serialized to a client.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class GithubConnection:
    """A user's connected GitHub account.

    :param user_id: The omnigent user id (email in OIDC/header mode,
        username in accounts mode) the connection belongs to.
    :param github_login: The connected GitHub login, e.g. ``"octocat"``.
    :param github_user_id: The connected GitHub numeric user id.
    :param access_token: Decrypted user access token, or ``None`` when
        the store returned a metadata-only view (status endpoints).
    :param refresh_token: Decrypted refresh token, or ``None``.
    :param token_expires_at: Unix epoch seconds the access token expires
        at, or ``None`` for non-expiring tokens.
    :param refresh_token_expires_at: Unix epoch seconds the refresh token
        expires at, or ``None``.
    :param scopes: Space-separated granted scopes.
    :param created_at: Unix epoch seconds the connection was first made.
    :param updated_at: Unix epoch seconds of the last token refresh /
        reconnect.
    """

    user_id: str
    github_login: str
    github_user_id: int
    access_token: str | None
    refresh_token: str | None
    token_expires_at: int | None
    refresh_token_expires_at: int | None
    scopes: str
    created_at: int
    updated_at: int
