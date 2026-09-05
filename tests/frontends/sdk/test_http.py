"""Loopback detection and the proxy bypass it drives on the SDK client.

A machine with an HTTP proxy configured — via ``HTTP_PROXY``/``ALL_PROXY``,
or on Windows via the system registry, which ``getproxies()`` also reads —
cannot reach its own local Omnigent server through that proxy: the proxy
resolves ``127.0.0.1`` against itself. httpx trusts the environment by
default, so a client built without ``trust_env=False`` fails with
``httpx.ConnectError: All connection attempts failed`` even though the
server is listening and healthy.
"""

from __future__ import annotations

import pytest
from omnigent_client import OmnigentClient
from omnigent_client._http import is_loopback_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:6767",
        "http://127.0.0.1:6767/v1/sessions",
        # The whole 127.0.0.0/8 block is loopback, not just .0.1.
        "http://127.9.9.9:80",
        "http://localhost:8080",
        # urlsplit lowercases the host, so casing must not matter.
        "http://LocalHost:8080",
        # RFC 6761 reserves .localhost names for the loopback interface.
        "http://dev.localhost:8080",
        "http://[::1]:6767",
        "https://127.0.0.1",
    ],
)
def test_loopback_urls_are_detected(url: str) -> None:
    """Every form of "this machine" must be classified as loopback.

    A false negative here is the bug: the client keeps trusting the
    environment's proxy and cannot reach the local server at all.
    """
    assert is_loopback_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://example.databricksapps.com",
        "https://omnigent.internal:6767",
        # Private, but still another host — reaching it may require the proxy.
        "http://10.0.0.5:6767",
        # Hostname that merely starts with the loopback IP.
        "http://127.0.0.1.example.com",
        "http://notlocalhost:8080",
        "",
    ],
)
def test_remote_urls_are_not_loopback(url: str) -> None:
    """Remote targets keep their proxy.

    A false positive would strip the proxy a corporate network needs to
    reach the server at all, so the check must not over-match.
    """
    assert is_loopback_url(url) is False


@pytest.mark.parametrize(
    ("server_url", "expected_trust_env"),
    [
        ("http://127.0.0.1:6767", False),
        ("http://localhost:6767", False),
        ("https://example.databricksapps.com", True),
    ],
)
def test_client_bypasses_env_proxies_only_for_loopback(
    server_url: str, expected_trust_env: bool
) -> None:
    """The client wires loopback detection into httpx's ``trust_env``.

    Without this the CLI's session-create call against a healthy local
    server dies on a proxied connection, which surfaces as a crash rather
    than anything the user can act on.
    """
    client = OmnigentClient(base_url=server_url)

    assert client._http.trust_env is expected_trust_env
