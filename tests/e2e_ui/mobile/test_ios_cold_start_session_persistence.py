"""iOS shell cold start: the ticket-login session must survive a relaunch.

Linux CI has no iOS runtime, so this re-enacts the shell's ticket login against
a real OIDC-enabled ``omnigent server`` and drives a persistent Chromium profile
(iPhone device profile) as a WKWebView stand-in; closing and relaunching it
plays force-quit + reopen. The cookie mirrors ``OidcLoginManager.sessionCookie``
with the expiry from ``/auth/cli-poll``. Chromium is not WebKit, so the native
side stays covered by ``OidcLoginManagerTests.swift``.
"""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Route,
    expect,
)
from playwright.sync_api import (
    Error as PlaywrightError,
)
from starlette.responses import HTMLResponse

from tests.e2e_ui.conftest import _find_free_port

_REPO_ROOT = Path(__file__).resolve().parents[3]
_IDP_EMAIL = "ios-user@example.test"
_CLIENT_ID = "ios-cold-start-client"
_SESSION_TTL_HOURS = 720
_SHELL_POLL_INTERVAL_S = 2.0
_SHELL_POLL_TIMEOUT_S = 300.0
_BOOT_WINDOW_S = 15.0
_RECORD_DIR_ENV = "OMNIGENT_E2E_RECORD_DIR"

_TEST_AGENT_YAML = """\
name: hello_world
prompt: You are a friendly assistant. Say hello and answer questions.

executor:
  model: gpt-4o-mini
  harness: openai-agents
"""


# ── Mock OpenID provider ──────────────────────────────────────────────────────


def _build_idp_app(issuer: str, key: rsa.RSAPrivateKey, kid: str) -> FastAPI:
    app = FastAPI()
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    public_jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})

    @app.get("/.well-known/openid-configuration")
    async def discovery() -> dict[str, object]:
        return {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token",
            "jwks_uri": f"{issuer}/jwks",
            "userinfo_endpoint": f"{issuer}/userinfo",
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
        }

    @app.get("/jwks")
    async def jwks() -> dict[str, object]:
        return {"keys": [public_jwk]}

    @app.get("/authorize")
    async def authorize(request: Request) -> HTMLResponse:
        query = request.query_params
        continue_url = (
            f"{query['redirect_uri']}?"
            f"{urlencode({'code': 'ios-cold-start-code', 'state': query['state']})}"
        )
        return HTMLResponse(
            "<html><body style='font-family:system-ui;padding:40px'>"
            "<h1>Identity provider sign-in</h1>"
            f'<a href="{html.escape(continue_url, quote=True)}">Continue as {_IDP_EMAIL}</a>'
            "</body></html>"
        )

    @app.post("/token")
    async def token(request: Request) -> dict[str, object]:
        form = parse_qs((await request.body()).decode())
        assert form.get("code") == ["ios-cold-start-code"], form
        now = int(time.time())
        id_token = jwt.encode(
            {
                "iss": issuer,
                "aud": _CLIENT_ID,
                "sub": "ios-user",
                "email": _IDP_EMAIL,
                "email_verified": True,
                "iat": now,
                "auth_time": now,
                "exp": now + 300,
            },
            key,
            algorithm="RS256",
            headers={"kid": kid},
        )
        return {
            "access_token": "ios-cold-start-access",
            "token_type": "Bearer",
            "expires_in": 3600,
            "id_token": id_token,
        }

    return app


def _wait_for_uvicorn(server: uvicorn.Server, what: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.started:
            return
        time.sleep(0.05)
    raise RuntimeError(f"{what} did not start")


@pytest.fixture(scope="module")
def mock_idp() -> Iterator[str]:
    """Serve a minimal OpenID provider; yields its issuer URL."""
    port = _find_free_port()
    issuer = f"http://127.0.0.1:{port}"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server = uvicorn.Server(
        uvicorn.Config(
            _build_idp_app(issuer, key, kid="ios-cold-start-key"),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_for_uvicorn(server, "mock IdP")
        yield issuer
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# ── OIDC-enabled omnigent server ──────────────────────────────────────────────


@dataclass(frozen=True)
class OidcServer:
    origin: str
    log_path: Path


@pytest.fixture(scope="module")
def oidc_server(
    mock_idp: str, tmp_path_factory: pytest.TempPathFactory, built_spa: None
) -> Iterator[OidcServer]:
    """Spawn the real server with OIDC auth and a 720h session TTL.

    ``built_spa`` builds the SPA bundle the server serves (a no-op under
    ``--ui-skip-build``), matching the shared OIDC login-flow test.
    """
    port = _find_free_port()
    origin = f"http://127.0.0.1:{port}"
    server_tmp = tmp_path_factory.mktemp("ios_cold_start_server")
    log_path = server_tmp / "server.log"
    agent_yaml = server_tmp / "hello_world.yaml"
    agent_yaml.write_text(_TEST_AGENT_YAML)
    env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_AUTH_PROVIDER": "oidc",
        "OMNIGENT_OIDC_ISSUER": mock_idp,
        "OMNIGENT_OIDC_CLIENT_ID": _CLIENT_ID,
        "OMNIGENT_OIDC_CLIENT_SECRET": "ios-cold-start-secret",
        "OMNIGENT_OIDC_REDIRECT_URI": f"{origin}/auth/callback",
        "OMNIGENT_OIDC_COOKIE_SECRET": secrets.token_hex(32),
        "OMNIGENT_OIDC_SESSION_TTL_HOURS": str(_SESSION_TTL_HOURS),
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "ANTHROPIC_API_KEY": "",
    }
    env.pop("OMNIGENT_AUTH_ENABLED", None)
    with open(log_path, "w") as log_handle:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{server_tmp / 'test.db'}",
                "--artifact-location",
                str(server_tmp / "artifacts"),
                "--agent",
                str(agent_yaml),
            ],
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 120
            last_error = "not polled yet"
            ready = False
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    last_error = f"exited with {proc.returncode}"
                    break
                try:
                    info = httpx.get(f"{origin}/v1/info", timeout=2)
                    if info.status_code == 200 and info.json().get("login_url") == "/auth/login":
                        ready = True
                        break
                    last_error = f"/v1/info {info.status_code}: {info.text[:200]}"
                except (httpx.HTTPError, ValueError) as exc:
                    # ValueError covers a 200 with a non-JSON body during boot.
                    last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(0.5)
            if not ready:
                log_handle.flush()
                raise RuntimeError(
                    f"OIDC server not ready: {last_error}\n{log_path.read_text()[-3000:]}"
                )
            yield OidcServer(origin=origin, log_path=log_path)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


# ── Shell re-enactment ────────────────────────────────────────────────────────


def _request_ticket(origin: str) -> tuple[str, str]:
    response = httpx.post(f"{origin}/auth/cli-login", content=b"", timeout=10)
    assert response.status_code == 200, response.text
    payload = response.json()
    login_url = payload["login_url"]
    assert login_url.startswith("/") and not login_url.startswith("//"), login_url
    return payload["ticket"], f"{origin}{login_url}"


def _complete_login_in_system_browser(browser: Browser, login_url: str) -> None:
    """Safari stand-in: an isolated context completes the IdP login for the ticket."""
    system_browser = browser.new_context()
    try:
        tab = system_browser.new_page()
        tab.goto(login_url)
        expect(tab.get_by_role("heading", name="Identity provider sign-in")).to_be_visible()
        tab.get_by_role("link", name=f"Continue as {_IDP_EMAIL}").click()
        expect(tab.get_by_role("heading", name="Login successful")).to_be_visible()
        expect(tab.get_by_text("return to the terminal")).to_be_visible()
    finally:
        system_browser.close()


def _poll_for_token(origin: str, ticket: str) -> dict[str, object]:
    deadline = time.monotonic() + _SHELL_POLL_TIMEOUT_S
    last = "no response"
    while time.monotonic() < deadline:
        time.sleep(_SHELL_POLL_INTERVAL_S)
        response = httpx.get(f"{origin}/auth/cli-poll", params={"ticket": ticket}, timeout=10)
        last = f"{response.status_code}: {response.text[:200]}"
        if response.status_code == 200:
            return response.json()
        if response.status_code == 410:
            raise AssertionError(f"ticket expired: {response.text}")
        # cli-poll documents 202 as its only pending status; fail fast otherwise.
        assert response.status_code == 202, f"unexpected cli-poll status {last}"
    raise AssertionError(f"cli-poll never returned the token; last response {last}")


def _shell_session_cookie(origin: str, token: str, *, expires_in: int | None) -> dict[str, object]:
    """Build the persistent or session-only cookie for the restart test.

    ``None`` is this test's control (no expiry, so Chromium drops it); the shell
    always sets an expiry, falling back to 8h only when the server omits one. The
    test server is http, so this uses the ``ap_session`` name; the HTTPS
    ``__Host-ap_session`` naming is covered by the Swift unit tests.
    """
    cookie: dict[str, object] = {"name": "ap_session", "value": token, "url": origin}
    if expires_in is not None:
        cookie["expires"] = time.time() + expires_in
    return cookie


def _redacted(payload: dict[str, object]) -> dict[str, object]:
    return {
        key: (f"<{len(value)} chars>" if key in {"token", "refresh_token"} else value)
        for key, value in payload.items()
        if isinstance(value, (str, int, float, bool)) or value is None
    }


# ── WebView stand-in ──────────────────────────────────────────────────────────


@dataclass
class AppBoot:
    label: str
    me_status: int | None = None
    api_401_paths: list[str] = field(default_factory=list)
    login_redirect_url: str | None = None
    login_redirect_status: int | None = None
    login_redirect_location: str | None = None
    login_redirect_error: str | None = None
    final_url: str = ""
    opening_sign_in_visible: bool = False
    session_cookie_present: bool = False
    session_cookie_expires: float | None = None


class WebViewStandIn:
    """Persistent iPhone-profile Chromium playing the shell's WKWebView."""

    def __init__(self, playwright: Playwright, profile_dir: Path, record_dir: Path | None) -> None:
        device = dict(playwright.devices["iPhone 13"])
        device.pop("default_browser_type", None)
        kwargs: dict[str, object] = {}
        if record_dir is not None:
            kwargs["record_video_dir"] = str(record_dir)
            kwargs["record_video_size"] = device["viewport"]
        self.context: BrowserContext = playwright.chromium.launch_persistent_context(
            str(profile_dir), headless=True, **device, **kwargs
        )
        self.page: Page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self._boot: AppBoot | None = None
        self.page.on("response", self._on_response)

    def _on_response(self, response) -> None:
        boot = self._boot
        if boot is None:
            return
        path = urlparse(response.url).path
        if path == "/v1/me":
            boot.me_status = response.status
        if response.status == 401 and path.startswith("/v1/"):
            boot.api_401_paths.append(path)

    def _cancel_login_hop(self, route: Route) -> None:
        # The shell cancels the off-origin IdP navigation and keeps the current
        # document (OmnigentWebView.decidePolicyFor); a 204 does the same here.
        boot = self._boot
        if boot is not None:
            boot.login_redirect_url = route.request.url
            try:
                real = route.fetch(max_redirects=0)
                boot.login_redirect_status = real.status
                boot.login_redirect_location = real.headers.get("location")
            except PlaywrightError as exc:
                boot.login_redirect_error = str(exc)
        route.fulfill(status=204)

    def open(self, origin: str, label: str, evidence_dir: Path) -> AppBoot:
        """Load the pinned server as the shell does on launch and after login."""
        boot = AppBoot(label=label)
        self._boot = boot
        self.context.route(f"{origin}/auth/login*", self._cancel_login_hop)
        try:
            self.page.goto(f"{origin}/")
            deadline = time.monotonic() + _BOOT_WINDOW_S
            while time.monotonic() < deadline:
                if boot.login_redirect_url is not None:
                    self.page.wait_for_timeout(3000)
                    break
                if boot.me_status == 200 and self.page.get_by_label("Message the agent").count():
                    self.page.wait_for_timeout(3000)
                    break
                self.page.wait_for_timeout(250)
            boot.final_url = self.page.url
            boot.opening_sign_in_visible = self.page.get_by_text("Opening sign-in…").count() > 0
            for cookie in self.context.cookies(origin):
                if cookie["name"] == "ap_session":
                    boot.session_cookie_present = True
                    # Playwright reports -1 (not absence) for a session-only cookie.
                    boot.session_cookie_expires = cookie.get("expires")
            self.page.screenshot(path=str(evidence_dir / f"{label}.png"))
        finally:
            # Always drop the route handler and freeze the snapshot, so a failure
            # mid-boot cannot leak the handler or let the listener mutate it.
            self.context.unroute(f"{origin}/auth/login*")
            self._boot = None
        return boot

    def close(self, clip_path: Path | None) -> None:
        """Terminate the browser process; the shell's force-quit stand-in."""
        video = self.page.video if clip_path is not None else None
        self.context.close()
        if video is not None:
            # shutil.move, not Path.rename: the record dir may be a different mount.
            shutil.move(video.path(), str(clip_path))


def _record_dir() -> Path | None:
    raw = os.environ.get(_RECORD_DIR_ENV)
    if not raw:
        return None
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slug(request: pytest.FixtureRequest) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name)


def _write_evidence(evidence_dir: Path, **entries: object) -> None:
    (evidence_dir / "journey.json").write_text(json.dumps(entries, indent=2, default=str) + "\n")


def _sign_in_like_the_shell(
    browser: Browser, webview: WebViewStandIn, origin: str, *, persistent_cookie: bool
) -> dict[str, object]:
    ticket, login_url = _request_ticket(origin)
    _complete_login_in_system_browser(browser, login_url)
    poll = _poll_for_token(origin, ticket)
    assert {"token", "user_id", "expires_in"} <= poll.keys(), poll
    expires_in = poll["expires_in"]
    assert isinstance(expires_in, int) and expires_in > 0, poll
    webview.context.add_cookies(
        [
            _shell_session_cookie(
                origin,
                str(poll["token"]),
                expires_in=expires_in if persistent_cookie else None,
            )
        ]
    )
    return poll


@dataclass(frozen=True)
class ColdStartJourney:
    before: AppBoot
    signed_in: AppBoot
    relaunch: AppBoot


def _sign_in_then_relaunch(
    playwright: Playwright,
    browser: Browser,
    origin: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
    *,
    persistent_cookie: bool,
) -> ColdStartJourney:
    """Open signed out, complete the ticket login, force-quit, reopen."""
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    record_dir = _record_dir()
    slug = _slug(request)
    profile_dir = tmp_path / "webview-profile"
    webview = WebViewStandIn(playwright, profile_dir, record_dir)
    try:
        before = webview.open(origin, "before-sign-in", evidence_dir)
        poll = _sign_in_like_the_shell(
            browser, webview, origin, persistent_cookie=persistent_cookie
        )
        signed_in = webview.open(origin, "signed-in", evidence_dir)
    finally:
        webview.close(record_dir / f"{slug}-1-signed-in.webm" if record_dir else None)

    webview = WebViewStandIn(playwright, profile_dir, record_dir)
    try:
        relaunch = webview.open(origin, "relaunch", evidence_dir)
    finally:
        webview.close(record_dir / f"{slug}-2-relaunch.webm" if record_dir else None)

    _write_evidence(
        evidence_dir,
        persistent_cookie=persistent_cookie,
        before=asdict(before),
        cli_poll=_redacted(poll),
        signed_in=asdict(signed_in),
        relaunch=asdict(relaunch),
    )
    assert before.me_status == 401 and before.login_redirect_url is not None
    assert {"token", "user_id", "expires_in"} <= poll.keys()
    assert poll["user_id"] == _IDP_EMAIL
    assert signed_in.me_status == 200 and signed_in.session_cookie_present
    assert signed_in.login_redirect_url is None and not signed_in.opening_sign_in_visible
    return ColdStartJourney(before=before, signed_in=signed_in, relaunch=relaunch)


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_shell_session_cookie_survives_cold_start(
    playwright: Playwright,
    browser: Browser,
    oidc_server: OidcServer,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """The shell's cookie carries the server's expiry, so a relaunch stays signed in."""
    journey = _sign_in_then_relaunch(
        playwright, browser, oidc_server.origin, tmp_path, request, persistent_cookie=True
    )
    relaunch = journey.relaunch
    assert relaunch.session_cookie_present, (
        f"session cookie gone after relaunch; /v1/me -> {relaunch.me_status}, "
        f"login redirect -> {relaunch.login_redirect_url}"
    )
    assert relaunch.session_cookie_expires is not None
    assert relaunch.session_cookie_expires > time.time(), (
        f"surviving cookie is not persistent; expires -> {relaunch.session_cookie_expires}"
    )
    assert relaunch.me_status == 200
    assert relaunch.api_401_paths == []
    assert relaunch.login_redirect_url is None
    assert not relaunch.opening_sign_in_visible


def test_session_only_cookie_is_lost_on_cold_start(
    playwright: Playwright,
    browser: Browser,
    oidc_server: OidcServer,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """Control: the same cookie without an expiry is dropped with the process.

    Keeps the stand-in honest: if Chromium kept session-only cookies across a
    relaunch, the test above would prove nothing.
    """
    journey = _sign_in_then_relaunch(
        playwright, browser, oidc_server.origin, tmp_path, request, persistent_cookie=False
    )
    relaunch = journey.relaunch
    assert not relaunch.session_cookie_present
    assert relaunch.me_status == 401
    assert relaunch.login_redirect_url is not None
