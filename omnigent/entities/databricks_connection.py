"""Per-user Databricks connection entity.

Plain dataclass returned from
:class:`~omnigent.connections.databricks.DatabricksConnectionStore`. Token
material is carried encrypted in the store row; the token fields here hold the
*decrypted* values and are only ever populated on the server-side vend path,
never serialized to a client.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class DatabricksConnection:
    """A user's connected Databricks workspace.

    :param user_id: The omnigent user id the connection belongs to.
    :param workspace_host: The connected workspace origin (``https://…``); the
        OAuth + AI Gateway MCP endpoints are relative to it.
    :param databricks_user: The connected Databricks user name (email).
    :param databricks_user_id: The connected Databricks SCIM user id.
    :param access_token: Decrypted OAuth access token, or ``None`` on a
        metadata-only view (status endpoints).
    :param refresh_token: Decrypted refresh token, or ``None``.
    :param token_expires_at: Unix epoch seconds the access token expires at, or
        ``None``.
    :param refresh_token_expires_at: Unix epoch seconds the refresh token expires
        at, or ``None``.
    :param scopes: Space-separated granted scopes.
    :param created_at: Unix epoch seconds the connection was first made.
    :param updated_at: Unix epoch seconds of the last refresh / reconnect.
    """

    user_id: str
    workspace_host: str
    databricks_user: str
    databricks_user_id: str
    access_token: str | None
    refresh_token: str | None
    token_expires_at: int | None
    refresh_token_expires_at: int | None
    scopes: str
    created_at: int
    updated_at: int
