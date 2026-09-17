"""E2E: opaque custom-theme sidebar must keep the dark palette in the embed.

Reproduces a reported regression: with a
custom color theme active in dark mode, disabling **Translucent sidebars**
renders the conversations sidebar near-white while the main pane stays dark.
Enabling the toggle switches the sidebar to the expected dark palette;
disabling it brings the near-white panel back.

The failure is specific to the **embed island** (``web/src/embed.tsx``) — the
build the Omnigent Desktop shell shows when it points at a workspace-hosted
server, where the SPA mounts inside a host page. The embed's scoped stylesheet
(``web/vite.embed.config.ts``) rewrites ``:root`` → ``.omnigent-app`` and
``.dark`` → ``.omnigent-app .dark``, so the ``.dark`` class lives on an INNER
div while the ``--custom-*`` variables land on the OUTER scope root, which
never carries ``.dark``. ``rebaseVariant()`` (``web/src/lib/customTheme.ts``)
keeps ``sidebarBackground`` as the palette's base value — for every base but
omni the literal ``var(--sidebar)`` — so ``--custom-dark-sidebar-background: var(--sidebar)``
substitutes on the scope root against the scope root's LIGHT ``--sidebar``
(matched by ``.omnigent-app:not(.dark)[data-theme=custom]``) and inherits down
as a light color. The opaque dark rule ``.dark[data-theme=custom]
.conversations-sidebar { background: var(--custom-dark-sidebar-background) }``
then paints the sidebar near-white; the translucent rule uses the correctly
derived ``--custom-dark-sidebar`` literal and stays dark. The standalone SPA
(everything on ``<html>``) resolves the same var chain against the dark tokens
and does not exhibit the bug.

The test drives the REAL user journey on the real artifacts: it builds the
actual embed island (``vite build --config vite.embed.config.ts``), wraps it in
a minimal host page (host-owned React + react-router, host-driven dark mode —
what the workspace monolith's bundler does), serves the host page same-origin
over the live e2e server via Playwright route interception, and then walks the
reported steps: open Settings → Appearance in dark mode → apply a custom theme
with a dark sidebar (tune the Dracula preset, deriving a Custom theme based on
it) → observe the opaque sidebar → toggle Translucent sidebars on → observe →
toggle it off → observe. It asserts the CORRECT behavior — the opaque sidebar keeps the dark
palette — so it FAILS while the bug is live and guards the fix afterwards.

No LLM turn is involved.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import filelock
import pytest
from playwright.sync_api import Locator, Page, Route, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WEB_DIR = _REPO_ROOT / "web"
_DIST_EMBED = _WEB_DIR / "dist-embed"
_HOST_DIR = _DIST_EMBED / "e2e-host"
_HOST_DIST = _HOST_DIR / "dist"
_VITE = _WEB_DIR / "node_modules" / ".bin" / "vite"

# The path prefix the host page's own assets are served under (same-origin with
# the live server, fulfilled from disk by the route handler below).
_HOST_BASE = "/embed-host/"

_HOST_INDEX_HTML = """\
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Host app (embed harness)</title>
    <style>
      /* Minimal host chrome: a dark host page, like the desktop shell in dark mode. */
      html, body { height: 100%; margin: 0; background: #1f272e; }
      #host-root { height: 100vh; width: 100vw; }
    </style>
  </head>
  <body>
    <div id="host-root"></div>
    <script type="module" src="./entry.js"></script>
  </body>
</html>
"""

_HOST_ENTRY_JS = """\
// Minimal host: render the embed island (OmnigentApp) the way the workspace
// monolith does - host-owned React + react-router, host-driven dark mode.
import React from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { OmnigentApp } from "../omnigent-embed.js";
import "../omnigent-embed.css";

createRoot(document.getElementById("host-root")).render(
  React.createElement(
    BrowserRouter,
    null,
    React.createElement(OmnigentApp, { isDarkMode: true }),
  ),
);
"""

_HOST_VITE_CONFIG = """\
// Host-wrapper build: bundles entry.js (host React + router + the embed
// island) into a self-contained page, standing in for the monolith's bundler
// ingest of the embed intermediate. Bare deps resolve from web/node_modules by
// walking up from this directory; "react-router" (a transitive dep of
// react-router-dom, not hoisted by pnpm) is aliased to the copy
// react-router-dom itself resolves, so the island and the host share one
// router instance exactly like the monolith build.
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.resolve(here, "../..");
const requireFromWeb = createRequire(path.join(webDir, "package.json"));
const requireFromRrd = createRequire(requireFromWeb.resolve("react-router-dom/package.json"));
const reactRouterDir = path.dirname(requireFromRrd.resolve("react-router/package.json"));

export default {
  base: "/embed-host/",
  define: { "process.env.NODE_ENV": '"production"' },
  resolve: {
    alias: { "react-router": reactRouterDir },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    chunkSizeWarningLimit: 10000,
  },
};
"""


@pytest.fixture(scope="session")
def embed_host_dist() -> Path:
    """Build the embed island + a minimal host page bundle; return the host dist dir.

    Two builds, serialized under a cross-process lock (concurrent pytest
    shards would clobber each other's ``dist-embed``):

    1. the REAL embed island — ``vite build --config vite.embed.config.ts`` —
       exactly the artifact the workspace monolith ingests (scoped CSS, bare
       React externals);
    2. a tiny host wrapper that provides React/react-router and mounts
       ``OmnigentApp`` in dark mode, bundled into ``dist-embed/e2e-host/dist``.

    The embed build must run first: its ``emptyOutDir`` wipes ``dist-embed/``.
    """
    lock_path = _WEB_DIR / ".build-embed.lock"
    with filelock.FileLock(str(lock_path), timeout=600):
        subprocess.run(
            [str(_VITE), "build", "--config", "vite.embed.config.ts"],
            cwd=_WEB_DIR,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        _HOST_DIR.mkdir(parents=True, exist_ok=True)
        (_HOST_DIR / "index.html").write_text(_HOST_INDEX_HTML)
        (_HOST_DIR / "entry.js").write_text(_HOST_ENTRY_JS)
        (_HOST_DIR / "vite.config.mjs").write_text(_HOST_VITE_CONFIG)
        subprocess.run(
            [
                str(_VITE),
                "build",
                "--config",
                str(_HOST_DIR / "vite.config.mjs"),
                str(_HOST_DIR),
            ],
            cwd=_WEB_DIR,
            check=True,
            stdin=subprocess.DEVNULL,
        )
    assert (_HOST_DIST / "index.html").is_file(), "host wrapper build produced no index.html"
    return _HOST_DIST


def _install_embed_host(page: Page, host_dist: Path) -> None:
    """Serve the embed-host page same-origin over the live server.

    Document navigations get the host page (so ANY app path — e.g.
    ``/settings/appearance`` — boots the embed island, which then routes on the
    real pathname, like the monolith mount does); ``/embed-host/*`` asset
    requests are fulfilled from the host build; everything else (``/v1/*`` API
    calls, websockets are never intercepted) passes through to the real server.
    """

    def _serve(route: Route) -> None:
        request = route.request
        path = urlparse(request.url).path
        if path.startswith(_HOST_BASE):
            asset = host_dist / path[len(_HOST_BASE) :]
            if asset.is_file():
                route.fulfill(path=str(asset))
            else:
                route.fulfill(status=404, body="not found")
        elif request.resource_type == "document":
            route.fulfill(path=str(host_dist / "index.html"))
        else:
            route.fallback()

    page.route("**/*", _serve)


def _parse_css_color(value: str) -> tuple[int, int, int, float]:
    """Parse ``rgb(...)`` / ``rgba(...)`` computed colors into (r, g, b, alpha)."""
    match = re.match(
        r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)",
        value,
    )
    assert match, f"unexpected computed color {value!r}"
    red, green, blue = (round(float(match.group(i))) for i in (1, 2, 3))
    alpha = float(match.group(4)) if match.group(4) else 1.0
    return red, green, blue, alpha


def _luminance(value: str) -> float:
    """WCAG relative luminance of a computed CSS color (alpha ignored)."""

    def linear(channel: int) -> float:
        scaled = channel / 255
        return scaled / 12.92 if scaled <= 0.04045 else ((scaled + 0.055) / 1.055) ** 2.4

    red, green, blue, _alpha = _parse_css_color(value)
    return 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue)


def _sidebar_background(sidebar: Locator) -> str:
    return sidebar.evaluate("el => getComputedStyle(el).backgroundColor")


# The dark custom palette's sidebar is near-black (luminance ~0.01); the buggy
# resolution paints it near-white (~0.94). Anything above this threshold is a
# light panel a dark-mode user would see as broken.
_DARK_LUMINANCE_MAX = 0.35


def test_embed_opaque_custom_sidebar_keeps_dark_palette(
    page: Page, live_server: str, embed_host_dist: Path
) -> None:
    """Opaque and translucent custom sidebars must both use the dark palette.

    Journey (mirrors the report): dark embed → Settings → Appearance → apply a
    custom theme with a dark sidebar (tune the Dracula preset into a Custom
    theme) → opaque sidebar (Translucent sidebars OFF, the custom default) →
    toggle ON → toggle OFF. The setting must only change opacity/material,
    never flip the sidebar to the light palette.
    """
    _install_embed_host(page, embed_host_dist)

    page.goto(f"{live_server}/settings/appearance")

    # The embed island booted: outer scope root + inner host-driven dark root.
    scope_root = page.locator("div.omnigent-app")
    expect(scope_root).to_be_visible(timeout=30_000)
    expect(page.locator("div.omnigent-app > div.dark")).to_be_attached()

    # Apply a custom theme with a dark sidebar: pick the Dracula preset, then
    # nudge the contrast slider — tuning any control derives a Custom theme
    # based on that palette (``basePalette: "dracula"``), whose
    # ``sidebarBackground`` is the palette-tokens default ``var(--sidebar)``
    # (the omni base overrides it with concrete gradients, which sidesteps the
    # bug — the report's failing rgba values come from a non-omni base).
    # Translucent sidebars starts OFF: the opaque failing state from the report.
    palette_select = page.get_by_test_id("color-theme-select")
    expect(palette_select).to_be_visible(timeout=30_000)
    palette_select.click()
    page.get_by_test_id("palette-dracula").click()
    expect(scope_root).to_have_attribute("data-theme", "dracula")
    contrast = page.get_by_test_id("custom-theme-contrast")
    contrast.focus()
    contrast.press("ArrowRight")
    expect(scope_root).to_have_attribute("data-theme", "custom")

    translucent_toggle = page.get_by_test_id("custom-theme-translucent-sidebar")
    expect(translucent_toggle).to_be_visible()
    expect(translucent_toggle).to_have_attribute("aria-checked", "false")

    sidebar = page.locator("aside.conversations-sidebar")
    expect(sidebar).to_be_visible()

    # 1. Opaque (Translucent sidebars OFF — the reported failing state).
    opaque_background = _sidebar_background(sidebar)

    # 2. Translucent ON — the report's known-good contrast state.
    translucent_toggle.click()
    expect(scope_root).to_have_attribute("data-custom-translucent-sidebar", "")
    translucent_background = _sidebar_background(sidebar)

    # 3. Opaque again — the report re-disables the toggle and the light panel returns.
    translucent_toggle.click()
    expect(scope_root).not_to_have_attribute("data-custom-translucent-sidebar", "")
    opaque_background_again = _sidebar_background(sidebar)

    # The translucent path resolves the derived dark sidebar token — dark today
    # and after any fix. It anchors what "the same dark palette" means.
    assert _luminance(translucent_background) < _DARK_LUMINANCE_MAX, (
        f"translucent custom sidebar is not dark in dark mode: "
        f"{translucent_background} (luminance {_luminance(translucent_background):.3f})"
    )

    # THE BUG: with Translucent sidebars OFF the sidebar must stay on the
    # dark palette, not flip to a near-white panel.
    assert _luminance(opaque_background) < _DARK_LUMINANCE_MAX, (
        f"opaque custom sidebar uses the light palette in dark mode: "
        f"background {opaque_background} (luminance {_luminance(opaque_background):.3f}) "
        f"while the translucent state is dark ({translucent_background})"
    )
    assert _luminance(opaque_background_again) < _DARK_LUMINANCE_MAX, (
        f"re-disabling Translucent sidebars flips the sidebar back to the light "
        f"palette: {opaque_background_again} "
        f"(luminance {_luminance(opaque_background_again):.3f})"
    )
