"""Give a managed sandbox the owner's connected Claude subscription.

Called by ``omnigent host`` at startup, next to the GitHub and Databricks setup.
When the session owner has connected a Claude subscription (a token from
``claude setup-token``), the host fetches it from the generic credential broker
(:mod:`omnigent.server.routes.host_credentials`, ``provider=claude``) and exports
it as ``CLAUDE_CODE_OAUTH_TOKEN``. The host forwards that variable to its runners
(the harness credential allowlist in :mod:`omnigent.host.connect`), so Claude Code
harnesses authenticate as the owner without any per-sandbox sign-in.

The token lives only in the host's process environment and its children; it is
never written to the sandbox's disk. Best-effort: a no-op outside a managed
sandbox, without a launch token, when the broker is unreachable, or when the
owner hasn't connected Claude.
"""

from __future__ import annotations

import logging
import os

from omnigent.host.credential_broker import fetch_host_credential
from omnigent.host.identity import HOST_TOKEN_ENV_VAR
from omnigent.host.identity_env import in_managed_sandbox

_logger = logging.getLogger(__name__)

CLAUDE_OAUTH_TOKEN_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"


def configure_host_claude(server_url: str, host_id: str) -> bool:
    """Export the owner's connected Claude subscription token for this host's runners.

    A connected subscription replaces any ``CLAUDE_CODE_OAUTH_TOKEN`` the sandbox
    image or launcher provided, since it is the owner's explicit choice.

    :param server_url: The server the host registers with.
    :param host_id: This host's id, which the launch token is bound to.
    :returns: ``True`` when the owner's token was exported; ``False`` otherwise.
    """
    if not in_managed_sandbox():
        return False
    host_token = (os.environ.get(HOST_TOKEN_ENV_VAR) or "").strip()
    if not host_token:
        return False
    payload = fetch_host_credential(server_url, host_id, host_token, "claude")
    if not payload or not payload.get("connected"):
        return False
    token = str(payload.get("token") or "").strip()
    if not token:
        return False
    os.environ[CLAUDE_OAUTH_TOKEN_ENV_VAR] = token
    _logger.info("Claude credential: using the owner's connected subscription")
    return True
