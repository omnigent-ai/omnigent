"""Claude subscription connection: configuration, token validation, broker resolver.

Lets each user connect their own Claude subscription once, by pasting the token
``claude setup-token`` prints. The server stores it in the per-user credential
store, and a managed sandbox launched by that user fetches it from the generic
credential broker (:mod:`omnigent.server.routes.host_credentials`) and exports it
as ``CLAUDE_CODE_OAUTH_TOKEN`` (see :mod:`omnigent.host.claude_credential`).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from omnigent.connections.claude import ClaudeConnectionStore
from omnigent.process_logging import env_truthy

_logger = logging.getLogger(__name__)

CLAUDE_CONNECT_ENV_VAR = "OMNIGENT_CLAUDE_CONNECT_ENABLED"

# Prefix of the OAuth tokens ``claude setup-token`` prints. Anthropic API keys
# (``sk-ant-api…``) use a different prefix and a different environment variable.
_SETUP_TOKEN_PREFIX = "sk-ant-oat"
_MAX_TOKEN_CHARS = 1024


@dataclass(frozen=True)
class ClaudeConnectConfig:
    """Enables per-user Claude subscription connections on this server.

    Carries no settings today: the token comes from the user and needs no
    server-side OAuth client.
    """

    @staticmethod
    def from_env() -> ClaudeConnectConfig | None:
        """Return the config when ``OMNIGENT_CLAUDE_CONNECT_ENABLED`` is truthy, else ``None``."""
        if not env_truthy(os.environ.get(CLAUDE_CONNECT_ENV_VAR)):
            return None
        return ClaudeConnectConfig()


def claude_connect_from_env(
    database_uri: str,
) -> tuple[ClaudeConnectConfig | None, ClaudeConnectionStore | None]:
    """Build the server's Claude connect config and store from the environment.

    Shared by ``omnigent server`` and the container entrypoint. Returns
    ``(None, None)`` when the feature is off, and ``(config, None)`` with an error
    logged when it is on but the credential store has no cipher configured.
    """
    config = ClaudeConnectConfig.from_env()
    if config is None:
        return None, None
    from omnigent.stores.credential_store import build_secret_cipher

    cipher = build_secret_cipher()
    if cipher is None:
        _logger.error(
            "%s is set but Claude subscription connect is disabled: configure the "
            "credential store's KMS or Vault key to enable it.",
            CLAUDE_CONNECT_ENV_VAR,
        )
        return config, None
    return config, ClaudeConnectionStore(database_uri, cipher)


class InvalidSetupTokenError(ValueError):
    """The pasted value is not a ``claude setup-token`` OAuth token."""


def normalize_setup_token(raw: str) -> str:
    """Return the trimmed token, or raise :class:`InvalidSetupTokenError`.

    Accepts only the OAuth token shape so an Anthropic API key, which Claude
    Code reads from a different variable, is rejected with a clear message.
    """
    token = raw.strip()
    if not token.startswith(_SETUP_TOKEN_PREFIX):
        raise InvalidSetupTokenError(
            "Paste the token printed by `claude setup-token` (it starts with sk-ant-oat)."
        )
    if len(token) > _MAX_TOKEN_CHARS or any(character.isspace() for character in token):
        raise InvalidSetupTokenError(
            "The token is malformed; copy it again from `claude setup-token`."
        )
    return token


async def resolve_claude_credential(
    user_id: str,
    *,
    store: ClaudeConnectionStore,
    client: Any = None,
) -> dict[str, object] | None:
    """Resolve the Claude broker payload for *user_id*, or ``None`` when not connected.

    The provider adapter the generic credential broker calls. *client* is unused:
    a setup token needs no provider API call to vend.
    """
    del client
    connection = await asyncio.to_thread(store.get, user_id, with_tokens=True)
    if connection is None or not connection.oauth_token:
        return None
    return {"token": connection.oauth_token}
