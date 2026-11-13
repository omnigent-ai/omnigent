"""Short-lived tokens for reading a session's workspace directly from its host.

The host sends a random key in its hello. The server, after checking that the
caller owns the session, signs a token with that key naming the session and its
workspace. The host verifies tokens locally, so a direct read costs no server
call. :data:`TOKEN_TTL_S` bounds how long a revoked owner keeps direct access.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

import jwt

TOKEN_TTL_S = 300
_AUDIENCE = "omnigent-workspace"
_ALGORITHM = "HS256"


@dataclass(frozen=True)
class WorkspaceGrant:
    """What a verified token lets its bearer read.

    :param session_id: Session the token was minted for.
    :param workspace: The session's workspace on the host, as the server
        records it, e.g. ``"~/src/project"``.
    """

    session_id: str
    workspace: str


def new_token_key() -> str:
    """Return a fresh 256-bit signing key for one host process."""
    return secrets.token_urlsafe(32)


def mint_token(
    key: str, *, host_id: str, session_id: str, workspace: str, now: float | None = None
) -> tuple[str, int]:
    """Sign a token for one session's workspace on one host.

    :param key: The host's key from its hello.
    :param host_id: Host the token is for, e.g. ``"host_a1b2c3d4"``.
    :param session_id: Session the caller owns.
    :param workspace: The session's recorded workspace.
    :param now: Issue time in Unix seconds; defaults to the current time.
    :returns: The token and its expiry in Unix seconds.
    """
    issued = int(time.time() if now is None else now)
    expires = issued + TOKEN_TTL_S
    claims = {
        "aud": _AUDIENCE,
        "hid": host_id,
        "sub": session_id,
        "ws": workspace,
        "iat": issued,
        "exp": expires,
    }
    return jwt.encode(claims, key, algorithm=_ALGORITHM), expires


def verify_token(key: str, token: str, *, host_id: str) -> WorkspaceGrant | None:
    """Return the grant in a valid, unexpired token for this host, else ``None``."""
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=[_ALGORITHM],
            audience=_AUDIENCE,
            # iat is informational: a host clock behind the server's must not
            # reject a fresh token. exp still bounds every token.
            options={"require": ["exp", "hid", "sub", "ws"], "verify_iat": False},
        )
    except jwt.PyJWTError:
        return None
    if claims["hid"] != host_id:
        return None
    return WorkspaceGrant(session_id=str(claims["sub"]), workspace=str(claims["ws"]))
