"""Browser coverage for the desktop OIDC handoff status window.

The Electron main process opens the system browser and owns ticket polling;
this shell page keeps that wait cancellable and makes a failed attempt
retryable. Playwright drives the shipped HTML with the isolated preload API
stubbed before its script runs, exercising the actual user-visible controls.
"""

import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse

from playwright.sync_api import Page, expect

_OIDC_LOGIN_PAGE = (
    Path(__file__).resolve().parents[3] / "web" / "electron" / "src" / "oidc_login.html"
)
_ELECTRON_DIR = _OIDC_LOGIN_PAGE.parents[1]

_PRELOAD_STUB = """
window.__oidc = { calls: [], listener: null };
window.omnigentOidcLogin = {
  onState: (listener) => {
    window.__oidc.listener = listener;
    return () => { window.__oidc.listener = null; };
  },
  cancel: () => window.__oidc.calls.push("cancel"),
  retry: () => window.__oidc.calls.push("retry"),
};
"""

_TICKET_CLIENT = """
(async () => {
  const { runOidcBrowserLogin } = require("./src/oidc_auth");
  const opened = [];
  const statuses = [];
  const result = await runOidcBrowserLogin(
    { fetch },
    process.env.OIDC_STUB_URL,
    async (url) => { opened.push(url); await fetch(url); },
    { pollIntervalMs: 1, timeoutMs: 1000, onPollError: (status) => statuses.push(status) },
  );
  console.log(JSON.stringify({ result, opened, statuses }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""


def _run_ticket_flow(statuses: list[int]) -> dict[str, object]:
    pending = list(statuses)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/auth/cli-login":
                self.send_error(404)
                return
            self._json(200, {"ticket": "one-time", "login_url": "/auth/login?ticket=one-time"})

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/auth/login":
                self._json(200, {"completed": True})
                return
            if path != "/auth/cli-poll":
                self.send_error(404)
                return
            status = pending.pop(0) if pending else 202
            self._json(status, {"token": "session-token"} if status == 200 else {})

        def _json(self, status: int, body: dict[str, object]) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {**os.environ, "OIDC_STUB_URL": f"http://127.0.0.1:{server.server_port}"}
    try:
        result = subprocess.run(
            ["node", "--eval", _TICKET_CLIENT],
            cwd=_ELECTRON_DIR,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    return json.loads(result.stdout)


def test_oidc_handoff_wait_error_retry_and_cancel(page: Page) -> None:
    """The browser handoff clearly reports progress and offers safe recovery."""
    page.add_init_script(_PRELOAD_STUB)
    page.goto(_OIDC_LOGIN_PAGE.as_uri())

    page.evaluate(
        """() => window.__oidc.listener({
          phase: "waiting",
          message: "Waiting for sign-in…",
          host: "accounts.example.com",
        })"""
    )
    expect(page.get_by_role("heading")).to_have_text("Continue sign-in in your browser")
    expect(page.locator("#message")).to_have_text("Waiting for sign-in…")
    expect(page.locator("#host")).to_have_text("accounts.example.com")
    expect(page.get_by_role("button", name="Retry")).to_be_hidden()

    page.evaluate(
        """() => window.__oidc.listener({
          phase: "error",
          message: "The sign-in ticket expired.",
          host: "accounts.example.com",
        })"""
    )
    expect(page.get_by_role("heading")).to_have_text("Sign-in did not finish")
    expect(page.locator("#message")).to_have_text("The sign-in ticket expired.")

    page.get_by_role("button", name="Retry").click()
    page.get_by_role("button", name="Cancel").click()
    assert page.evaluate("() => window.__oidc.calls") == ["retry", "cancel"]


def test_oidc_ticket_flow_retries_transient_statuses_against_local_server() -> None:
    """The shipped client completes after the contract's retryable status matrix."""
    result = _run_ticket_flow([429, 502, 503, 504, 200])

    assert result["result"] == {"ok": True, "token": "session-token"}
    assert result["statuses"] == [429, 502, 503, 504]
    assert result["opened"][0].endswith("/auth/login?ticket=one-time")


def test_oidc_ticket_flow_fails_authorization_statuses_against_local_server() -> None:
    """401 and 403 are fatal contract responses rather than transient polling errors."""
    for status in (401, 403):
        result = _run_ticket_flow([status])
        assert result["result"] == {"ok": False, "reason": "failed"}
        assert result["statuses"] == []
