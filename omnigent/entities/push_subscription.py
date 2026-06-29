"""Push subscription entity — a browser's Web Push registration (#8).

When a user enables notifications, the browser's ``PushManager.subscribe``
yields an endpoint URL plus the ``p256dh`` / ``auth`` keys the server needs to
encrypt payloads for that client (RFC 8291). We persist one row per
(user, endpoint) so the server can deliver pushes even when the app is
backgrounded or closed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PushSubscription:
    """A browser Web Push subscription owned by a user.

    :param id: Opaque primary key, e.g. ``"push_a1b2c3..."``.
    :param user_id: The owning user's identity.
    :param endpoint: The push-service URL to POST encrypted payloads to
        (unique — re-subscribing the same browser upserts this row).
    :param p256dh: The client public key (base64url, uncompressed P-256 point).
    :param auth: The client auth secret (base64url, 16 bytes).
    :param created_at: Unix epoch seconds at row creation.
    """

    id: str
    user_id: str
    endpoint: str
    p256dh: str
    auth: str
    created_at: int
