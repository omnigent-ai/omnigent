"""Enable verified TLS on local fake upstreams, including child processes."""

import ssl
from http.server import ThreadingHTTPServer
from pathlib import Path

from omnigent.inner.egress.ca import ensure_ca
from omnigent.inner.egress.certs import HostCertCache


def enable_https(server: ThreadingHTTPServer, directory: Path) -> dict[str, str]:
    """Return per-test trust settings for an HTTPS server addressed as localhost."""
    ca, key = ensure_ca(cache_dir=directory)
    context = HostCertCache(ca, key).get_ssl_context("localhost")
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return {"SSL_CERT_FILE": str(ca), "REQUESTS_CA_BUNDLE": str(ca)}
