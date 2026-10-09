"""Front-door bearer for sandbox hosts behind an authenticating reverse proxy.

A server-managed sandbox host authenticates to the Omnigent server with its
launch token (``X-Omnigent-Host-Token``). When the server sits behind a proxy
that itself demands a bearer — the Databricks Apps OAuth proxy, which rejects
PATs and redirects unauthenticated requests to a login page — neither the
host nor the runners it spawns can get past the proxy with the launch token
alone.

The launcher (which runs inside the server and holds a proxy-acceptable
credential) writes a short-lived bearer into a private file in the sandbox and
refreshes it on every start, resume, and keepalive. ``OMNIGENT_PROXY_BEARER_FILE``
names that file. Host tunnel/HTTP calls and runner callbacks send its contents
as ``Authorization: Bearer …``; the file is re-read on every call so a refresh
is picked up on the next request or reconnect. The server's own identity
check is unchanged: managed hosts still resolve by launch token.
"""

from __future__ import annotations

import os
from pathlib import Path

PROXY_BEARER_FILE_ENV_VAR = "OMNIGENT_PROXY_BEARER_FILE"


def proxy_bearer_configured() -> bool:
    """Whether this process was launched with a proxy-bearer file."""
    return bool(os.environ.get(PROXY_BEARER_FILE_ENV_VAR, "").strip())


def read_proxy_bearer() -> str | None:
    """Return the current proxy bearer, or ``None`` when unset or unreadable."""
    path = os.environ.get(PROXY_BEARER_FILE_ENV_VAR, "").strip()
    if not path:
        return None
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None
