"""Per-host TLS certificate generation for the MITM proxy.

Each intercepted HTTPS connection requires a certificate that the
client will trust. We generate short-lived certs on the fly, signed
by the CA from :mod:`~omnigent.inner.egress.ca`, and cache them in
memory for the proxy's lifetime.
"""

from __future__ import annotations

import datetime
import functools
import logging
import ssl
import tempfile
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import dsa, ec, rsa
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)

_HOST_KEY_SIZE = 2048
_HOST_CERT_VALIDITY_HOURS = 24
_CACHE_MAX_SIZE = 256


class HostCertCache:
    """LRU cache of per-host TLS certificates signed by a given CA.

    :param ca_cert_path: Path to the CA certificate PEM file.
    :param ca_key_path: Path to the CA private key PEM file.
    """

    def __init__(self, ca_cert_path: Path, ca_key_path: Path) -> None:
        self._ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
        ca_public_key = self._ca_cert.public_key()
        if not isinstance(
            ca_public_key,
            rsa.RSAPublicKey | dsa.DSAPublicKey | ec.EllipticCurvePublicKey,
        ):
            raise ValueError("CA certificate must use an RSA, DSA, or EC public key")
        ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
        if not isinstance(
            ca_key,
            rsa.RSAPrivateKey | dsa.DSAPrivateKey | ec.EllipticCurvePrivateKey,
        ):
            raise ValueError("CA private key must use an RSA, DSA, or EC signing key")
        self._ca_public_key = ca_public_key
        self._ca_key = ca_key
        self._get_or_create = functools.lru_cache(maxsize=_CACHE_MAX_SIZE)(self._generate)

    def get_ssl_context(self, hostname: str) -> ssl.SSLContext:
        """Return a server-side SSLContext for *hostname*.

        :param hostname: The hostname to generate a certificate for,
            e.g. ``"api.github.com"``.
        :returns: An ssl.SSLContext configured for TLS server use
            with the generated certificate.
        """
        cert_pem, key_pem = self._get_or_create(hostname)
        return self._build_context(cert_pem, key_pem)

    def _generate(self, hostname: str) -> tuple[bytes, bytes]:
        """Generate a cert + key for *hostname*, signed by the CA."""
        key = rsa.generate_private_key(public_exponent=65537, key_size=_HOST_KEY_SIZE)

        now = datetime.datetime.now(datetime.timezone.utc)

        builder = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name(
                    [
                        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
                    ]
                )
            )
            .issuer_name(self._ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(hours=_HOST_CERT_VALIDITY_HOURS))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(hostname)]),
                critical=False,
            )
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._ca_public_key),
                critical=False,
            )
        )

        cert = builder.sign(self._ca_key, hashes.SHA256())

        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )

        logger.debug("Generated host cert for %s", hostname)
        return cert_pem, key_pem

    @staticmethod
    def _build_context(cert_pem: bytes, key_pem: bytes) -> ssl.SSLContext:
        """Build an SSLContext from in-memory PEM cert/key bytes."""
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as cf:
            cf.write(cert_pem)
            cert_file = cf.name
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as kf:
            kf.write(key_pem)
            key_file = kf.name
        try:
            ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
        finally:
            Path(cert_file).unlink(missing_ok=True)
            Path(key_file).unlink(missing_ok=True)
        return ctx
