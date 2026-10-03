"""Throwaway certificate authority for intercepting a harness's HTTPS model traffic.

Machines with Claude Code managed settings pin ``ANTHROPIC_BASE_URL`` to a
corporate gateway that environment variables cannot override. The lab points
``HTTPS_PROXY`` at its model proxy and trusts this CA through
``NODE_EXTRA_CA_CERTS``, so the proxy can terminate the harness's TLS and serve
model calls from the mock. Nothing intercepted is forwarded off the machine.
"""

from __future__ import annotations

import datetime
import ssl
import threading
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_VALIDITY = datetime.timedelta(days=2)


class TlsInterceptor:
    """Mint per-host server certificates signed by a lab-local CA.

    :param directory: Where the CA certificate and minted keys are written.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._contexts: dict[str, ssl.SSLContext] = {}
        now = datetime.datetime.now(datetime.UTC)
        self._key = ec.generate_private_key(ec.SECP256R1())
        self._name = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "Omnigent resilience lab CA")]
        )
        self._ca = (
            x509.CertificateBuilder()
            .subject_name(self._name)
            .issuer_name(self._name)
            .public_key(self._key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(hours=1))
            .not_valid_after(now + _VALIDITY)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(self._key, hashes.SHA256())
        )
        self.ca_path = directory / "lab-ca.pem"
        self.ca_path.write_bytes(self._ca.public_bytes(serialization.Encoding.PEM))

    def server_context(self, host: str) -> ssl.SSLContext:
        """Return a server-side TLS context presenting a certificate for *host*.

        :param host: Hostname from the client's CONNECT, e.g. ``"api.anthropic.com"``.
        :returns: A cached context for that host.
        """
        with self._lock:
            context = self._contexts.get(host)
            if context is None:
                context = self._mint(host)
                self._contexts[host] = context
            return context

    def _mint(self, host: str) -> ssl.SSLContext:
        now = datetime.datetime.now(datetime.UTC)
        key = ec.generate_private_key(ec.SECP256R1())
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(self._name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(hours=1))
            .not_valid_after(now + _VALIDITY)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
            )
            .sign(self._key, hashes.SHA256())
        )
        stem = self._directory / host.replace(":", "_")
        cert_path = stem.with_suffix(".pem")
        key_path = stem.with_suffix(".key")
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert_path, key_path)
        return context
