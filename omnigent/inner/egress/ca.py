"""CA certificate generation, caching, and combined bundle creation.

Generates a self-signed CA used by the MITM proxy to sign per-host
certificates. The CA key and cert are cached on disk so they persist
across agent runs. A combined PEM bundle (system CAs + our CA) is
created for injection into the sandbox via environment variables.
"""

from __future__ import annotations

import datetime
import logging
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR = os.path.expanduser("~/.cache/omnigent-egress")
_CA_VALIDITY_DAYS = 365
_CA_KEY_SIZE = 2048


def ensure_ca(cache_dir: Path | None = None) -> tuple[Path, Path]:
    """Return ``(ca_cert_path, ca_key_path)``, generating if needed.

    The CA is regenerated when the cert file is missing or expired.

    :param cache_dir: Directory for cached CA files. Defaults to
        ``~/.cache/omnigent-egress``.
    :returns: Tuple of ``(cert_path, key_path)`` as absolute :class:`Path`.
    """
    cache = cache_dir or Path(_DEFAULT_CACHE_DIR)
    cache.mkdir(parents=True, exist_ok=True)

    cert_path = cache / "ca.pem"
    key_path = cache / "ca-key.pem"

    if cert_path.exists() and key_path.exists():
        if not _is_expired(cert_path):
            logger.debug("Reusing cached CA at %s", cert_path)
            return cert_path, key_path
        logger.info("Cached CA expired — regenerating")

    _generate_ca(cert_path, key_path)
    return cert_path, key_path


def ensure_ca_bundle(
    ca_cert_path: Path,
    cache_dir: Path | None = None,
    extra_ca_certs: list[str] | None = None,
) -> Path:
    """Create a combined PEM bundle: system CAs + extra CAs + our MITM CA.

    :param ca_cert_path: Path to the MITM proxy's own CA certificate.
    :param cache_dir: Directory for the cached bundle file.
    :param extra_ca_certs: Paths to additional PEM CA certificate files
        to include (e.g. staging/internal CAs not in the public trust
        store).
    :returns: Path to the combined bundle file.
    """
    cache = cache_dir or Path(_DEFAULT_CACHE_DIR)
    cache.mkdir(parents=True, exist_ok=True)
    bundle_path = cache / "ca-bundle.pem"

    parts: list[bytes] = [_system_ca_bundle()]

    for cert_path_str in extra_ca_certs or ():
        p = Path(cert_path_str).expanduser()
        if not p.exists():
            logger.warning("Extra CA cert not found, skipping: %s", p)
            continue
        data = p.read_bytes()
        if b"-----BEGIN CERTIFICATE-----" not in data:
            logger.warning("Extra CA cert is not PEM, skipping: %s", p)
            continue
        parts.append(data)
        logger.info("Including extra CA cert: %s", p)

    parts.append(ca_cert_path.read_bytes())

    bundle_path.write_bytes(b"\n".join(parts))
    logger.debug("Wrote combined CA bundle to %s", bundle_path)
    return bundle_path


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _generate_ca(cert_path: Path, key_path: Path) -> None:
    """Generate a new self-signed CA key + cert and write to disk."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=_CA_KEY_SIZE)

    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "Omnigent Egress MITM CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "omnigent"),
        ]
    )

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=_CA_VALIDITY_DAYS))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    os.chmod(key_path, 0o600)

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    logger.info("Generated new CA cert at %s", cert_path)


def _is_expired(cert_path: Path) -> bool:
    """Return True if the PEM cert at *cert_path* has expired."""
    try:
        data = cert_path.read_bytes()
        cert = x509.load_pem_x509_certificate(data)
        now = datetime.datetime.now(datetime.timezone.utc)
        return now >= cert.not_valid_after_utc
    except Exception:  # noqa: BLE001 — invalid/missing CA cert regenerates
        return True


def _system_ca_bundle() -> bytes:
    """Return the system CA bundle as PEM bytes.

    Prefers the OS trust store (``ssl.get_default_verify_paths``) so
    that CAs added by corporate MDM, IT policy, or the user are
    included. Falls back to the certifi (Mozilla) bundle when the OS
    path doesn't exist or is empty. Path resolution is shared with
    :func:`omnigent.tls.resolve_ca_file` (single source of truth).
    """
    from omnigent.tls import resolve_ca_file

    return Path(resolve_ca_file()).read_bytes()
