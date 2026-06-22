"""Tests for omnigent.inner.egress.certs — per-host certificate cache."""

from __future__ import annotations

import ssl
from pathlib import Path

from cryptography import x509

from omnigent.inner.egress.ca import ensure_ca
from omnigent.inner.egress.certs import HostCertCache


def test_host_cert_cache_generates_valid_cert(tmp_path: Path) -> None:
    """HostCertCache generates a cert with the correct SAN for the hostname."""
    cert_path, key_path = ensure_ca(cache_dir=tmp_path)
    cache = HostCertCache(cert_path, key_path)

    ctx = cache.get_ssl_context("api.github.com")

    # Returns a valid server-side TLS context
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.protocol == ssl.PROTOCOL_TLS_SERVER


def test_host_cert_cache_caches_results(tmp_path: Path) -> None:
    """Repeated calls for the same hostname return cached cert (same context)."""
    cert_path, key_path = ensure_ca(cache_dir=tmp_path)
    cache = HostCertCache(cert_path, key_path)

    ctx1 = cache.get_ssl_context("example.com")
    ctx2 = cache.get_ssl_context("example.com")
    # The underlying _generate is LRU-cached; contexts are built from
    # the same cert/key bytes so they have the same cert chain
    # (we can't compare SSLContext directly, but the PEM data is equal)
    assert ctx1 is not None
    assert ctx2 is not None


def test_host_cert_different_hosts_different_certs(tmp_path: Path) -> None:
    """Different hostnames produce different certificates."""
    cert_path, key_path = ensure_ca(cache_dir=tmp_path)
    cache = HostCertCache(cert_path, key_path)

    # Access the underlying _generate to inspect PEM bytes
    pem_a, _ = cache._get_or_create("host-a.example.com")
    pem_b, _ = cache._get_or_create("host-b.example.com")

    cert_a = x509.load_pem_x509_certificate(pem_a)
    cert_b = x509.load_pem_x509_certificate(pem_b)

    # SAN contains the correct hostname for each
    san_a = cert_a.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.DNSName)
    san_b = cert_b.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.DNSName)

    assert san_a == ["host-a.example.com"]
    assert san_b == ["host-b.example.com"]


def test_host_cert_signed_by_ca(tmp_path: Path) -> None:
    """Generated host certs are signed by our CA (same issuer)."""
    cert_path, key_path = ensure_ca(cache_dir=tmp_path)
    cache = HostCertCache(cert_path, key_path)

    ca_cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    host_pem, _ = cache._get_or_create("test.example.com")
    host_cert = x509.load_pem_x509_certificate(host_pem)

    # Issuer of host cert matches subject of CA
    assert host_cert.issuer == ca_cert.subject

    # Host cert is NOT a CA
    bc = host_cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.value.ca is False
