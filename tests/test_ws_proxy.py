"""Tests for mandatory-egress proxy support for WebSocket tunnels.

Covers the env-based proxy selection (``omnigent.util.ws_proxy.ws_env_proxy_url``)
and the blocking CONNECT dialer (``open_proxy_connect_socket``) against a real
in-process proxy socket.
"""

from __future__ import annotations

import base64
import socket
import threading

import pytest

from omnigent.util.ws_proxy import (
    open_proxy_connect_socket,
    redact_proxy_url,
    ws_env_proxy_url,
)

_TUNNEL_URL = "ws://server.sandbox.test:8000/v1/hosts/h/tunnel"


class _FakeConnectProxy:
    """One-shot CONNECT proxy: records the request, sends a canned reply.

    After a 2xx reply it echoes everything it receives, so the test can
    prove the returned socket is the tunnel (bytes round-trip through it).
    """

    def __init__(self, reply: bytes) -> None:
        self._reply = reply
        self.request: bytes = b""
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.port: int = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve_once, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        """The proxy URL a client would put in ``http_proxy``."""
        return f"http://127.0.0.1:{self.port}"

    def _serve_once(self) -> None:
        conn, _ = self._server.accept()
        conn.settimeout(5.0)
        try:
            while b"\r\n\r\n" not in self.request:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                self.request += chunk
            conn.sendall(self._reply)
            if not self._reply.startswith(b"HTTP/1.1 2"):
                return
            # Tunnel established: echo so the test can round-trip bytes.
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                conn.sendall(data)
        except OSError:
            return
        finally:
            conn.close()

    def close(self) -> None:
        self._server.close()
        self._thread.join(timeout=5.0)


# --- proxy selection -------------------------------------------------------


def test_ws_url_follows_http_proxy() -> None:
    """A ws:// tunnel uses http_proxy, like a plain-http client."""
    env = {"http_proxy": "http://127.0.0.1:3128"}
    assert ws_env_proxy_url(_TUNNEL_URL, env) == "http://127.0.0.1:3128"


def test_wss_url_follows_https_proxy_not_http_proxy() -> None:
    """A wss:// tunnel uses https_proxy; http_proxy alone does not apply."""
    assert ws_env_proxy_url("wss://s.test/t", {"https_proxy": "http://p:1"}) == "http://p:1"
    assert ws_env_proxy_url("wss://s.test/t", {"http_proxy": "http://p:1"}) is None


def test_all_proxy_is_the_fallback_for_both_schemes() -> None:
    """all_proxy applies when the scheme-specific variable is unset."""
    env = {"ALL_PROXY": "http://fallback:8080"}
    assert ws_env_proxy_url("ws://s.test/t", env) == "http://fallback:8080"
    assert ws_env_proxy_url("wss://s.test/t", env) == "http://fallback:8080"


def test_lowercase_env_wins_over_uppercase() -> None:
    """The conventional lowercase form takes precedence."""
    env = {"http_proxy": "http://lower:1", "HTTP_PROXY": "http://upper:2"}
    assert ws_env_proxy_url("ws://s.test/t", env) == "http://lower:1"


def test_no_proxy_env_means_direct_dial() -> None:
    """Without proxy variables the tunnel dials direct (returns None)."""
    assert ws_env_proxy_url(_TUNNEL_URL, {}) is None


def test_bare_host_port_proxy_value_is_treated_as_http() -> None:
    """A scheme-less proxy value is conventionally a plain HTTP proxy."""
    assert ws_env_proxy_url("ws://s.test/t", {"http_proxy": "proxy:3128"}) == "http://proxy:3128"


def test_unsupported_proxy_scheme_dials_direct() -> None:
    """SOCKS proxies are not spoken; fall back to the direct dial."""
    assert ws_env_proxy_url("ws://s.test/t", {"http_proxy": "socks5://p:1080"}) is None


def test_no_proxy_bypasses_exact_host_and_subdomains() -> None:
    """no_proxy entries match the host itself and its subdomains."""
    env = {"http_proxy": "http://p:1", "no_proxy": "example.com"}
    assert ws_env_proxy_url("ws://example.com/t", env) is None
    assert ws_env_proxy_url("ws://sub.example.com/t", env) is None
    assert ws_env_proxy_url("ws://notexample.com/t", env) == "http://p:1"


def test_no_proxy_wildcard_and_dotted_forms_match_subdomains() -> None:
    """``*.example.com`` and ``.example.com`` behave like ``example.com``."""
    for entry in ("*.example.com", ".example.com"):
        env = {"http_proxy": "http://p:1", "no_proxy": entry}
        assert ws_env_proxy_url("ws://sub.example.com/t", env) is None


def test_no_proxy_star_disables_proxying_entirely() -> None:
    """A lone ``*`` entry turns the proxy off for every host."""
    env = {"http_proxy": "http://p:1", "no_proxy": "*"}
    assert ws_env_proxy_url("ws://anything.test/t", env) is None


def test_no_proxy_port_entry_requires_matching_port() -> None:
    """A host:port entry bypasses only that port."""
    env = {"http_proxy": "http://p:1", "no_proxy": "example.com:8443"}
    assert ws_env_proxy_url("ws://example.com:8443/t", env) is None
    assert ws_env_proxy_url("ws://example.com:8000/t", env) == "http://p:1"


def test_no_proxy_loopback_entries_bypass_loopback_targets() -> None:
    """The sandbox-standard loopback exemptions dial direct."""
    env = {"http_proxy": "http://p:1", "no_proxy": "localhost,127.0.0.1,::1"}
    assert ws_env_proxy_url("ws://127.0.0.1:8000/t", env) is None
    assert ws_env_proxy_url("ws://localhost:8000/t", env) is None
    assert ws_env_proxy_url("ws://[::1]:8000/t", env) is None
    assert ws_env_proxy_url(_TUNNEL_URL, env) == "http://p:1"


def test_non_ws_or_hostless_urls_never_proxy() -> None:
    """Only ws(s):// URLs with a host consult the proxy env."""
    env = {"http_proxy": "http://p:1", "all_proxy": "http://p:1"}
    assert ws_env_proxy_url("unix:///tmp/sock", env) is None
    assert ws_env_proxy_url("not a url", env) is None


def test_redact_proxy_url_strips_credentials() -> None:
    """Logging never carries proxy credentials."""
    assert redact_proxy_url("http://user:secret@proxy:3128") == "http://proxy:3128"
    assert redact_proxy_url("http://proxy:3128") == "http://proxy:3128"


# --- CONNECT dialer --------------------------------------------------------


async def test_connect_dialer_establishes_a_byte_tunnel() -> None:
    """A 200 reply yields a live socket piped through the proxy."""
    proxy = _FakeConnectProxy(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    sock = None
    try:
        sock = await open_proxy_connect_socket(proxy.url, _TUNNEL_URL, timeout=5.0)
        head = proxy.request.split(b"\r\n\r\n", 1)[0]
        lines = head.split(b"\r\n")
        assert lines[0] == b"CONNECT server.sandbox.test:8000 HTTP/1.1"
        assert b"Host: server.sandbox.test:8000" in lines
        # The tunnel is long-lived; the handshake timeout must not linger.
        assert sock.gettimeout() is None
        sock.settimeout(5.0)
        sock.sendall(b"ping")
        assert sock.recv(4) == b"ping"
    finally:
        if sock is not None:
            sock.close()
        proxy.close()


async def test_connect_dialer_sends_basic_proxy_auth() -> None:
    """Credentials in the proxy URL ride as Proxy-Authorization: Basic."""
    proxy = _FakeConnectProxy(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    proxy_url = f"http://alice:open%20sesame@127.0.0.1:{proxy.port}"
    sock = None
    try:
        sock = await open_proxy_connect_socket(proxy_url, _TUNNEL_URL, timeout=5.0)
        expected = base64.b64encode(b"alice:open sesame").decode("ascii")
        assert f"Proxy-Authorization: Basic {expected}".encode("latin-1") in proxy.request
    finally:
        if sock is not None:
            sock.close()
        proxy.close()


async def test_connect_dialer_raises_on_proxy_refusal() -> None:
    """A non-2xx CONNECT reply is a connection error, not a hang."""
    proxy = _FakeConnectProxy(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
    try:
        with pytest.raises(OSError, match="refused CONNECT"):
            await open_proxy_connect_socket(proxy.url, _TUNNEL_URL, timeout=5.0)
    finally:
        proxy.close()


async def test_connect_dialer_raises_on_bytes_before_the_client_speaks() -> None:
    """Early bytes after the 200 mean a confused proxy; fail, don't desync."""
    proxy = _FakeConnectProxy(b"HTTP/1.1 200 Connection Established\r\n\r\nGARBAGE")
    try:
        with pytest.raises(OSError, match="unexpected bytes"):
            await open_proxy_connect_socket(proxy.url, _TUNNEL_URL, timeout=5.0)
    finally:
        proxy.close()


async def test_connect_dialer_raises_when_proxy_hangs_up() -> None:
    """A proxy that closes without replying surfaces as a connection error."""
    proxy = _FakeConnectProxy(b"")
    try:
        with pytest.raises(OSError, match="closed the connection"):
            await open_proxy_connect_socket(proxy.url, _TUNNEL_URL, timeout=5.0)
    finally:
        proxy.close()
