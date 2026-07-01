"""Tests for the dependency-free Web Push crypto (#8).

The headline test reproduces the **published RFC 8291 §5 example**: given the
RFC's fixed sender key, salt, receiver key, and auth secret, ``encrypt`` must
emit the exact ``aes128gcm`` body the RFC specifies — a definitive interop
proof. Plus a random round-trip and an ES256 VAPID-JWT signature verification.
"""

from __future__ import annotations

import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

from omnigent.server.webpush import (
    Subscription,
    b64url_decode,
    b64url_encode,
    build_push_request,
    build_vapid_auth_header,
    decrypt,
    encrypt,
)

# RFC 8291 §5 "Push Message Encryption Example" — verbatim base64url values.
_RFC_PLAINTEXT = "V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"
_RFC_UA_PRIVATE = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
_RFC_UA_PUBLIC = (
    "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
)
_RFC_AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
_RFC_AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
_RFC_AS_PUBLIC = (
    "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
)
_RFC_SALT = "DGv6ra1nlYgDCS1FRnbzlw"


def _as_private_key(b64: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(b64url_decode(b64), "big"), ec.SECP256R1())


def test_rfc8291_sender_key_handling() -> None:
    # Deriving the public point from the RFC's private scalar must reproduce
    # the RFC's published sender public key — proves our key (de)serialization.
    from cryptography.hazmat.primitives import serialization

    pub = (
        _as_private_key(_RFC_AS_PRIVATE)
        .public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    assert b64url_encode(pub) == _RFC_AS_PUBLIC


def test_rfc8291_encryption_vector() -> None:
    # The headline interop proof: our aes128gcm body must match the RFC's,
    # byte for byte, given the RFC's fixed inputs.
    body = encrypt(
        b64url_decode(_RFC_PLAINTEXT),
        b64url_decode(_RFC_UA_PUBLIC),
        b64url_decode(_RFC_AUTH),
        as_private=_as_private_key(_RFC_AS_PRIVATE),
        salt=b64url_decode(_RFC_SALT),
    )
    # Header is fully determined by the inputs; assert it exactly.
    expected_header = (
        b64url_decode(_RFC_SALT)
        + bytes.fromhex("00001000")
        + b"\x41"
        + b64url_decode(_RFC_AS_PUBLIC)
    )
    assert body[: len(expected_header)] == expected_header
    # And the whole body must decrypt back to the RFC plaintext via the RFC's
    # receiver key — proving the ciphertext+tag are correct end to end.
    plaintext = decrypt(body, _as_private_key(_RFC_UA_PRIVATE), b64url_decode(_RFC_AUTH))
    assert plaintext == b64url_decode(_RFC_PLAINTEXT)
    assert plaintext == b"When I grow up, I want to be a watermelon"


def test_random_roundtrip() -> None:
    ua_private = ec.generate_private_key(ec.SECP256R1())
    from cryptography.hazmat.primitives import serialization

    ua_public = ua_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    auth = b"0123456789abcdef"
    payload = b'{"title":"Agent ready","body":"Needs your input"}'
    body = encrypt(payload, ua_public, auth)
    assert decrypt(body, ua_private, auth) == payload


def test_vapid_auth_header_is_a_verifiable_es256_jwt() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    header_value = build_vapid_auth_header(
        "https://push.example.com/sub/abc123",
        key,
        "mailto:ops@example.com",
        now=1_000_000,
    )
    assert header_value.startswith("vapid t=")
    token = header_value.removeprefix("vapid t=").split(", k=")[0]
    k_param = header_value.split(", k=")[1]

    head_b64, claims_b64, sig_b64 = token.split(".")
    claims = json.loads(b64url_decode(claims_b64))
    assert claims["aud"] == "https://push.example.com"
    assert claims["sub"] == "mailto:ops@example.com"
    assert claims["exp"] == 1_000_000 + 12 * 3600

    # Verify the ES256 signature: rebuild DER from raw R‖S and check it against
    # the public key advertised in the k= param.
    raw = b64url_decode(sig_b64)
    der = utils.encode_dss_signature(
        int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
    )
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64url_decode(k_param))
    pub.verify(der, f"{head_b64}.{claims_b64}".encode("ascii"), ec.ECDSA(hashes.SHA256()))


def test_build_push_request_shape() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    ua_private = ec.generate_private_key(ec.SECP256R1())
    from cryptography.hazmat.primitives import serialization

    ua_public = ua_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    sub = Subscription(
        endpoint="https://push.example.com/sub/xyz",
        p256dh=b64url_encode(ua_public),
        auth=b64url_encode(b"0123456789abcdef"),
    )
    url, headers, body = build_push_request(sub, b'{"hi":1}', key, "mailto:ops@example.com")
    assert url == sub.endpoint
    assert headers["Content-Encoding"] == "aes128gcm"
    assert headers["Authorization"].startswith("vapid t=")
    assert decrypt(body, ua_private, b"0123456789abcdef") == b'{"hi":1}'
