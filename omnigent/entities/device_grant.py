"""Device-authorization grant entity (RFC 8628).

Plain dataclass returned from
:class:`omnigent.server.device_grant_store.DeviceGrantStore`. Backs the
generic delegated-login mechanism (Slack is the first consumer, but the
grant is client-agnostic — see ``designs/DEVICE_AUTH.md``). Kept separate
from the ``accounts``-provider entities in ``account.py``: the device grant
is its own auth mechanism, not a user/token row.

Secrets (``device_code``, refresh token) are stored hashed in the DB and
never surfaced on this entity — it carries only the grant's non-secret
state.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class DeviceGrant:
    """A device-authorization grant row (RFC 8628).

    :param id: Opaque grant id; also the ``grant_id`` claim on issued
        access tokens (used for revocation).
    :param user_code: Short code shown on the verification page.
    :param status: ``pending`` / ``approved`` / ``denied`` /
        ``redeemed`` / ``revoked``.
    :param client_id: RFC 8628 client identifier — a public string naming
        the requesting application (e.g. ``"slack"``); display + audit
        only, not an authorization key.
    :param user_id: Omnigent identity that approved it; ``None`` while
        pending.
    :param created_at: Unix epoch seconds when created.
    :param expires_at: Unix epoch seconds when the device_code stops
        being exchangeable.
    :param approved_at: Unix epoch seconds when approved (starts the
        absolute lifetime clock), or ``None`` while pending.
    :param last_polled_at: Unix epoch seconds of the last token poll,
        or ``None`` if never polled.
    """

    id: str
    user_code: str
    status: str
    client_id: str | None
    user_id: str | None
    created_at: int
    expires_at: int
    approved_at: int | None
    last_polled_at: int | None
