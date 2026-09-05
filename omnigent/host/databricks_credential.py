"""Materialize the session owner's Databricks credential in a managed sandbox.

Called by ``omnigent host`` at startup (executor-agnostic, like
:func:`omnigent.git_credential_github.configure_host_git`). When the owner has
linked a Databricks workspace via the OAuth U2M connect flow, the host fetches
their (server-refreshed) token from the generic credential broker
(:mod:`omnigent.server.routes.host_credentials`, provider ``databricks``) and writes it as a
``~/.databrickscfg`` profile plus ``DATABRICKS_CONFIG_PROFILE``. The existing
opencode-native gateway (:func:`omnigent.opencode_native_provider.resolve_databricks_gateway`)
and the Databricks MCP token resolver both key off that profile, so agent model
serving and MCP calls route through the user's Databricks AI Gateway
(``https://<workspace>/serving-endpoints``) as them — no per-executor wiring.

Best-effort: a no-op when the host token is absent, the broker is unreachable,
the feature is off, or the owner hasn't connected Databricks (the sandbox then
keeps whatever ambient ``~/.databrickscfg`` it already had). The token is a
short-lived OAuth access token; it is re-fetched (and server-side refreshed) on
each host launch, so a long-lived session should relaunch to refresh it.
"""

from __future__ import annotations

import configparser
import contextlib
import logging
import os
from pathlib import Path

import httpx

from omnigent.host.identity import HOST_TOKEN_ENV_VAR, MANAGED_HOST_TOKEN_HEADER

_logger = logging.getLogger(__name__)

_TIMEOUT_S = 15.0

# The profile our token is written under. Distinct from any ambient profile so
# merging never clobbers an operator's existing ``~/.databrickscfg`` sections.
HOST_DATABRICKS_PROFILE = "omnigent"


def _credential_url(server: str, host_id: str) -> str:
    return f"{server.rstrip('/')}/v1/hosts/{host_id}/credentials/databricks"


def _fetch(server: str, host_id: str, host_token: str) -> dict | None:
    """Fetch the broker JSON, or ``None`` on any failure."""
    try:
        resp = httpx.get(
            _credential_url(server, host_id),
            headers={MANAGED_HOST_TOKEN_HEADER: host_token},
            timeout=_TIMEOUT_S,
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _write_profile(cfg_path: Path, host: str, token: str) -> bool:
    """Merge an ``[omnigent]`` ``host``/``token`` section into *cfg_path*.

    Preserves any other profiles already in the file. Returns ``True`` on
    success, ``False`` if the file can't be read or written.
    """
    parser = configparser.ConfigParser()
    try:
        if cfg_path.exists():
            parser.read(cfg_path)
    except (OSError, configparser.Error):
        return False
    parser[HOST_DATABRICKS_PROFILE] = {"host": host, "token": token}
    try:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cfg_path, "w", encoding="utf-8") as handle:
            parser.write(handle)
        with contextlib.suppress(OSError):
            os.chmod(cfg_path, 0o600)
    except OSError:
        return False
    return True


def configure_host_databricks(server_url: str, host_id: str) -> bool:
    """Point the sandbox's Databricks SDK at the owner's per-user token.

    Fetches the broker credential and, when the owner has Databricks connected,
    writes an ``[omnigent]`` ``~/.databrickscfg`` profile and exports
    ``DATABRICKS_CONFIG_PROFILE`` so the runner (which inherits it via the env
    allowlist) resolves the gateway/MCP as them.

    :returns: ``True`` when a per-user profile was written; ``False`` otherwise
        (no-op — ambient config, if any, is left untouched).
    """
    token = (os.environ.get(HOST_TOKEN_ENV_VAR) or "").strip()
    if not token:
        return False
    data = _fetch(server_url, host_id, token)
    if not data or not data.get("connected"):
        return False
    workspace_host = str(data.get("workspace_host") or "").rstrip("/")
    db_token = str(data.get("token") or "")
    if not workspace_host or not db_token:
        return False
    cfg_path = Path(os.environ.get("DATABRICKS_CONFIG_FILE") or (Path.home() / ".databrickscfg"))
    if not _write_profile(cfg_path, workspace_host, db_token):
        _logger.info("Databricks credential: could not write %s", cfg_path)
        return False
    # Both the gateway and MCP resolvers read the profile via the SDK, which
    # honors DATABRICKS_CONFIG_PROFILE; the runner inherits it (allowlisted).
    os.environ["DATABRICKS_CONFIG_PROFILE"] = HOST_DATABRICKS_PROFILE
    _logger.info("Databricks credential: profile %r → %s", HOST_DATABRICKS_PROFILE, workspace_host)
    return True
