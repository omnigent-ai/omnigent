"""Unit tests for the tunnel TLS trust-store helpers."""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest

from omnigent.inner.egress.ca import ensure_ca
from omnigent.runner.transports.ws_tunnel.tls import (
    TunnelTLSError,
    is_tls_verification_failure,
    tunnel_ssl_context,
)


def test_no_override_returns_none() -> None:
    """No CA-bundle env → no context, so the library default is kept."""
    assert tunnel_ssl_context("wss://srv/tunnel", {}) is None


def test_plaintext_ws_never_gets_context(tmp_path: Path) -> None:
    """A ws:// URL returns None even when a bundle is configured.

    websockets rejects ``ssl=`` on a plaintext URI, so an ambient bundle
    must not break a local http:// server.
    """
    cert_path, _key = ensure_ca(cache_dir=tmp_path)
    assert (
        tunnel_ssl_context("ws://127.0.0.1:6767/tunnel", {"OMNIGENT_CA_BUNDLE": str(cert_path)})
        is None
    )


def test_bundle_builds_context(tmp_path: Path) -> None:
    """A readable PEM produces a verifying SSL context."""
    cert_path, _key = ensure_ca(cache_dir=tmp_path)
    ctx = tunnel_ssl_context("wss://srv/tunnel", {"OMNIGENT_CA_BUNDLE": str(cert_path)})
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_bundle_is_additive_to_system_store(tmp_path: Path) -> None:
    """The bundle is trusted ON TOP of the system store, not instead of it.

    A user with REQUESTS_CA_BUNDLE pointed at an internal PyPI mirror CA
    (not behind a TLS-inspecting proxy) must still verify the public roots
    that sign a normal wss:// host — the override must not shrink trust to
    the single custom cert.
    """
    cert_path, _key = ensure_ca(cache_dir=tmp_path)
    ctx = tunnel_ssl_context("wss://srv/tunnel", {"REQUESTS_CA_BUNDLE": str(cert_path)})
    assert isinstance(ctx, ssl.SSLContext)
    system_only = ssl.create_default_context()
    # System roots plus exactly the one custom CA — a bundle-only context
    # would hold just the single cert.
    assert ctx.cert_store_stats()["x509"] == system_only.cert_store_stats()["x509"] + 1


def test_env_precedence_prefers_dedicated_var(tmp_path: Path) -> None:
    """OMNIGENT_CA_BUNDLE wins over the conventional trust-store vars."""
    dedicated, _k1 = ensure_ca(cache_dir=tmp_path / "a")
    fallback, _k2 = ensure_ca(cache_dir=tmp_path / "b")
    ctx = tunnel_ssl_context(
        "wss://srv/tunnel",
        {"OMNIGENT_CA_BUNDLE": str(dedicated), "SSL_CERT_FILE": str(fallback)},
    )
    assert isinstance(ctx, ssl.SSLContext)


def test_ssl_cert_file_used_as_fallback(tmp_path: Path) -> None:
    """SSL_CERT_FILE is honoured when the dedicated var is unset."""
    cert_path, _key = ensure_ca(cache_dir=tmp_path)
    ctx = tunnel_ssl_context("wss://srv/tunnel", {"SSL_CERT_FILE": str(cert_path)})
    assert isinstance(ctx, ssl.SSLContext)


def test_missing_bundle_raises_naming_path(tmp_path: Path) -> None:
    """A nonexistent bundle path fails loud rather than falling back."""
    missing = tmp_path / "nope.pem"
    with pytest.raises(TunnelTLSError) as excinfo:
        tunnel_ssl_context("wss://srv/tunnel", {"OMNIGENT_CA_BUNDLE": str(missing)})
    assert str(missing) in str(excinfo.value)
    assert "OMNIGENT_CA_BUNDLE" in str(excinfo.value)


def test_non_pem_bundle_raises(tmp_path: Path) -> None:
    """A file that isn't a PEM fails loud with a readable message."""
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate")
    with pytest.raises(TunnelTLSError):
        tunnel_ssl_context("wss://srv/tunnel", {"OMNIGENT_CA_BUNDLE": str(junk)})


def test_verification_failure_is_fatal() -> None:
    """A certificate-verification error is classified permanent."""
    assert is_tls_verification_failure(ssl.SSLCertVerificationError("verify failed"))


def test_other_ssl_errors_are_transient() -> None:
    """A non-verification TLS error stays retryable."""
    assert not is_tls_verification_failure(ssl.SSLEOFError("eof"))
    assert not is_tls_verification_failure(ssl.SSLError("generic"))
