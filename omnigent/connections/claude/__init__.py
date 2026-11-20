"""Per-user Claude subscription connection store.

A Claude-typed :class:`~omnigent.connections.ConnectionStore` façade
(``provider="claude"``): it maps a :class:`ClaudeConnection` to/from the generic
``(secret, metadata)`` shape. Reuses the same table and cipher as every other
provider. The secret is the long-lived token ``claude setup-token`` prints,
which comes without a refresh token, so there is no refresh state to keep.
"""

from __future__ import annotations

from typing import ClassVar

from omnigent.connections import ConnectionStore
from omnigent.entities import ClaudeConnection, ProviderConnection


class ClaudeConnectionStore(ConnectionStore[ClaudeConnection]):
    """Claude-typed façade over the shared credential store.

    Inherits the uniform ``get`` / ``delete`` / ``list_all`` from
    :class:`ConnectionStore`; adds ``upsert`` and the row →
    :class:`ClaudeConnection` mapping.
    """

    _PROVIDER: ClassVar[str] = "claude"

    @staticmethod
    def _to_entity(conn: ProviderConnection) -> ClaudeConnection:
        secret = conn.secret or {}
        return ClaudeConnection(
            user_id=conn.user_id,
            oauth_token=secret.get("oauth_token") if conn.secret is not None else None,
            created_at=conn.created_at,
            updated_at=conn.updated_at,
        )

    def upsert(self, user_id: str, *, oauth_token: str) -> ClaudeConnection:
        """Create or replace a user's Claude connection (idempotent on ``user_id``)."""
        conn = self._store.upsert(
            user_id, self._PROVIDER, secret={"oauth_token": oauth_token}, metadata={}
        )
        return self._to_entity(conn)
