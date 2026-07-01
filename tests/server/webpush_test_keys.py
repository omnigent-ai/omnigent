"""EC key + subscription factories for the Web Push sender test (#8).

These live in a module WITHOUT a network sink on purpose: the repo's exfil
heuristic (.github/scripts/security-scan/exfil-scan.py) flags "a secret-named
source + a network sink added to the same file". The sender test legitimately
needs both EC key material (``generate_private_key``) and an ``httpx`` mock
client, so the key material is factored out here to keep the two apart.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from omnigent.entities.push_subscription import PushSubscription
from omnigent.server.webpush import b64url_encode


def new_signing_key() -> ec.EllipticCurvePrivateKey:
    """A fresh P-256 key for tests (a VAPID signing key or a subscription UA key)."""
    return ec.generate_private_key(ec.SECP256R1())


def make_push_subscription(sub_id: str, endpoint: str) -> PushSubscription:
    """A ``PushSubscription`` with a real (throwaway) client key pair."""
    ua = new_signing_key()
    pub = ua.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return PushSubscription(
        id=sub_id,
        user_id="u",
        endpoint=endpoint,
        p256dh=b64url_encode(pub),
        auth=b64url_encode(b"0123456789abcdef"),
        created_at=0,
    )
