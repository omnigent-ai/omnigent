"""VAPID key management for Web Push (#8).

The server needs a *stable* VAPID keypair: a browser subscribes with the
server's VAPID public key (``applicationServerKey``), so if the key changed
across restarts every existing subscription would break. We persist the
private key as PEM next to the server's data and reuse it; the public key
(uncompressed P-256 point, base64url) is what the frontend subscribes with and
what the push service verifies the JWT against.
"""

from __future__ import annotations

import logging
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from omnigent.server.webpush import b64url_encode

_logger = logging.getLogger(__name__)


def load_or_create_vapid_key(pem_path: Path) -> ec.EllipticCurvePrivateKey:
    """Load the server's VAPID private key, generating + persisting if absent.

    :param pem_path: Where the PEM lives (e.g. ``<data>/vapid_private_key.pem``).
    :returns: The stable P-256 VAPID private key.
    """
    if pem_path.exists():
        key = serialization.load_pem_private_key(pem_path.read_bytes(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError(f"{pem_path} is not an EC private key")
        return key

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pem_path.parent.mkdir(parents=True, exist_ok=True)
    pem_path.write_bytes(pem)
    try:
        pem_path.chmod(0o600)
    except OSError:  # best-effort on platforms without POSIX modes
        _logger.debug("could not chmod VAPID key %s", pem_path, exc_info=True)
    _logger.info("generated a new VAPID key at %s", pem_path)
    return key


def vapid_application_server_key(key: ec.EllipticCurvePrivateKey) -> str:
    """Return the public ``applicationServerKey`` (base64url uncompressed point).

    This is the value the frontend passes to ``PushManager.subscribe`` and the
    server advertises at ``GET /v1/push/vapid-public-key``.

    :param key: The server's VAPID private key.
    :returns: The base64url-encoded uncompressed P-256 public point.
    """
    pub = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return b64url_encode(pub)
