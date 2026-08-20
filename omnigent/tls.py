"""TLS trust for omnigent's own outbound client connections.

omnigent's uv / python-build-standalone interpreter ships OpenSSL with no
default certificate path (``ssl.get_default_verify_paths()`` returns
``cafile=None`` / ``capath=None``), so a bare ``ssl.create_default_context()``
loads zero roots and every ``wss://`` verification fails with
``CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain``.

This module resolves a usable CA bundle — the OS trust store first, so CAs added
by corporate MDM / IT policy and an operator-supplied ``SSL_CERT_FILE`` are
honored, falling back to the bundled certifi (Mozilla) roots — and builds a
verifying client SSL context from it. Use :func:`client_ssl_context` for omnigent's
own outbound websocket/HTTP clients.

This is distinct from :mod:`omnigent.inner.egress.ca`, which *manufactures* a
self-signed CA for the sandbox MITM proxy; here we only consume existing trust
for our own client connections. Both share the CA-file resolution below.
"""

from __future__ import annotations

import logging
import ssl
from pathlib import Path

logger = logging.getLogger(__name__)

_client_ssl_context: ssl.SSLContext | None = None


def resolve_ca_file() -> str:
    """Return a path to a non-empty CA bundle for client verification.

    Prefers the OS trust store (``ssl.get_default_verify_paths``) so CAs added by
    corporate MDM, IT policy, or an operator-set ``SSL_CERT_FILE`` are included;
    falls back to the bundled certifi (Mozilla) roots when the OS path is missing
    or empty (the uv / python-build-standalone case).

    :returns: Absolute path to a CA bundle file containing at least one cert.
    """
    paths = ssl.get_default_verify_paths()
    for candidate in (paths.cafile, paths.openssl_cafile):
        if candidate:
            p = Path(candidate)
            if p.is_file() and p.stat().st_size > 0:
                logger.debug("Using system CA bundle: %s", p)
                return str(p)

    import certifi

    logger.debug("System CA bundle not found, falling back to certifi")
    return certifi.where()


def client_ssl_context() -> ssl.SSLContext:
    """Return a cached verifying client SSL context.

    Built once (lazily) so a reconnect loop doesn't re-read the bundle on every
    attempt. Keeps the secure defaults of :func:`ssl.create_default_context`
    (hostname checking enabled, ``verify_mode == CERT_REQUIRED``).

    :returns: A shared :class:`ssl.SSLContext` trusting the resolved CA bundle.
    """
    global _client_ssl_context
    if _client_ssl_context is None:
        _client_ssl_context = ssl.create_default_context(cafile=resolve_ca_file())
    return _client_ssl_context
