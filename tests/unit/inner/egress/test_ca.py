"""Tests for omnigent.inner.egress.ca — CA generation and bundle creation."""

from __future__ import annotations

import datetime
from pathlib import Path

from cryptography import x509

from omnigent.inner.egress.ca import ensure_ca, ensure_ca_bundle


def test_ensure_ca_generates_new_ca(tmp_path: Path) -> None:
    """Calling ensure_ca on an empty cache dir creates cert + key files."""
    cert_path, key_path = ensure_ca(cache_dir=tmp_path)

    # Files are created in the specified cache directory
    assert cert_path.exists()
    assert key_path.exists()
    assert cert_path.parent == tmp_path
    assert key_path.parent == tmp_path

    # The cert is a valid X.509 PEM that is a CA
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.value.ca is True

    # Not expired
    now = datetime.datetime.now(datetime.timezone.utc)
    assert now < cert.not_valid_after_utc


def test_ensure_ca_reuses_existing(tmp_path: Path) -> None:
    """Calling ensure_ca twice returns the same paths without regenerating."""
    cert1, key1 = ensure_ca(cache_dir=tmp_path)
    content1 = cert1.read_bytes()

    cert2, key2 = ensure_ca(cache_dir=tmp_path)
    content2 = cert2.read_bytes()

    # Same file, same content — no regeneration
    assert cert1 == cert2
    assert key1 == key2
    assert content1 == content2


def test_ensure_ca_bundle_includes_system_and_custom_ca(tmp_path: Path) -> None:
    """The bundle contains system CAs and our MITM CA appended."""
    cert_path, _key_path = ensure_ca(cache_dir=tmp_path)
    bundle_path = ensure_ca_bundle(cert_path, cache_dir=tmp_path)

    assert bundle_path.exists()
    bundle_data = bundle_path.read_bytes()

    # Our CA cert is present at the end of the bundle
    our_ca_pem = cert_path.read_bytes()
    assert our_ca_pem in bundle_data

    # Bundle contains at least one other certificate (system CAs)
    cert_count = bundle_data.count(b"-----BEGIN CERTIFICATE-----")
    # At minimum: 1 system CA + our CA = 2
    assert cert_count >= 2, (
        f"Expected at least 2 certs in bundle (system + ours), got {cert_count}"
    )


def test_ensure_ca_key_permissions(tmp_path: Path) -> None:
    """The CA private key file has restrictive permissions (0600)."""
    _cert_path, key_path = ensure_ca(cache_dir=tmp_path)
    mode = key_path.stat().st_mode & 0o777
    assert mode == 0o600, f"Expected key perms 0600, got {oct(mode)}"
