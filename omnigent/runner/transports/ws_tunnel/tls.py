"""TLS trust-store resolution shared by the host and runner WebSocket tunnels.

A corporate TLS-inspecting proxy (Zscaler/Netskope) or a private CA
re-signs the ``wss://`` handshake with a certificate the system trust
store doesn't know, so the tunnel fails with ``CERTIFICATE_VERIFY_FAILED``
and no way to supply the organisation's bundle. These helpers resolve a
bundle from the environment and build the ``ssl`` context both tunnels
hand to ``websockets``.
"""

from __future__ import annotations

import logging
import ssl
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

_logger = logging.getLogger(__name__)

CA_BUNDLE_ENV_VAR = "OMNIGENT_CA_BUNDLE"

# Checked in order, first one set wins. OMNIGENT_CA_BUNDLE is the dedicated
# knob; the rest are the conventional trust-store vars the surrounding
# toolchain (requests, curl) already honours, so an operator who configured
# those for a corporate proxy gets a working tunnel for free.
_CA_BUNDLE_ENV_VARS: tuple[str, ...] = (
    CA_BUNDLE_ENV_VAR,
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
)


class TunnelTLSError(Exception):
    """A configured CA bundle is unusable (missing path, or not a PEM).

    The message is the full user-facing explanation including the fix.
    """


def ca_bundle_fix_hint() -> str:
    """Build the remedy for a TLS trust failure on the tunnel.

    :returns: A sentence naming the env var and an example invocation.
    """
    return (
        f"Export your organisation's CA bundle and point {CA_BUNDLE_ENV_VAR} "
        "(or SSL_CERT_FILE) at the PEM file, e.g. "
        f"`{CA_BUNDLE_ENV_VAR}=/path/to/corp-ca.pem omnigent host --server <url>`."
    )


def _resolve_ca_bundle(env: Mapping[str, str]) -> tuple[str, Path] | None:
    """Find the first CA bundle override set in *env*.

    :param env: Environment mapping to read, e.g. ``os.environ``.
    :returns: The ``(var_name, path)`` that won, or ``None`` when no
        override is configured.
    """
    for name in _CA_BUNDLE_ENV_VARS:
        value = env.get(name, "").strip()
        if value:
            return name, Path(value)
    return None


def tunnel_ssl_context(
    tunnel_url: str,
    env: Mapping[str, str],
) -> ssl.SSLContext | None:
    """Build the ``ssl`` context for a tunnel handshake, if one is needed.

    Returns ``None`` unless a CA bundle override is configured, so the
    default install keeps ``websockets``' own ``ssl=True`` handling and
    the system trust store. ``ws://`` never gets a context — ``websockets``
    rejects ``ssl=`` on a plaintext URI.

    :param tunnel_url: The tunnel URL, e.g.
        ``"wss://acme.databricks.com/v1/hosts/host_abc/tunnel"``.
    :param env: Environment mapping to read the override from, e.g.
        ``os.environ``.
    :returns: A context trusting the configured bundle, or ``None`` to
        leave the library default alone.
    :raises TunnelTLSError: If the configured bundle is missing or
        unreadable — a typo'd path must fail loud, not silently fall back
        to the system store.
    """
    if urlparse(tunnel_url).scheme != "wss":
        return None
    resolved = _resolve_ca_bundle(env)
    if resolved is None:
        return None
    var_name, path = resolved
    if not path.is_file():
        raise TunnelTLSError(
            f"{var_name}={path} does not point at a readable file, so the TLS "
            f"trust store for {tunnel_url} could not be built. "
            f"{ca_bundle_fix_hint()}"
        )
    try:
        # Add the bundle ON TOP of the system trust store, not instead of it.
        # A bundle set for one purpose (an internal PyPI mirror via
        # REQUESTS_CA_BUNDLE) must not stop the tunnel trusting the public
        # roots that sign a normal wss:// host.
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=str(path))
    except (ssl.SSLError, OSError) as exc:
        raise TunnelTLSError(
            f"{var_name}={path} could not be loaded as a CA bundle: {exc}. "
            f"It must be a PEM file containing one or more certificates. "
            f"{ca_bundle_fix_hint()}"
        ) from exc
    _logger.info("Using CA bundle from %s=%s for %s", var_name, path, tunnel_url)
    return context


def is_tls_verification_failure(exc: BaseException) -> bool:
    """Whether *exc* is a permanent TLS trust failure.

    Only certificate verification is permanent: reconnecting can never
    teach the process to trust a certificate. Other ``ssl.SSLError``
    subtypes (a mid-handshake ``SSLEOFError`` from a bounced ingress, say)
    are transient and belong on the retry path.

    :param exc: The exception raised by the handshake.
    :returns: ``True`` for a certificate-verification failure.
    """
    return isinstance(exc, ssl.SSLCertVerificationError)
