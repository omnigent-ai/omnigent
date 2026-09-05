"""Per-user integration credential entity (provider-agnostic).

Plain dataclass returned from
:class:`omnigent.stores.credential_store.CredentialStore`. The
``secret`` mapping carries the *decrypted* secret material and is only ever
populated on server-side vend paths, never serialized to a client;
``metadata`` holds non-secret provider fields. See
``designs/CREDENTIAL_STORE.md``.
"""

from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass(frozen=True)
class ProviderConnection:
    """A user's connected third-party integration.

    :param user_id: The omnigent user id the connection belongs to.
    :param provider: Provider key, e.g. ``"github"``.
    :param account_id: Provider account discriminator (``""`` = single account).
    :param secret: Decrypted secret material as a mapping, or ``None`` when the
        store returned a metadata-only view (status endpoints).
    :param metadata: Non-secret provider metadata (login, ids, scopes, expiries).
    :param created_at: Unix epoch seconds the connection was first made.
    :param updated_at: Unix epoch seconds of the last refresh / reconnect.
    """

    user_id: str
    provider: str
    account_id: str
    # repr=False keeps decrypted tokens out of the auto-generated repr; the
    # explicit __repr__ below reduces the secret to a presence marker so a log
    # line, exception frame, or APM capture never emits token material.
    secret: dict[str, Any] | None = dataclasses.field(repr=False)
    metadata: dict[str, Any]
    created_at: int
    updated_at: int

    def __repr__(self) -> str:
        # ``is not None`` (not truthiness) so a loaded-but-empty secret still
        # shows as present, distinct from the metadata-only ``None`` view.
        secret = "<redacted>" if self.secret is not None else None
        return (
            f"ProviderConnection(user_id={self.user_id!r}, provider={self.provider!r}, "
            f"account_id={self.account_id!r}, secret={secret}, metadata={self.metadata!r}, "
            f"created_at={self.created_at}, updated_at={self.updated_at})"
        )
