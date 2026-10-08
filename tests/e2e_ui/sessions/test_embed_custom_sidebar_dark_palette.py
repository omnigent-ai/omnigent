"""E2E: an opaque custom-theme sidebar must keep the dark palette in the embed island.

Omnigent Desktop shows whatever page its server serves; pointed at a
workspace-hosted server that page is the embed island (``web/src/embed.tsx``),
where the ``--custom-*`` theme variables are set on the outer ``.omnigent-app``
scope root while the host-driven ``.dark`` class lives on an inner div. With a
custom color theme in dark mode and **Translucent sidebars** off, the
conversations sidebar paints near-white while the main pane stays dark; turning
the toggle on makes it dark, turning it off brings the light panel back.

Exercises the real embed island, with the standalone SPA (style root and dark root
both ``<html>``) as a control. No LLM turn is involved.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import filelock
import pytest
from playwright.sync_api import Locator, Page, Route, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WEB_DIR = _REPO_ROOT / "web"
# Own gitignored output so this build and `pnpm build:embed` never wipe each other.
_DIST_EMBED = _WEB_DIR / "dist-embed-e2e"
_HOST_DIR = _DIST_EMBED / "e2e-host"
_HOST_DIST = _HOST_DIR / "dist"
_VITE = _WEB_DIR / "node_modules" / ".bin" / "vite"

# Same-origin prefix for the host page's own assets (fulfilled from disk).
_HOST_BASE = "/embed-host/"

_HOST_INDEX_HTML = """\
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Host app (embed harness)</title>
    <style>
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
// pnpm does not hoist react-router (a dependency of react-router-dom), so alias
// it to the copy react-router-dom resolves; the subpath goes first because the
// bare directory alias bypasses the package export map.
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.resolve(here, "../..");
const requireFromWeb = createRequire(path.join(webDir, "package.json"));
const requireFromRrd = createRequire(requireFromWeb.resolve("react-router-dom/package.json"));
const reactRouterDir = path.dirname(requireFromRrd.resolve("react-router/package.json"));
const reactRouterDom = requireFromRrd.resolve("react-router/dom");

export default {
  base: "/embed-host/",
  define: { "process.env.NODE_ENV": '"production"' },
  resolve: {
    alias: { "react-router/dom": reactRouterDom, "react-router": reactRouterDir },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    chunkSizeWarningLimit: 10000,
  },
};
"""

# The dark custom sidebar is near-black (luminance ~0.02); the light palette's
# sidebar is near-white (~0.88). Anything above this reads as a light panel.
_DARK_LUMINANCE_MAX = 0.35

# Dracula's dark sidebar is rgba(..., 0.8) and translucency lowers it to 0.72. A
# transparent fallback (alpha 0) is dark by luminance, so alpha is checked too.
_OPAQUE_ALPHA = 0.8
_TRANSLUCENT_ALPHA = 0.72

# Written after a complete build so sibling xdist workers of the same run reuse
# it instead of wiping the output while another worker is still serving it.
_BUILD_STAMP = _HOST_DIR / ".built-by-run"


def _build_embed_host() -> None:
    if not _VITE.is_file():
        raise RuntimeError(f"{_VITE} is missing; run `pnpm install --filter web` first")
    subprocess.run(
        [str(_VITE), "build", "--config", "vite.embed.config.ts", "--outDir", str(_DIST_EMBED)],
        cwd=_WEB_DIR,
        check=True,
        stdin=subprocess.DEVNULL,
    )
    _HOST_DIR.mkdir(parents=True, exist_ok=True)
    (_HOST_DIR / "index.html").write_text(_HOST_INDEX_HTML)
    (_HOST_DIR / "entry.js").write_text(_HOST_ENTRY_JS)
    (_HOST_DIR / "vite.config.mjs").write_text(_HOST_VITE_CONFIG)
    subprocess.run(
        [str(_VITE), "build", "--config", str(_HOST_DIR / "vite.config.mjs"), str(_HOST_DIR)],
        cwd=_WEB_DIR,
        check=True,
        stdin=subprocess.DEVNULL,
    )


@pytest.fixture(scope="session")
def embed_host_dist(request: pytest.FixtureRequest) -> Path:
    """Build the embed island and the host page bundle; return the host dist dir.

    ``--ui-skip-build`` reuses an existing host build, mirroring the SPA option.
    The embed build runs first because its ``emptyOutDir`` wipes the output dir.
    """
    if request.config.getoption("--ui-skip-build") and (_HOST_DIST / "index.html").is_file():
        return _HOST_DIST
    run_id = os.environ.get("PYTEST_XDIST_TESTRUNUID", "")
    with filelock.FileLock(str(_WEB_DIR / ".build-embed.lock"), timeout=900):
        if not (run_id and _BUILD_STAMP.is_file() and _BUILD_STAMP.read_text() == run_id):
            _build_embed_host()
            if run_id:
                _BUILD_STAMP.write_text(run_id)
    assert (_HOST_DIST / "index.html").is_file(), "host page build produced no index.html"
    return _HOST_DIST


def install_embed_host(page: Page, host_dist: Path) -> None:
    """Serve the embed host page same-origin over the live server.

    Document navigations get the host page, so any app path boots the island,
    which then routes on the real pathname like the monolith mount does.
    ``/embed-host/*`` assets come from the host build; everything else passes
    through to the real server.
    """

    def _serve(route: Route) -> None:
        request = route.request
        path = urlparse(request.url).path
        if path.startswith(_HOST_BASE):
            asset = (host_dist / path[len(_HOST_BASE) :].lstrip("/")).resolve()
            if asset.is_relative_to(host_dist.resolve()) and asset.is_file():
                route.fulfill(path=str(asset))
            else:
                route.fulfill(status=404, body="not found")
        elif request.resource_type == "document":
            route.fulfill(path=str(host_dist / "index.html"))
        else:
            route.fallback()

    page.route("**/*", _serve)


def parse_css_color(value: str) -> tuple[int, int, int, float]:
    """Parse ``rgb()`` / ``rgba()`` / ``#rrggbb`` colors into (r, g, b, alpha)."""
    value = value.strip()
    if hex_match := re.fullmatch(r"#([0-9a-fA-F]{6})", value):
        digits = hex_match.group(1)
        return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16), 1.0
    match = re.fullmatch(
        r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)", value
    )
    assert match, f"unexpected computed color {value!r}"
    red, green, blue = (round(float(match.group(i))) for i in (1, 2, 3))
    alpha = float(match.group(4)) if match.group(4) else 1.0
    return red, green, blue, alpha


def luminance(value: str) -> float:
    """WCAG relative luminance of a CSS color (alpha ignored)."""

    def linear(channel: int) -> float:
        scaled = channel / 255
        return scaled / 12.92 if scaled <= 0.04045 else ((scaled + 0.055) / 1.055) ** 2.4

    red, green, blue, _alpha = parse_css_color(value)
    return 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue)


def alpha(value: str) -> float:
    return parse_css_color(value)[3]


def background_color(element: Locator) -> str:
    return element.evaluate("el => getComputedStyle(el).backgroundColor")


def apply_dracula_custom_theme(page: Page, style_root: Locator) -> None:
    """Pick the Dracula preset, then tune it so the selection becomes Custom.

    Every non-omni preset leaves ``sidebarBackground`` at the palette default
    ``var(--sidebar)``, which is the value the report's failing state resolves.
    """
    palette_select = page.get_by_test_id("color-theme-select")
    expect(palette_select).to_be_visible(timeout=30_000)
    palette_select.click()
    page.get_by_test_id("palette-dracula").click()
    expect(style_root).to_have_attribute("data-theme", "dracula")
    contrast = page.get_by_test_id("custom-theme-contrast")
    contrast.focus()
    contrast.press("ArrowRight")
    expect(style_root).to_have_attribute("data-theme", "custom")
    expect(palette_select).to_contain_text("Custom")


def drive_translucency_journey(page: Page, style_root: Locator) -> dict[str, str]:
    """Report the sidebar's computed background for opaque -> translucent -> opaque."""
    toggle = page.get_by_test_id("custom-theme-translucent-sidebar")
    expect(toggle).to_be_visible()
    expect(toggle).to_have_attribute("aria-checked", "false")
    sidebar = page.locator("aside.conversations-sidebar")
    expect(sidebar).to_be_visible()

    backgrounds = {"opaque": background_color(sidebar)}

    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "true")
    expect(style_root).to_have_attribute("data-custom-translucent-sidebar", "")
    backgrounds["translucent"] = background_color(sidebar)

    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "false")
    expect(style_root).not_to_have_attribute("data-custom-translucent-sidebar", "")
    backgrounds["opaque_again"] = background_color(sidebar)
    return backgrounds


def assert_sidebar_stays_dark(backgrounds: dict[str, str], pane_background: str) -> None:
    """Both opaque states must share the dark palette the pane and translucent state use."""
    assert luminance(pane_background) < _DARK_LUMINANCE_MAX, (
        f"main pane is not dark in dark mode: {pane_background}"
    )
    translucent = backgrounds["translucent"]
    assert luminance(translucent) < _DARK_LUMINANCE_MAX, (
        f"translucent custom sidebar is not dark in dark mode: {translucent} "
        f"(luminance {luminance(translucent):.3f})"
    )
    for state in ("opaque", "opaque_again"):
        value = backgrounds[state]
        assert luminance(value) < _DARK_LUMINANCE_MAX, (
            f"with Translucent sidebars off ({state}) the custom sidebar uses the light "
            f"palette in dark mode: {value} (luminance {luminance(value):.3f}) while the "
            f"translucent state is dark ({translucent}) and the pane is {pane_background}"
        )
    expected_alpha = {
        "opaque": _OPAQUE_ALPHA,
        "translucent": _TRANSLUCENT_ALPHA,
        "opaque_again": _OPAQUE_ALPHA,
    }
    for state, expected in expected_alpha.items():
        value = backgrounds[state]
        assert math.isclose(alpha(value), expected, abs_tol=0.01), (
            f"{state} custom sidebar is {value}, expected alpha {expected}: a transparent or "
            f"unresolved background is not the dark palette"
        )


def test_embed_opaque_custom_sidebar_keeps_dark_palette(
    page: Page, live_server: str, embed_host_dist: Path
) -> None:
    """Embedded, dark host: opaque and translucent custom sidebars both stay dark."""
    install_embed_host(page, embed_host_dist)
    page.goto(f"{live_server}/settings/appearance")

    scope_root = page.locator("div.omnigent-app")
    expect(scope_root).to_be_visible(timeout=30_000)
    dark_root = page.locator("div.omnigent-app > div.dark")
    expect(dark_root).to_be_attached()

    apply_dracula_custom_theme(page, scope_root)
    backgrounds = drive_translucency_journey(page, scope_root)
    pane_background = dark_root.evaluate(
        "el => getComputedStyle(el).getPropertyValue('--background').trim()"
    )
    assert_sidebar_stays_dark(backgrounds, pane_background)


def test_standalone_opaque_custom_sidebar_keeps_dark_palette(page: Page, live_server: str) -> None:
    """Standalone SPA control: the same journey keeps the sidebar dark throughout."""
    page.emulate_media(color_scheme="light")
    page.goto(f"{live_server}/settings/appearance")

    mode = page.get_by_role("radiogroup", name="Mode", exact=True)
    expect(mode).to_be_visible(timeout=30_000)
    dark = mode.get_by_role("radio", name="Dark")
    dark.click()
    expect(dark).to_have_attribute("aria-checked", "true")
    html_root = page.locator("html")
    expect(html_root).to_have_class(re.compile(r"\bdark\b"))

    apply_dracula_custom_theme(page, html_root)
    backgrounds = drive_translucency_journey(page, html_root)
    pane_background = html_root.evaluate(
        "el => getComputedStyle(el).getPropertyValue('--background').trim()"
    )
    assert_sidebar_stays_dark(backgrounds, pane_background)
