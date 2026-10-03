"""Browser journey for one-shot embedded session recovery.

Uses the real embed UI with a local host fetcher that can reject after expiry.
"""

from __future__ import annotations

import http.server
import mimetypes
import os
import re
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import filelock
import httpx
import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WEB_DIR = _REPO_ROOT / "web"
# Generated under web so imports and Tailwind resolve through its dependencies.
_HARNESS_DIR = _WEB_DIR / ".e2e-embed-host"

_EXPIRED_MESSAGE = "Fetch request failed due to expired user session"
_EXPIRED_PATTERN = re.compile(r"Fetch request failed due (?:to )?expired user session")

# Match the SPA development proxy surface.
_PROXY_PREFIXES = ("/v1", "/api", "/auth", "/health")

_INDEX_HTML = """\
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Omnigent embed host (e2e)</title>
    <!-- Replaced by the harness HTTP server with the spawned omnigent
         server's ws:// origin so the embed's WebSockets bypass the
         HTTP-only static proxy. -->
    <script>
      window.__OMNIGENT_E2E_WS_BASE__ = "%OMNIGENT_E2E_WS_BASE%";
    </script>
    <style>
      html,
      body,
      #host-root {
        height: 100%;
        margin: 0;
      }
    </style>
  </head>
  <body>
    <div id="host-root"></div>
    <script type="module" src="./host-entry.tsx"></script>
  </body>
</html>
"""

_HOST_ENTRY_TSX = """\
// Minimal stand-in for a host application embedding Omnigent (the Databricks
// monolith's `loadOmnigentEmbed` path): renders the real embed island
// (`OmnigentApp`) inside the host's own React tree + router and installs the
// host transport config, per the production embed contract.
//
// The `fetcher` plays the monolith's `workspaceFetch`: requests go same-origin
// (the harness HTTP server proxies /v1|/api|/auth|/health to the spawned
// omnigent server). Calling `expire()` flips it into the state the real
// workspaceFetch enters once the workspace user session lapses: every call
// REJECTS with `Error("Fetch request failed due to expired user session")`
// instead of returning a Response. A fresh page load models the host renewing
// its session (the monolith re-authenticates on navigation), so the expired
// flag resets on boot.
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { OmnigentApp, setOmnigentHostConfig, type OmnigentHostConfig } from "../src/embed";

let expired = false;
(window as unknown as Record<string, unknown>).__omnigentEmbedHarness = {
  expire: () => {
    expired = true;
  },
  expired: () => expired,
};

const wsBase = (window as unknown as { __OMNIGENT_E2E_WS_BASE__?: string })
  .__OMNIGENT_E2E_WS_BASE__;

const hostConfig: OmnigentHostConfig = {
  serverIdentity: "omnigent-e2e-embed-host",
  fetcher: async (path: string, init?: RequestInit): Promise<Response> => {
    if (expired) {
      throw new Error("Fetch request failed due to expired user session");
    }
    return fetch(path, init);
  },
  resolveWebSocketUrl: (path: string): string =>
    wsBase && wsBase.startsWith("ws")
      ? `${wsBase}${path}`
      : `ws://${window.location.host}${path}`,
};

// The production host installs the transport config eagerly (before first
// render) AND passes it as props to `OmnigentApp`; mirror both.
setOmnigentHostConfig(hostConfig);

const rootEl = document.getElementById("host-root");
if (!rootEl) throw new Error("host-root element missing");
createRoot(rootEl).render(
  <BrowserRouter>
    <OmnigentApp {...hostConfig} />
  </BrowserRouter>,
);
"""

_VITE_CONFIG_MJS = """\
// Build config for the e2e embed-host harness page (expired-embedded-session
// recovery test). Root is web/ so bare imports, Tailwind source scanning, and the `@`
// alias resolve exactly like the production build; the harness page is the
// only rollup input.
import path from "node:path";
import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const here = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(here, "..");

export default defineConfig({
  root: webRoot,
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(webRoot, "src"),
    },
  },
  build: {
    outDir: path.resolve(here, "dist"),
    emptyOutDir: true,
    rollupOptions: {
      input: path.resolve(here, "index.html"),
    },
    chunkSizeWarningLimit: 10_000,
  },
});
"""


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    """Honor ``OMNIGENT_E2E_RECORD_DIR`` for the synchronous page fixture."""
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        return {**browser_context_args, "record_video_dir": record_dir}
    return browser_context_args


@pytest.fixture(scope="session")
def embed_host_dist(built_spa: None) -> Path:
    """Build the local embed-host harness after the web toolchain is installed."""
    _HARNESS_DIR.mkdir(exist_ok=True)
    (_HARNESS_DIR / "index.html").write_text(_INDEX_HTML)
    (_HARNESS_DIR / "host-entry.tsx").write_text(_HOST_ENTRY_TSX)
    (_HARNESS_DIR / "vite.config.mjs").write_text(_VITE_CONFIG_MJS)
    env = {**os.environ, "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0"}
    # Share the SPA build lock because both builds empty their output directory.
    with filelock.FileLock(str(_WEB_DIR / ".build.lock"), timeout=600):
        # Avoid pnpm's interactive dependency check under captured pytest output.
        vite_bin = _WEB_DIR / "node_modules" / ".bin" / "vite"
        assert vite_bin.exists(), (
            f"{vite_bin} missing -- run `pnpm install --filter web` first "
            "(the built_spa fixture installs it unless --ui-skip-build was set "
            "on a worktree without JS dependencies)"
        )
        subprocess.run(
            [str(vite_bin), "build", "--config", str(_HARNESS_DIR / "vite.config.mjs")],
            cwd=_WEB_DIR,
            check=True,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    dist = _HARNESS_DIR / "dist"
    index = dist / ".e2e-embed-host" / "index.html"
    assert index.is_file(), (
        f"harness build produced no page at {index} -- vite's rollup input or "
        "output layout changed; adjust the fixture's expected path"
    )
    return dist


class _EmbedHostHandler(http.server.BaseHTTPRequestHandler):
    """Serve the harness and proxy its API calls to the local Omnigent server."""

    protocol_version = "HTTP/1.1"
    # Injected by the fixture's bound subclass.
    dist_root: Path
    index_html: bytes
    upstream: str

    def log_message(self, format: str, *args: Any) -> None:
        pass  # keep pytest output readable

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def _handle(self) -> None:
        path_only = self.path.split("?", 1)[0]
        try:
            if any(
                path_only == prefix or path_only.startswith(prefix + "/")
                for prefix in _PROXY_PREFIXES
            ):
                self._proxy()
            else:
                self._static(path_only)
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser closed mid-response (e.g. teardown) -- not a failure

    def _proxy(self) -> None:
        if "upgrade" in self.headers.get("Connection", "").lower():
            # The harness routes WebSockets directly to the upstream.
            self.send_error(501, "WebSocket upgrade not supported by e2e proxy")
            return
        body = None
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            body = self.rfile.read(length)
        fwd_headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower()
            not in ("host", "connection", "keep-alive", "transfer-encoding", "content-length")
        }
        try:
            upstream_resp = httpx.request(
                self.command,
                f"{self.upstream}{self.path}",
                headers=fwd_headers,
                content=body,
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            self.send_error(502, f"upstream request failed: {type(exc).__name__}")
            return
        payload = upstream_resp.content
        self.send_response(upstream_resp.status_code)
        for k, v in upstream_resp.headers.items():
            # httpx decompressed the payload, invalidating encoding and length.
            if k.lower() in (
                "content-length",
                "content-encoding",
                "transfer-encoding",
                "connection",
                "keep-alive",
            ):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _static(self, path_only: str) -> None:
        candidate = (self.dist_root / path_only.lstrip("/")).resolve()
        inside = str(candidate).startswith(str(self.dist_root.resolve()))
        if path_only != "/" and inside and candidate.is_file():
            payload = candidate.read_bytes()
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        elif "." not in path_only.rsplit("/", 1)[-1]:
            # SPA fallback: serve the harness page for route-like paths.
            payload = self.index_html
            content_type = "text/html; charset=utf-8"
        else:
            self.send_error(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)


class _ThreadingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


@pytest.fixture
def embed_host_url(embed_host_dist: Path, live_server: str) -> Iterator[str]:
    """Serve the built harness on a random loopback port."""
    ws_base = "ws://" + live_server.removeprefix("http://")
    index_html = (
        (embed_host_dist / ".e2e-embed-host" / "index.html")
        .read_text()
        .replace("%OMNIGENT_E2E_WS_BASE%", ws_base)
        .encode()
    )
    handler = type(
        "BoundEmbedHostHandler",
        (_EmbedHostHandler,),
        {"dist_root": embed_host_dist, "index_html": index_html, "upstream": live_server},
    )
    server = _ThreadingServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_expired_embedded_host_session_recovers(
    page: Page,
    seeded_session: tuple[str, str],
    embed_host_url: str,
) -> None:
    _, session_id = seeded_session

    page.goto(f"{embed_host_url}/")
    sidebar = page.get_by_test_id("sidebar-conversation-list")
    expect(sidebar).to_be_visible(timeout=30_000)
    # The seeded row proves data flowed through the host fetcher.
    expect(sidebar.locator(f'a[href="/c/{session_id}"]')).to_be_visible(timeout=15_000)

    page.evaluate("window.__omnigentEmbedHarness.expire()")

    # A background request may start recovery before the filter click completes.
    try:
        session_filter = page.get_by_test_id("session-filter")
        session_filter.hover()
        session_filter.click()
        page.get_by_test_id("session-filter-archived").click()
    except PlaywrightError:
        pass

    deadline = time.monotonic() + 45.0
    recovered = False
    error_visible_since: float | None = None
    while time.monotonic() < deadline:
        page.wait_for_timeout(250)
        try:
            still_expired = page.evaluate(
                "window.__omnigentEmbedHarness && window.__omnigentEmbedHarness.expired()"
            )
        except PlaywrightError:
            continue
        if still_expired is False:
            recovered = True
            break
        if page.get_by_text(_EXPIRED_PATTERN).first.is_visible():
            error_visible_since = error_visible_since or time.monotonic()
            if time.monotonic() - error_visible_since > 8.0:
                break

    if not recovered:
        if error_visible_since is not None:
            pytest.fail(
                "expired-session wedge reproduced: after the embedded host session expired, "
                f"the app surfaced the raw '{_EXPIRED_MESSAGE}' error and never "
                "recovered (no reload; the harness fetcher stayed expired). "
                "Expected the embed to reload the page once so the host renews "
                "its session."
            )
        pytest.fail(
            "the app neither recovered nor surfaced the expired-session error "
            "within 45s -- the filter switch may not have fired a session-list "
            "query; check the harness trigger"
        )

    expect(page.get_by_test_id("sidebar-conversation-list")).to_be_visible(timeout=30_000)
    expect(page.get_by_text(_EXPIRED_PATTERN)).to_have_count(0)
