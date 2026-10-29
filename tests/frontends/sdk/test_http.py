"""Loopback detection and the proxy bypass it drives on the SDK client.

A machine with an HTTP proxy configured — via ``HTTP_PROXY``/``ALL_PROXY``,
or on Windows via the system registry, which ``getproxies()`` also reads —
cannot reach its own local Omnigent server through that proxy: the proxy
resolves ``127.0.0.1`` against itself. httpx trusts the environment by
default, so a client built without ``trust_env=False`` fails with
``httpx.ConnectError: All connection attempts failed`` even though the
server is listening and healthy.

The client's opt-in headers are checked here too.
"""

from __future__ import annotations

import httpx
import pytest
from omnigent_client import TERMINAL_FALLBACK_HEADER, OmnigentClient
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


@pytest.mark.parametrize("opted_in", [False, True])
async def test_client_asks_for_prompts_held_in_terminal_only_when_told(opted_in: bool) -> None:
    """Raw ``sessions.stream()`` callers see held prompts expire unless they send the header."""
    stream_requests: list[httpx.Request] = []

    def _end_stream(request: httpx.Request) -> httpx.Response:
        stream_requests.append(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=b"data: [DONE]\n\n"
        )

    headers = {"Authorization": "Bearer token"}
    if opted_in:
        headers[TERMINAL_FALLBACK_HEADER] = "on"
    async with OmnigentClient("http://127.0.0.1:9", headers=headers) as client:
        client._http._transport = httpx.MockTransport(_end_stream)
        assert [event async for event in client.sessions.stream("conv_abc")] == []

    [stream_request] = stream_requests
    assert stream_request.url.path == "/v1/sessions/conv_abc/stream"
    assert stream_request.headers.get("x-omnigent-terminal-fallback") == (
        "on" if opted_in else None
    )
    assert stream_request.headers["authorization"] == "Bearer token"
