"""Per-user Claude subscription connection entity.

Plain dataclass returned from
:class:`~omnigent.connections.claude.ClaudeConnectionStore`. The token is carried
encrypted in the store row; ``oauth_token`` holds the *decrypted* value and is
only ever populated on the server-side vend path, never serialized to a client.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class ClaudeConnection:
    """A user's connected Claude subscription.

    :param user_id: The omnigent user id the connection belongs to.
    :param oauth_token: Decrypted ``claude setup-token`` OAuth token, or ``None``
        on a metadata-only view (status endpoints).
    :param created_at: Unix epoch seconds the subscription was first connected.
    :param updated_at: Unix epoch seconds of the last reconnect.
    """

    user_id: str
    oauth_token: str | None
    created_at: int
    updated_at: int
