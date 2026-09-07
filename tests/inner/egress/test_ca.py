"""Tests for omnigent.inner.egress.ca — CA generation and bundle creation."""

from __future__ import annotations

import datetime
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from omnigent.inner.egress import ca as ca_module
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


def _make_ca_pem(common_name: str) -> bytes:
    """Return the PEM bytes of a throwaway self-signed CA cert."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def test_system_ca_bundle_includes_capath_loose_certs(tmp_path: Path, monkeypatch) -> None:
    """A CA present only as a loose file under ``capath`` is included.

    Corporate MDM / IT roots are often installed as loose files in the
    OpenSSL ``capath`` directory rather than merged into the single
    ``cafile`` bundle. Reading only ``cafile`` misses them, which breaks
    upstream TLS verification for anything signed by such a root. The
    bundle must pick up the loose ``capath`` certs too.
    """
    cafile = tmp_path / "cafile.pem"
    capath = tmp_path / "capath"
    capath.mkdir()
    cafile_ca = _make_ca_pem("CAfile Root")
    capath_ca = _make_ca_pem("Capath-only Root")
    cafile.write_bytes(cafile_ca)
    (capath / "corp-root.pem").write_bytes(capath_ca)

    fake_paths = ssl.DefaultVerifyPaths(
        cafile=str(cafile),
        capath=str(capath),
        openssl_cafile_env="",
        openssl_cafile=str(cafile),
        openssl_capath_env="",
        openssl_capath=str(capath),
    )
    monkeypatch.setattr(ca_module.ssl, "get_default_verify_paths", lambda: fake_paths)

    bundle = ca_module._system_ca_bundle()

    assert capath_ca in bundle, "loose capath CA missing from the bundle"
    assert cafile_ca in bundle, "cafile CA missing (regression)"


def test_system_ca_bundle_skips_non_pem_in_capath(tmp_path: Path, monkeypatch) -> None:
    """Non-PEM files in ``capath`` are skipped without error."""
    capath = tmp_path / "capath"
    capath.mkdir()
    real_ca = _make_ca_pem("Real Root")
    (capath / "root.pem").write_bytes(real_ca)
    (capath / "README").write_bytes(b"not a certificate\n")

    fake_paths = ssl.DefaultVerifyPaths(
        cafile=None,
        capath=str(capath),
        openssl_cafile_env="",
        openssl_cafile=None,
        openssl_capath_env="",
        openssl_capath=str(capath),
    )
    monkeypatch.setattr(ca_module.ssl, "get_default_verify_paths", lambda: fake_paths)

    bundle = ca_module._system_ca_bundle()

    assert real_ca in bundle
    assert b"not a certificate" not in bundle
