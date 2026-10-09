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
import os
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


def explicit_trust_sources() -> tuple[str | None, str | None]:
    """Return the operator-configured ``(cafile, capath)`` that exist on disk.

    Only the ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` environment variables count as
    configuration; OpenSSL's compiled-in defaults are fallbacks and are never
    mixed into an explicitly restricted trust set. A configured path that is
    missing or empty (a rotated bundle) is logged and dropped instead of raised.

    :returns: ``(cafile, capath)``, each ``None`` when unset or unusable.
    """
    paths = ssl.get_default_verify_paths()
    cafile = os.environ.get(paths.openssl_cafile_env) or None
    capath = os.environ.get(paths.openssl_capath_env) or None
    if cafile is not None and not (Path(cafile).is_file() and Path(cafile).stat().st_size > 0):
        logger.warning(
            "%s=%s is not a readable CA bundle; ignoring it", paths.openssl_cafile_env, cafile
        )
        cafile = None
    if capath is not None and not Path(capath).is_dir():
        logger.warning("%s=%s is not a directory; ignoring it", paths.openssl_capath_env, capath)
        capath = None
    return cafile, capath


def resolve_ca_dir() -> str | None:
    """Return the configured ``SSL_CERT_DIR`` when it is an existing directory.

    :returns: The capath directory, or ``None`` when unset or missing.
    """
    return explicit_trust_sources()[1]


def client_ssl_context() -> ssl.SSLContext:
    """Return a cached verifying client SSL context.

    Trusts exactly the configured sources when any is usable (``SSL_CERT_FILE``
    and/or ``SSL_CERT_DIR``), otherwise the OS bundle or certifi via
    :func:`resolve_ca_file`. Built once so reconnect loops do not re-read the
    bundle; keeps :func:`ssl.create_default_context`'s hostname checking and
    ``CERT_REQUIRED``.

    :returns: A shared :class:`ssl.SSLContext`.
    """
    global _client_ssl_context
    if _client_ssl_context is None:
        cafile, capath = explicit_trust_sources()
        if cafile is None and capath is None:
            cafile = resolve_ca_file()
        _client_ssl_context = ssl.create_default_context(cafile=cafile, capath=capath)
    return _client_ssl_context
