"""CLI-side auth storage for ``omnigent login``.

Persists per-server auth state in ``~/.omnigent/auth_tokens.json``
keyed by server URL. Two record shapes live side by side:

- **Session JWTs** from the browser-based OIDC / accounts login flow
  (``{"token": ..., "user_id": ..., "expires_at": ...}``).
- **Databricks Apps pointer records**
  (``{"auth_type": "databricks", "workspace_host": ...}``) written by
  ``omnigent login <apps-url>``. These deliberately store NO token:
  Databricks OAuth access tokens expire after ~1 hour, so the record
  just names the workspace whose host-keyed Databricks CLI OAuth cache
  (``databricks auth login --host <ws>``) mints fresh bearers on
  demand.

See ``designs/OIDC_AUTH.md`` §CLI Login Flow.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time
from pathlib import Path

_logger = logging.getLogger(__name__)
_TOKEN_FILE_NAME = "auth_tokens.json"


def _token_file_path() -> Path:
    """Return the path to the auth token storage file.

    Uses the shared ``~/.omnigent`` state directory.

    :returns: Path to ``~/.omnigent/auth_tokens.json``.
    """
    from omnigent_ui_sdk.terminal._config import state_dir

    return state_dir() / _TOKEN_FILE_NAME


def _normalize_server_url(server_url: str) -> str:
    """Normalize a server URL for use as a dict key.

    Strips trailing slashes so ``http://localhost:6767`` and
    ``http://localhost:6767/`` resolve to the same entry.

    :param server_url: The server URL to normalize.
    :returns: Normalized URL string.
    """
    return server_url.rstrip("/")


def _store_entry(server_url: str, entry: dict[str, str | float]) -> None:
    """Create or update a server's record in the auth-tokens file.

    Writes ``~/.omnigent/auth_tokens.json`` with user-only
    read/write permissions (``0o600``) — the file may hold session
    JWTs, which are sensitive.

    :param server_url: The server URL the record is keyed by, e.g.
        ``"http://localhost:6767"``.
    :param entry: The record to store, e.g.
        ``{"token": "...", "user_id": "...", "expires_at": 1750000000.0}``.
    """
    path = _token_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, dict[str, str | float]] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}

    data[_normalize_server_url(server_url)] = entry

    path.write_text(json.dumps(data, indent=2))
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def store_token(
    server_url: str,
    token: str,
    user_id: str,
    expires_at: float,
) -> None:
    """Persist a session token for a server.

    :param server_url: The server URL, e.g.
        ``"http://localhost:6767"``.
    :param token: The session JWT string.
    :param user_id: The authenticated user's email, e.g.
        ``"alice@example.com"``.
    :param expires_at: Unix timestamp when the token expires.
    """
    _store_entry(
        server_url,
        {
            "token": token,
            "user_id": user_id,
            "expires_at": expires_at,
        },
    )


def store_databricks_auth(
    server_url: str,
    workspace_host: str,
    user_id: str | None = None,
    org_id: str | None = None,
) -> None:
    """Persist a Databricks Apps auth pointer record for a server.

    Unlike :func:`store_token` this stores no bearer: Databricks OAuth
    access tokens expire after ~1 hour, so the record only names the
    workspace host whose ``databricks auth login --host <ws>`` OAuth
    cache the auth chain should mint fresh tokens from (see
    ``omnigent.inner.databricks_executor._resolve_databricks_auth``).

    :param server_url: The Databricks Apps server URL, e.g.
        ``"https://myapp-123.aws.databricksapps.com"``.
    :param workspace_host: The workspace that fronts the app, e.g.
        ``"https://example.databricks.com"``.
    :param user_id: The authenticated user's email when known, e.g.
        ``"alice@example.com"``. Display-only.
    :param org_id: The workspace org id when known (from the
        ``x-databricks-org-id`` response header), e.g.
        ``"2850744067564480"``. Used to build workspace web-UI links
        (the ``?o=`` query param).
    """
    entry: dict[str, str | float] = {
        "auth_type": "databricks",
        "workspace_host": workspace_host.rstrip("/"),
    }
    if user_id:
        entry["user_id"] = user_id
    if org_id:
        entry["org_id"] = org_id
    _store_entry(server_url, entry)


def _load_entry(server_url: str) -> dict[str, str | float] | None:
    """Load the raw stored record for a server, if any.

    :param server_url: The server URL, e.g.
        ``"http://localhost:6767"``.
    :returns: The stored record dict, or ``None`` when the file or
        entry is missing/unreadable.
    """
    path = _token_file_path()
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    entry = data.get(_normalize_server_url(server_url))
    return entry if isinstance(entry, dict) else None


def load_token(server_url: str) -> str | None:
    """Load a stored session token for a server.

    Returns ``None`` if no token is stored, the token has expired,
    or the file is unreadable. Databricks pointer records (which hold
    no token) also return ``None`` — resolve those via
    :func:`load_databricks_workspace_host` instead.

    :param server_url: The server URL, e.g.
        ``"http://localhost:6767"``.
    :returns: The session JWT string, or ``None``.
    """
    entry = _load_entry(server_url)
    if entry is None:
        return None

    expires_at = entry.get("expires_at", 0)
    if isinstance(expires_at, (int, float)) and expires_at < time.time():
        _logger.debug("Stored token for %s has expired", _normalize_server_url(server_url))
        return None

    token = entry.get("token")
    return token if isinstance(token, str) else None


def load_databricks_workspace_host(server_url: str) -> str | None:
    """Load the workspace host from a Databricks Apps pointer record.

    :param server_url: The server URL, e.g.
        ``"https://myapp-123.aws.databricksapps.com"``.
    :returns: The workspace host, e.g.
        ``"https://example.databricks.com"``, or ``None`` when the
        stored record (if any) is not a Databricks pointer record.
    """
    entry = _load_entry(server_url)
    if entry is None or entry.get("auth_type") != "databricks":
        return None
    host = entry.get("workspace_host")
    return host if isinstance(host, str) and host else None


def load_databricks_org_id(server_url: str) -> str | None:
    """Load the workspace org id from a Databricks pointer record.

    :param server_url: The server URL, e.g.
        ``"https://example.databricks.com/api/2.0/omnigent"``.
    :returns: The org id, e.g. ``"2850744067564480"``, or ``None``
        when the stored record (if any) is not a Databricks pointer
        record or carries no org id.
    """
    entry = _load_entry(server_url)
    if entry is None or entry.get("auth_type") != "databricks":
        return None
    org_id = entry.get("org_id")
    return org_id if isinstance(org_id, str) and org_id else None


# Workspace-routing header. When a Databricks host fronts many workspaces
# under one hostname, the bare host is the account; the API proxy routes a
# workspace request by this header (equivalently to the ``?o=`` query param).
DATABRICKS_ORG_ID_HEADER = "X-Databricks-Org-Id"


# Opaque extra request headers for dev/test: a JSON object of header name→value
# in :data:`DATABRICKS_EXTRA_HEADERS_ENV_VAR`. Databricks deployments use it to
# carry request-routing selector headers so a request pins to a specific server
# instance/replica instead of the default one. Folded into
# :func:`databricks_request_headers` below so it travels with every
# client→server connection built through that one helper — a per-call-site
# bearer that skips this helper misses the selectors. Unset in prod.
DATABRICKS_EXTRA_HEADERS_ENV_VAR = "OMNIGENT_DATABRICKS_EXTRA_HEADERS"


def _databricks_extra_headers() -> dict[str, str]:
    """Return the opaque extra request headers when configured, else ``{}``.

    Reads :data:`DATABRICKS_EXTRA_HEADERS_ENV_VAR`, a JSON object of header
    name→value. Missing or malformed (unset, not JSON, or not an object) →
    ``{}``, so production and local runs are unaffected.

    :returns: A header dict parsed from the env var, or an empty dict.
    """
    raw = os.environ.get(DATABRICKS_EXTRA_HEADERS_ENV_VAR, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(value) for key, value in parsed.items()}


def databricks_request_headers(
    server_url: str, *, bearer_token: str | None = None
) -> dict[str, str]:
    """Build the headers for a request to a Databricks-fronted server.

    The single source of truth for server-request headers. It always
    includes the :data:`DATABRICKS_ORG_ID_HEADER` workspace-routing header
    when ``omnigent login https://<host>/?o=<id>`` recorded a selector, and
    adds ``Authorization`` when a bearer is supplied. Folding both into one
    builder makes routing travel with auth: a caller that has a token gets
    routing for free, and a caller whose credential is set elsewhere (an
    httpx ``Auth`` that mints per request, or the managed-host token header)
    omits the token and still gets routing.

    Both values are omitted when absent, so single-workspace and
    local-unauthenticated callers get ``{}`` and are unaffected.

    Also folds in any opaque dev/test headers from
    :data:`DATABRICKS_EXTRA_HEADERS_ENV_VAR` (request-routing selectors set by
    some Databricks deployments) so every chokepoint that builds headers through
    this one helper carries them when set.

    :param server_url: The server URL, e.g.
        ``"https://example.databricks.com/api/2.0/omnigent"``.
    :param bearer_token: The workspace bearer token, or ``None`` when the
        credential is supplied by a separate mechanism (or there is none).
    :returns: A header dict carrying ``Authorization``, ``X-Databricks-Org-Id``,
        and/or the configured extra headers as available, possibly empty.
    """
    headers: dict[str, str] = {}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    org_id = load_databricks_org_id(server_url)
    if org_id:
        headers[DATABRICKS_ORG_ID_HEADER] = org_id
    # Opaque dev/test extra headers (request-routing selectors); no-op in prod
    # (env unset).
    headers.update(_databricks_extra_headers())
    return headers


def clear_token(server_url: str) -> None:
    """Remove a stored token for a server.

    No-op if no token is stored or the file doesn't exist.

    :param server_url: The server URL, e.g.
        ``"http://localhost:6767"``.
    """
    path = _token_file_path()
    if not path.exists():
        return

    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return

    key = _normalize_server_url(server_url)
    if key in data:
        del data[key]
        path.write_text(json.dumps(data, indent=2))
