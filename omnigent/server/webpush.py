"""Web Push (#8) — VAPID auth + RFC 8291 ``aes128gcm`` payload encryption.

Implemented directly on ``cryptography`` (already a dependency) rather than
pulling in ``pywebpush``/``py_vapid``, to avoid a dependency-graph change we
can't validate across install configs.

Two pieces, both pure/testable:

- :func:`encrypt` — RFC 8291 §3.4 + RFC 8188 ``aes128gcm`` content encoding:
  ECDH(sender, ua) → auth-mixed IKM → per-message ``salt`` → CEK/nonce → a
  single AES-128-GCM record. :func:`decrypt` is the inverse (used by tests and
  the mock receiver) so a round-trip exercises both directions of the spec.
- :func:`build_vapid_auth_header` — RFC 8292 VAPID: an ES256 JWT
  (``aud``=push-origin, ``exp``, ``sub``) plus the server's public key, returned
  as the ``Authorization: vapid t=<jwt>, k=<key>`` header value.

:func:`build_push_request` assembles the ``(url, headers, body)`` a push
service expects; the actual POST lives in the sender service.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand

_CURVE = ec.SECP256R1()
# RFC 8188 record size header. 4096 matches the published RFC 8291 example and
# is comfortably larger than any notification payload we send.
_RECORD_SIZE = 4096


def b64url_decode(value: str) -> bytes:
    """Decode unpadded (or padded) base64url to bytes."""
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def b64url_encode(data: bytes) -> str:
    """Encode bytes to unpadded base64url."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _uncompressed(public_key: ec.EllipticCurvePublicKey) -> bytes:
    """Serialize a P-256 public key to its 65-byte uncompressed point."""
    return public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    """HKDF-Extract (RFC 5869): PRK = HMAC-SHA256(salt, ikm)."""
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    """HKDF-Expand (RFC 5869) over SHA-256."""
    return HKDFExpand(algorithm=hashes.SHA256(), length=length, info=info).derive(prk)


def _derive_keys(
    ua_public: bytes,
    auth_secret: bytes,
    as_public: bytes,
    ecdh_secret: bytes,
    salt: bytes,
) -> tuple[bytes, bytes]:
    """Derive ``(content_encryption_key, nonce)`` per RFC 8291 §3.4 → RFC 8188.

    :param ua_public: The subscription ``p256dh`` (uncompressed point).
    :param auth_secret: The subscription's 16-byte ``auth`` secret.
    :param as_public: The application-server (sender) public key (uncompressed).
    :param ecdh_secret: The ECDH shared secret between sender and ua keys.
    :param salt: The 16-byte per-message salt.
    :returns: ``(cek, nonce)``.
    """
    # RFC 8291 §3.4: mix the auth secret into the ECDH output to get the IKM
    # that RFC 8188 then uses with the per-message salt.
    key_info = b"WebPush: info\x00" + ua_public + as_public
    ikm = _hkdf_expand(_hkdf_extract(auth_secret, ecdh_secret), key_info, 32)

    prk = _hkdf_extract(salt, ikm)
    cek = _hkdf_expand(prk, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf_expand(prk, b"Content-Encoding: nonce\x00", 12)
    return cek, nonce


def encrypt(
    plaintext: bytes,
    ua_public: bytes,
    auth_secret: bytes,
    *,
    as_private: ec.EllipticCurvePrivateKey | None = None,
    salt: bytes | None = None,
) -> bytes:
    """Encrypt ``plaintext`` for a subscription per RFC 8291 (``aes128gcm``).

    :param plaintext: The (already-serialized) push payload.
    :param ua_public: The subscription ``p256dh`` (65-byte uncompressed point).
    :param auth_secret: The subscription ``auth`` secret (16 bytes).
    :param as_private: Ephemeral sender private key; generated when ``None``
        (production). Tests pass the RFC's fixed key to match its vector.
    :param salt: 16-byte per-message salt; random when ``None``.
    :returns: The full ``aes128gcm`` message body (header ‖ single record).
    """
    if as_private is None:
        as_private = ec.generate_private_key(_CURVE)
    if salt is None:
        salt = os.urandom(16)

    as_public = _uncompressed(as_private.public_key())
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(_CURVE, ua_public)
    ecdh_secret = as_private.exchange(ec.ECDH(), ua_key)

    cek, nonce = _derive_keys(ua_public, auth_secret, as_public, ecdh_secret, salt)
    # Single record: plaintext ‖ 0x02 (last-record delimiter), no extra padding.
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + struct.pack(">I", _RECORD_SIZE) + struct.pack(">B", len(as_public)) + as_public
    return header + ciphertext


def decrypt(body: bytes, ua_private: ec.EllipticCurvePrivateKey, auth_secret: bytes) -> bytes:
    """Inverse of :func:`encrypt` — recover the plaintext (receiver side).

    Implemented independently of :func:`encrypt` so a round-trip exercises both
    directions of RFC 8291/8188 (and so a mock push receiver can verify a real
    send). Used by tests and the verification harness.

    :param body: The ``aes128gcm`` message body.
    :param ua_private: The subscription's private key (receiver).
    :param auth_secret: The subscription's ``auth`` secret.
    :returns: The decrypted plaintext (delimiter/padding stripped).
    """
    salt = body[:16]
    idlen = body[20]
    as_public = body[21 : 21 + idlen]
    ciphertext = body[21 + idlen :]

    ua_public = _uncompressed(ua_private.public_key())
    as_key = ec.EllipticCurvePublicKey.from_encoded_point(_CURVE, as_public)
    ecdh_secret = ua_private.exchange(ec.ECDH(), as_key)

    cek, nonce = _derive_keys(ua_public, auth_secret, as_public, ecdh_secret, salt)
    record = AESGCM(cek).decrypt(nonce, ciphertext, None)
    # Strip the trailing delimiter (0x01/0x02) and any zero padding.
    return record.rstrip(b"\x00")[:-1]


@dataclass(frozen=True)
class Subscription:
    """A browser ``PushSubscription`` as the push service needs it.

    :param endpoint: The push-service URL to POST the encrypted body to.
    :param p256dh: The client public key (base64url, uncompressed point).
    :param auth: The client auth secret (base64url, 16 bytes).
    """

    endpoint: str
    p256dh: str
    auth: str


def build_vapid_auth_header(
    endpoint: str,
    vapid_private_key: ec.EllipticCurvePrivateKey,
    subscriber: str,
    *,
    now: int | None = None,
    ttl_seconds: int = 12 * 3600,
) -> str:
    """Build the RFC 8292 ``Authorization: vapid t=<jwt>, k=<key>`` header.

    The JWT is ES256-signed with ``aud`` = the push endpoint's origin, an
    expiry, and ``sub`` = the operator contact (a ``mailto:``/URL). ``k`` is the
    server's VAPID public key (uncompressed point, base64url) — the push
    service uses it to verify the JWT.

    :param endpoint: The subscription endpoint (its origin becomes ``aud``).
    :param vapid_private_key: The server's stable VAPID signing key (P-256).
    :param subscriber: Operator contact, e.g. ``"mailto:ops@example.com"``.
    :param now: Override the clock (tests); defaults to wall-clock seconds.
    :param ttl_seconds: JWT lifetime; RFC caps this at 24h, default 12h.
    :returns: The full ``Authorization`` header value.
    """
    issued = int(time.time()) if now is None else now
    parsed = urlparse(endpoint)
    audience = f"{parsed.scheme}://{parsed.netloc}"

    header = b64url_encode(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = b64url_encode(
        json.dumps({"aud": audience, "exp": issued + ttl_seconds, "sub": subscriber}).encode()
    )
    signing_input = f"{header}.{claims}".encode("ascii")

    der = vapid_private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    # JWS ES256 wants raw R‖S (32 bytes each), not the DER cryptography emits.
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    jwt = f"{header}.{claims}.{b64url_encode(raw_sig)}"

    pub = _uncompressed(vapid_private_key.public_key())
    return f"vapid t={jwt}, k={b64url_encode(pub)}"


def build_push_request(
    subscription: Subscription,
    payload: bytes,
    vapid_private_key: ec.EllipticCurvePrivateKey,
    subscriber: str,
    *,
    ttl: int = 2419200,
) -> tuple[str, dict[str, str], bytes]:
    """Assemble the ``(url, headers, body)`` for a Web Push delivery.

    :param subscription: The target browser subscription.
    :param payload: The notification payload (e.g. JSON bytes).
    :param vapid_private_key: The server's VAPID signing key.
    :param subscriber: Operator contact for the VAPID ``sub`` claim.
    :param ttl: Push ``TTL`` in seconds (how long the service may queue it).
    :returns: ``(endpoint_url, headers, encrypted_body)`` ready to POST.
    """
    body = encrypt(payload, b64url_decode(subscription.p256dh), b64url_decode(subscription.auth))
    headers = {
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(ttl),
        "Authorization": build_vapid_auth_header(
            subscription.endpoint, vapid_private_key, subscriber
        ),
    }
    return subscription.endpoint, headers, body
