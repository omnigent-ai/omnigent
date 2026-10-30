"""``omnigent login`` behavior under a proxy-configured environment.

The login flow probes ``GET /v1/me`` and drives the detected auth flow with
plain httpx calls. Those calls must follow the repo's loopback convention
(``_trust_env_for``): an env-configured proxy can never reach a loopback
server, so loopback traffic bypasses ``HTTP(S)_PROXY`` entirely. Otherwise
the proxy's own answer (e.g. an unconditional 403) replaces the server's
401 ``login_url`` payload and login misdetects an accounts server as OIDC,
dying on ``POST /auth/cli-login`` with a misleading provider hint.

For non-loopback targets the proxy is honored (it may be the only route to
the server), so a probe answer that matches no Omnigent auth mode must fail
on the probe itself — naming the suspect proxy — instead of falling into
the OIDC ticket flow.
"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

import omnigent.cli as cli_mod

cli_group = cli_mod.cli

_PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


@pytest.fixture(autouse=True)
def token_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect auth_tokens.json and the global config to a temp dir.

    :param tmp_path: Pytest temp directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: The temp directory path.
    """
    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path",
        lambda: tmp_path / "auth_tokens.json",
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    return tmp_path


class _JsonHandler(BaseHTTPRequestHandler):
    """Base handler answering scripted JSON routes; subclasses fill ``routes``."""

    routes: dict[tuple[str, str], tuple[int, dict[str, object]]] = {}

    def _answer(self, method: str) -> None:
        key = (method, self.path.split("?")[0])
        status, body = self.routes.get(key, (404, {}))
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self._answer("GET")

    def do_POST(self) -> None:
        if self.headers.get("Content-Length"):
            self.rfile.read(int(self.headers["Content-Length"]))
        self._answer("POST")

    def log_message(self, *args: object) -> None:
        pass


def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    """Serve *handler* on a loopback port, yielding the base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture()
def deny_all_proxy() -> Iterator[str]:
    """An HTTP proxy answering 403 to everything, as a base URL."""

    class _DenyAll(BaseHTTPRequestHandler):
        def _deny(self) -> None:
            body = b"blocked by proxy policy"
            self.send_response(403)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_CONNECT = _deny

        def log_message(self, *args: object) -> None:
            pass

    yield from _serve(_DenyAll)


@pytest.fixture()
def accounts_server() -> Iterator[str]:
    """A loopback accounts-mode server stub."""

    class _Accounts(_JsonHandler):
        routes = {
            ("GET", "/v1/me"): (401, {"user_id": None, "login_url": "/login"}),
            (
                "POST",
                "/auth/login",
            ): (200, {"token": "jwt", "user": {"id": "admin"}, "expires_in": 3600}),
        }

    yield from _serve(_Accounts)


@pytest.fixture()
def oidc_server() -> Iterator[str]:
    """A loopback OIDC-mode server stub with an instantly fulfilled ticket."""

    class _Oidc(_JsonHandler):
        routes = {
            ("GET", "/v1/me"): (401, {"user_id": None, "login_url": "/auth/login"}),
            ("POST", "/auth/cli-login"): (200, {"ticket": "t1", "login_url": "/auth/go"}),
            (
                "GET",
                "/auth/cli-poll",
            ): (200, {"token": "jwt", "user_id": "alice", "expires_in": 3600}),
        }

    yield from _serve(_Oidc)


def _route_env_through_proxy(monkeypatch: pytest.MonkeyPatch, proxy_url: str) -> None:
    """Point every proxy env var at *proxy_url* with no NO_PROXY escape."""
    for var in _PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(var, proxy_url)


def _response(status: int, body: dict[str, object] | None = None) -> httpx.Response:
    """Build a real httpx.Response bound to a request, for scripted transports."""
    return httpx.Response(
        status,
        content=json.dumps(body).encode() if body is not None else b"",
        request=httpx.Request("GET", "https://probe.invalid/v1/me"),
    )


def test_login_accounts_flow_bypasses_env_proxy_for_loopback(
    monkeypatch: pytest.MonkeyPatch, accounts_server: str, deny_all_proxy: str
) -> None:
    """Loopback login must reach the server directly and run the accounts flow.

    With the proxy env honored, the deny-all proxy's 403 would replace the
    401 ``login_url`` payload and login would die on ``/auth/cli-login``.
    """
    _route_env_through_proxy(monkeypatch, deny_all_proxy)

    result = CliRunner().invoke(cli_group, ["login", accounts_server], input="admin\npw\n")

    assert result.exit_code == 0, result.output
    assert "accounts auth" in result.output
    assert "Logged in as admin" in result.output


def test_login_oidc_ticket_flow_bypasses_env_proxy_for_loopback(
    monkeypatch: pytest.MonkeyPatch, oidc_server: str, deny_all_proxy: str
) -> None:
    """The OIDC ticket POST and poll must also bypass the proxy on loopback."""
    _route_env_through_proxy(monkeypatch, deny_all_proxy)
    monkeypatch.setattr(webbrowser, "open", lambda url: True)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    result = CliRunner().invoke(cli_group, ["login", oidc_server])

    assert result.exit_code == 0, result.output
    assert "Logged in as alice" in result.output


def test_login_unexpected_probe_answer_fails_on_probe_not_cli_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe answer matching no auth mode fails loud instead of misdetecting OIDC.

    A 403 (here: what an intercepting corporate proxy returns for a
    non-loopback host) must not route login into the OIDC ticket flow —
    the error names the probe and suggests NO_PROXY, and ``/auth/cli-login``
    is never called.
    """
    posted: list[str] = []
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _response(403))
    monkeypatch.setattr(httpx, "post", lambda url, **kw: posted.append(url) or _response(403))
    for var in _PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.corp.example:3128")

    result = CliRunner().invoke(cli_group, ["login", "http://omni.internal:8000"])

    assert result.exit_code != 0
    assert "Unexpected response from http://omni.internal:8000/v1/me: HTTP 403" in result.output
    assert "NO_PROXY" in result.output
    assert "OMNIGENT_AUTH_PROVIDER" not in result.output
    assert posted == []


def test_login_unexpected_probe_answer_omits_proxy_hint_without_proxy_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without proxy env vars the probe failure must not blame a proxy."""
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _response(500))
    for var in _PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    result = CliRunner().invoke(cli_group, ["login", "http://omni.internal:8000"])

    assert result.exit_code != 0
    assert "Unexpected response from http://omni.internal:8000/v1/me: HTTP 500" in result.output
    assert "NO_PROXY" not in result.output


def test_login_unexpected_probe_answer_omits_proxy_hint_when_no_proxy_covers_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When NO_PROXY already excludes the host, the failure must not blame a proxy."""
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _response(500))
    for var in _PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.corp.example:3128")
    monkeypatch.setenv("NO_PROXY", "omni.internal")

    result = CliRunner().invoke(cli_group, ["login", "http://omni.internal:8000"])

    assert result.exit_code != 0
    assert "Unexpected response from http://omni.internal:8000/v1/me: HTTP 500" in result.output
    assert "NO_PROXY" not in result.output


def test_login_probe_hint_ignores_other_scheme_proxy_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proxy var for a different scheme cannot have answered, so no hint.

    httpx routes an http:// request only through HTTP_PROXY/ALL_PROXY —
    an HTTPS_PROXY-only environment must not be blamed for it.
    """
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _response(500))
    for var in _PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp.example:3128")

    result = CliRunner().invoke(cli_group, ["login", "http://omni.internal:8000"])

    assert result.exit_code != 0
    assert "Unexpected response from http://omni.internal:8000/v1/me: HTTP 500" in result.output
    assert "NO_PROXY" not in result.output


def test_login_probe_hint_honors_port_qualified_no_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A port-qualified NO_PROXY entry covering the target suppresses the hint.

    httpx honors ``NO_PROXY=host:port`` entries, so when one excludes the
    target the answer really came from the server.
    """
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _response(500))
    for var in _PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.corp.example:3128")
    monkeypatch.setenv("NO_PROXY", "omni.internal:8000")

    result = CliRunner().invoke(cli_group, ["login", "http://omni.internal:8000"])

    assert result.exit_code != 0
    assert "Unexpected response from http://omni.internal:8000/v1/me: HTTP 500" in result.output
    assert "NO_PROXY" not in result.output


def test_login_unrecognized_401_falls_back_to_oidc_ticket_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401 without a recognized login_url keeps the OIDC compatibility path.

    Older OIDC servers and auth middlewares answer 401 without the JSON
    ``login_url`` payload; login must still try ``/auth/cli-login`` for
    them (regression pin for the documented compatibility exception —
    only non-401 probe answers fail on the probe itself).
    """
    html_401 = httpx.Response(
        401,
        content=b"<html>authentication required</html>",
        request=httpx.Request("GET", "https://probe.invalid/v1/me"),
    )
    posted: list[str] = []
    monkeypatch.setattr(httpx, "get", lambda url, **kw: html_401)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: posted.append(url) or _response(503))
    for var in _PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    result = CliRunner().invoke(cli_group, ["login", "http://omni.internal:8000"])

    assert result.exit_code != 0
    assert posted == ["http://omni.internal:8000/auth/cli-login"]
    assert "OMNIGENT_AUTH_PROVIDER" in result.output


def test_login_probe_keeps_env_proxy_support_for_non_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-loopback targets must keep trust_env on: the env proxy may be the only route."""
    seen: list[object] = []

    def _capture(url: str, **kw: object) -> httpx.Response:
        seen.append(kw.get("trust_env"))
        return _response(403)

    monkeypatch.setattr(httpx, "get", _capture)
    for var in _PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    CliRunner().invoke(cli_group, ["login", "http://omni.internal:8000"])

    assert seen == [True]
