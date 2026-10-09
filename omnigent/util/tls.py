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


def _exists(path: str) -> bool:
    """Report whether *path* exists (one ``stat``; any error reads as missing)."""
    try:
        Path(path).stat()
    except OSError:
        return False
    return True


def explicit_trust_sources() -> tuple[str | None, str | None]:
    """Return the configured ``(cafile, capath)`` to honor, at most one of them.

    Only the ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` environment variables count as
    configuration; OpenSSL's compiled-in defaults are fallbacks and are never
    mixed into an explicit trust set. An existing file takes precedence over the
    directory, as in httpx's ``trust_env`` handling. A configured path that no
    longer exists (a rotated bundle) is logged and dropped instead of raised.

    :returns: ``(cafile, None)``, ``(None, capath)``, or ``(None, None)``.
    """
    paths = ssl.get_default_verify_paths()
    cafile = os.environ.get(paths.openssl_cafile_env) or None
    capath = os.environ.get(paths.openssl_capath_env) or None
    if cafile is not None and not _exists(cafile):
        logger.warning("%s=%s does not exist; ignoring it", paths.openssl_cafile_env, cafile)
        cafile = None
    if capath is not None and not _exists(capath):
        logger.warning("%s=%s does not exist; ignoring it", paths.openssl_capath_env, capath)
        capath = None
    if cafile is not None:
        capath = None
    return cafile, capath


def _no_trust_context() -> ssl.SSLContext:
    """Build a verifying client context that trusts no roots, so handshakes fail closed."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def client_ssl_context() -> ssl.SSLContext:
    """Return a cached verifying client SSL context.

    Built once so reconnect loops do not re-read the bundle. An existing
    configured source (:func:`explicit_trust_sources`) is honored exactly, so an
    empty or malformed one trusts nothing instead of gaining default roots. Only
    a missing path falls back to the OS bundle or certifi via
    :func:`resolve_ca_file`: a rotated-away bundle is a broken configuration,
    not a narrower trust policy. Verification is never disabled.

    :returns: A shared :class:`ssl.SSLContext`.
    """
    global _client_ssl_context
    if _client_ssl_context is None:
        context = None
        cafile, capath = explicit_trust_sources()
        if cafile is not None or capath is not None:
            try:
                context = ssl.create_default_context(cafile=cafile, capath=capath)
            except FileNotFoundError:
                logger.warning(
                    "Configured CA source (cafile=%s, capath=%s) vanished while loading; "
                    "using default roots",
                    cafile,
                    capath,
                )
            except OSError as exc:  # includes ssl.SSLError for an empty or malformed bundle
                logger.warning(
                    "Configured CA source (cafile=%s, capath=%s) could not be loaded (%s); "
                    "trusting no roots",
                    cafile,
                    capath,
                    exc,
                )
                context = _no_trust_context()
            else:
                if capath is not None and not any(Path(capath).iterdir()):
                    logger.warning(
                        "SSL_CERT_DIR=%s holds no certificates; trusting no roots", capath
                    )
        if context is None:
            context = ssl.create_default_context(cafile=resolve_ca_file())
        _client_ssl_context = context
    return _client_ssl_context
