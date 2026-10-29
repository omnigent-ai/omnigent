"""Front-door bearer for sandbox hosts behind an authenticating reverse proxy.

The launcher writes the owner's short-lived bearer into a private sandbox file
(named by ``OMNIGENT_PROXY_BEARER_FILE``) and refreshes it on start, resume, and
keepalive, so the host and its runners get past the proxy (e.g. Databricks Apps).
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

PROXY_BEARER_FILE_ENV_VAR = "OMNIGENT_PROXY_BEARER_FILE"
#: The only server the bearer may be sent to, set next to the file by the launcher.
PROXY_BEARER_SERVER_URL_ENV_VAR = "OMNIGENT_PROXY_BEARER_SERVER_URL"


def proxy_bearer_configured() -> bool:
    """Whether this process was launched with a proxy-bearer file."""
    return bool(os.environ.get(PROXY_BEARER_FILE_ENV_VAR, "").strip())


def _normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


def proxy_bearer_allowed_for(server_url: str) -> bool:
    """Whether the bearer may be sent to *server_url* (the launcher-configured server)."""
    allowed = os.environ.get(PROXY_BEARER_SERVER_URL_ENV_VAR, "").strip()
    return bool(allowed) and _normalize_url(allowed) == _normalize_url(server_url)


def read_proxy_bearer(server_url: str | None = None) -> str | None:
    """Return the current proxy bearer, or ``None`` when unset or unreadable.

    :param server_url: The server the caller is about to call, e.g.
        ``"https://omnigent.example.com"``. When given, the bearer is returned
        only if it matches the launcher-configured server, so the owner's token
        is never sent anywhere else. ``None`` is for the host's own server calls.
    """
    path = os.environ.get(PROXY_BEARER_FILE_ENV_VAR, "").strip()
    if not path:
        return None
    if server_url is not None and not proxy_bearer_allowed_for(server_url):
        return None
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return token or None
