"""HTTP transport helpers shared by the client and its callers."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


def is_loopback_url(url: str) -> bool:
    """
    Report whether *url* addresses this machine's loopback interface.

    Loopback traffic must never go through an HTTP proxy: the proxy
    resolves ``127.0.0.1`` against itself, so the request reaches the
    wrong host or is refused outright. httpx reads proxy settings from
    the environment by default, and on Windows that also covers the
    system registry — so a machine with any proxy configured cannot
    reach its own local server unless the caller passes
    ``trust_env=False``.

    :param url: Absolute URL to classify, e.g.
        ``"http://127.0.0.1:6767"``.
    :returns: ``True`` for loopback targets — ``localhost``, any
        ``.localhost`` name, ``127.0.0.0/8``, or ``::1``. ``False``
        otherwise, including for a URL with no host.
    """
    host = urlsplit(url).hostname
    if host is None:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
