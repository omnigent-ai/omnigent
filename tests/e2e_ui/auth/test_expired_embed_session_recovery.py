"""E2E: an expired embedded user session must recover, not wedge the app.

Omnigent embedded in a host application (the Databricks monolith) routes every
API call through the host's ``fetcher`` (``workspaceFetch``). Once the host
user session expires, that fetcher REJECTS with
``Error("Fetch request failed due to expired user session")`` instead of
returning a ``Response``. The web app surfaced that rejection raw -- query
surfaces (e.g. the sidebar session list) rendered
"Failed to load: Fetch request failed due expired user session" and the app
stayed wedged until the user manually reloaded the page.

Expected behavior: the embed reloads the current URL once so the host can
renew its session (or show its sign-in flow), after which the app recovers.

The Databricks monolith itself cannot be driven in this harness, so the test
mounts the REAL embed island (``web/src/embed.tsx`` -- ``OmnigentApp`` +
``setOmnigentHostConfig``) inside a minimal stand-in host page, per the same
embed contract the monolith uses: the host owns the page, the React tree, the
router, and the transport ``fetcher``. The harness fetcher same-origin-proxies
to the spawned live server and can be flipped into the expired state, from
which every request rejects with the exact message ``workspaceFetch`` produces.
A fresh page load models the host renewing its session (the monolith
re-authenticates on navigation), so a recovery reload un-wedges the app while
an unfixed build stays stuck on the raw error forever.

Part of the gated e2e suite (needs ``pnpm`` + a vite build); see this
package's ``conftest`` module docstring for how the suite is run.
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
# Lives under web/ so the harness page's bare imports, Tailwind source
# scanning, and the `@` alias resolve through web/node_modules exactly like
# the production build. Generated + built by the fixture below; never
# committed (only `dist`-like build output lands here).
_HARNESS_DIR = _WEB_DIR / ".e2e-embed-host"

# The exact rejection the Databricks monolith's workspaceFetch produces once
# the workspace user session has expired (see web/README.md, "Embedded
# session recovery"). User reports carry the same message minus the "to",
# so the assertion pattern tolerates both spellings.
_EXPIRED_MESSAGE = "Fetch request failed due to expired user session"
_EXPIRED_PATTERN = re.compile(r"Fetch request failed due (?:to )?expired user session")

# Same-origin paths the harness server forwards to the spawned omnigent
# server -- the set the SPA's own dev proxy forwards (web/vite.config.ts).
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
    """Honor ``OMNIGENT_E2E_RECORD_DIR`` for the sync ``page`` fixture.

    The conftest's ``_record_video`` monkeypatch only covers tests that drive
    the *async* Playwright API themselves; this test uses pytest-playwright's
    sync ``page`` fixture, whose context is built from these args.
    """
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        return {**browser_context_args, "record_video_dir": record_dir}
    return browser_context_args


@pytest.fixture(scope="session")
def embed_host_dist(built_spa: None) -> Path:
    """Write the embed-host harness sources under ``web/`` and vite-build them.

    :param built_spa: Depended on to guarantee the web toolchain is installed
        (``pnpm install``); the harness build does not use its output.
    :returns: The harness ``dist`` directory (assets at its root, the page at
        ``.e2e-embed-host/index.html``).
    """
    _HARNESS_DIR.mkdir(exist_ok=True)
    (_HARNESS_DIR / "index.html").write_text(_INDEX_HTML)
    (_HARNESS_DIR / "host-entry.tsx").write_text(_HOST_ENTRY_TSX)
    (_HARNESS_DIR / "vite.config.mjs").write_text(_VITE_CONFIG_MJS)
    env = {**os.environ, "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0"}
    # Reuse the SPA build lock: concurrent pytest sessions in the same
    # worktree would otherwise clobber each other's emptyOutDir.
    with filelock.FileLock(str(_WEB_DIR / ".build.lock"), timeout=600):
        # Invoke the vite binary directly: `pnpm exec` runs a deps-status
        # check that can kick off a TTY-gated `pnpm install` and abort under
        # captured pytest output.
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
    """Static server + API proxy standing in for the monolith's page origin.

    Serves the built harness page (SPA fallback for extension-less paths) and
    forwards :data:`_PROXY_PREFIXES` requests to the spawned omnigent server,
    so the embed's host fetcher stays same-origin exactly like
    ``workspaceFetch`` (the omnigent server itself sends no CORS headers).
    """

    protocol_version = "HTTP/1.1"
    # Injected onto the subclass by the fixture:
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
            # WebSockets go directly to the upstream (see resolveWebSocketUrl
            # in the harness page); this HTTP-only proxy can't carry them.
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
            # httpx transparently decompressed the payload, so the encoding
            # and length headers no longer describe it; hop-by-hop headers
            # don't survive proxying at all.
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
    """Serve the built harness page on a random loopback port.

    :returns: The harness origin, e.g. ``http://127.0.0.1:54321``.
    """
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
    """An expired host session must trigger recovery, not a permanent raw error.

    Journey (mirrors the user report): use embedded Omnigent normally,
    let the host user session expire behind the tab, then interact again. The
    unfixed app wedges on
    "Failed to load: Fetch request failed due expired user session" until a
    manual reload; the fixed app reloads itself once so the host renews the
    session, and the UI comes back healthy.
    """
    _, session_id = seeded_session

    # --- the app works while the host session is alive -------------------
    page.goto(f"{embed_host_url}/")
    sidebar = page.get_by_test_id("sidebar-conversation-list")
    expect(sidebar).to_be_visible(timeout=30_000)
    # The seeded session's row proves real data flowed through the host
    # fetcher (not just static chrome); match by its /c/<id> link target,
    # which is stable regardless of the row's display title.
    expect(sidebar.locator(f'a[href="/c/{session_id}"]')).to_be_visible(timeout=15_000)

    # --- the host user session expires behind the tab --------------------
    page.evaluate("window.__omnigentEmbedHarness.expire()")

    # --- the next interaction loads data through the expired fetcher -----
    # Switching the sidebar filter fires a fresh (uncached) session-list
    # query; on the unfixed build its rejection surfaces raw in the sidebar
    # ("Failed to load: ..." -- Sidebar's conversationsQuery error branch).
    # On a fixed build a background poll (e.g. runner health) may already
    # reject and trigger the recovery reload mid-click; that navigation makes
    # a click throw, which is the recovery itself -- fall through to the
    # verdict loop rather than failing the trigger.
    try:
        session_filter = page.get_by_test_id("session-filter")
        session_filter.hover()
        session_filter.click()
        page.get_by_test_id("session-filter-archived").click()
    except PlaywrightError:
        pass

    # --- verdict: recovery reload vs. permanent wedge --------------------
    # Fixed: the embed reloads the page once (hostFetch's expired-session
    # recovery); a fresh page load renews the harness host session, so the
    # in-memory `expired` flag reads False again. Buggy: no reload ever
    # happens -- the flag stays True and the raw message sits on screen.
    deadline = time.monotonic() + 45.0
    recovered = False
    error_visible_since: float | None = None
    while time.monotonic() < deadline:
        # Cooperative wait so Playwright keeps servicing page events.
        page.wait_for_timeout(250)
        try:
            still_expired = page.evaluate(
                "window.__omnigentEmbedHarness && window.__omnigentEmbedHarness.expired()"
            )
        except PlaywrightError:
            continue  # navigation in flight -- the recovery reload
        if still_expired is False:
            recovered = True
            break
        if page.get_by_text(_EXPIRED_PATTERN).first.is_visible():
            error_visible_since = error_visible_since or time.monotonic()
            if time.monotonic() - error_visible_since > 8.0:
                break  # wedged: raw error on screen, no recovery reload

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

    # Fixed behavior, second half: after the recovery reload the renewed
    # session must actually restore the app -- sidebar back, raw error gone.
    expect(page.get_by_test_id("sidebar-conversation-list")).to_be_visible(timeout=30_000)
    expect(page.get_by_text(_EXPIRED_PATTERN)).to_have_count(0)
