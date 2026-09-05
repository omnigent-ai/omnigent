"""Managed omnigent (embedded build): dark mode must survive the software keyboard.

Guards the managed-only regression where dark mode paints white around the
software keyboard: in the managed (Databricks-embedded) build the scoped
stylesheet collapses ``:root`` / ``html`` / ``body`` onto the ``.omnigent-app``
scope root, so the scoped ``body { background: var(--background) }`` rule
paints the scope root. The scope root must therefore resolve the DARK tokens
when the host drives dark mode (``dark`` class on the scope root +
``.omnigent-app.dark`` selector forms; see ``web/src/embed.tsx`` and
``web/vite.embed.config.ts``) — if it resolves the light tokens instead, the
scope root paints ``#fff``. Nothing shows that white while the app shell covers
the full viewport — but on a phone the iOS shell locks ``.app-shell`` to the
visual viewport when the software keyboard opens (``useIOSViewportLock`` + the
``[data-ios-native].app-shell`` height rule), and the strip the shell vacates
is painted by the scope root: white notches/corners around and behind the
keyboard in dark mode. Standalone is unaffected (``.dark`` sits on ``<html>``,
so ``body`` resolves the dark background) — hence "managed only".

The managed surface has no existing lane, so a session fixture builds the real
scoped embed island (``web/dist-embed``) plus a minimal host page mirroring the
Databricks monolith's mount (``tests/e2e_ui/embed_harness/``) and serves it
from the live test server's static dir. The keyboard's viewport effect is
simulated by publishing ``--omnigent-viewport-height``, exactly what
``useIOSViewportLock`` does when the real keyboard opens (same pattern as
``test_ios_ipad_safe_layout.py``). A standalone control test drives the same
journey on the plain SPA and must stay green, pinning the regression to the
managed embed.
"""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import filelock
import pytest
from PIL import Image
from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WEB_DIR = _REPO_ROOT / "web"
_HARNESS_SRC = _REPO_ROOT / "tests" / "e2e_ui" / "embed_harness"
_WEB_UI_DIST = _REPO_ROOT / "omnigent" / "server" / "static" / "web-ui"

# iPhone-sized viewport: the report is a phone journey (software keyboard).
_MOBILE_VIEWPORT = {"width": 390, "height": 844}
# Visual-viewport height with the software keyboard open — the value
# useIOSViewportLock publishes to --omnigent-viewport-height on a real device.
_KEYBOARD_TOP = 500

# Channel threshold splitting the dark background (#0e1013 ≈ 16) from the
# broken light paint (#fff = 255). Generous so any dark palette passes and any
# light leak fails.
_LIGHT_CHANNEL_MEAN = 128

# Minimal stand-in for the iOS WKWebView bridge (web/ios's injected
# window.omnigentNative), the same feature-detection stub the other mobile
# tests use, so isIOSShell() sees the iOS shell and the app-shell picks up
# data-ios-native + the keyboard viewport lock.
_IOS_SHELL_INIT_SCRIPT = """
window.omnigentNative = {
  kind: "ios",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  onOpenPath: function () { return function () {}; },
  onNativeInsets: function () { return function () {}; },
  onSidebarDrag: function () { return function () {}; },
  onViewModeChanged: function () { return function () {}; },
  setViewMode: function () {},
  setServerSwitcherHidden: function () {},
  setSidebarOpen: function () {},
};
"""


@pytest.fixture(scope="session")
def embed_host_url(live_server: str, request: pytest.FixtureRequest) -> str:
    """Build the managed-embed island + host harness and serve it same-origin.

    Builds ``web/dist-embed`` (the REAL scoped embed artifact,
    ``vite.embed.config.ts``), bundles the minimal host page in
    ``tests/e2e_ui/embed_harness/`` against it (resolving the island's bare
    React externals the way the monolith's rspack does), and copies the result
    into the live server's static dir so ``/embed-host/`` serves the managed
    build against the same API origin.

    :param live_server: Spawned test server base URL (serves the static dir).
    :param request: pytest request — used to skip on ``--ui-base-url`` (an
        external server's static dir can't be injected into).
    :returns: The harness URL, e.g. ``http://127.0.0.1:PORT/embed-host/``.
    """
    if request.config.getoption("--ui-base-url"):
        pytest.skip("embed-host harness needs the locally spawned server's static dir")

    vite = _WEB_DIR / "node_modules" / ".bin" / "vite"
    if not vite.exists():
        pytest.skip("web/node_modules not installed; cannot build the embed island")

    env_overrides = {"CI": "true", "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0"}
    import os

    env = {**os.environ, **env_overrides}

    # Same cross-process serialization contract as built_spa: concurrent
    # sessions/worktrees must not clobber each other's dist-embed.
    with filelock.FileLock(str(_WEB_DIR / ".embed-build.lock"), timeout=900):
        subprocess.run(
            [str(vite), "build", "--config", "vite.embed.config.ts"],
            cwd=_WEB_DIR,
            check=True,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        stage = _WEB_DIR / ".embed-host-harness"
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir()
        for name in ("index.html", "main.js", "vite.config.mjs"):
            shutil.copy2(_HARNESS_SRC / name, stage / name)
        subprocess.run(
            [str(vite), "build", "--config", str(stage / "vite.config.mjs")],
            cwd=_WEB_DIR,
            check=True,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        target = _WEB_UI_DIST / "embed-host"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(stage / "dist", target)

    return f"{live_server}/embed-host/"


def _open_keyboard_over_landing(page: Page, url: str) -> None:
    """Drive the shared journey: open the app, tap the composer, keyboard up.

    :param page: Playwright page (iOS bridge injected by the caller).
    :param url: App entry URL (managed harness or standalone SPA root).
    """
    page.goto(url)

    composer = page.get_by_test_id("new-chat-landing-input")
    expect(composer).to_be_visible(timeout=30_000)

    app_shell = page.locator(".app-shell")
    expect(app_shell).to_have_attribute("data-ios-native", "true")

    # Tap the message box — on the device this is what summons the software
    # keyboard — then apply the keyboard's viewport effect the exact way
    # useIOSViewportLock publishes it when the real keyboard opens.
    composer.click()
    page.evaluate(
        "height => document.documentElement.style"
        ".setProperty('--omnigent-viewport-height', `${height}px`)",
        _KEYBOARD_TOP,
    )
    expect(app_shell).to_have_css("height", f"{_KEYBOARD_TOP}px")

    # Journey sanity: the composer stays visible above the keyboard.
    box = composer.bounding_box()
    assert box is not None
    assert box["y"] + box["height"] <= _KEYBOARD_TOP + 1


def _sample_keyboard_band(page: Page) -> list[tuple[int, int, tuple[int, int, int]]]:
    """Screenshot the page and sample the strip the keyboard exposes.

    Samples the two bottom corners plus the band's center — the pixels a user
    sees around/behind the keyboard's rounded corners while it is up.

    :param page: The driven page, already keyboard-shrunken.
    :returns: ``(x, y, (r, g, b))`` for each sampled viewport point.
    """
    # Hold the keyboard-open state briefly so paint settles before sampling
    # (and a recorded run shows the exposed band clearly).
    page.wait_for_timeout(1200)
    img = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
    # Device profiles run at DPR > 1; map viewport coordinates onto pixels.
    scale = img.width / _MOBILE_VIEWPORT["width"]
    points = [
        (6, _MOBILE_VIEWPORT["height"] - 6),  # bottom-left corner
        (_MOBILE_VIEWPORT["width"] - 6, _MOBILE_VIEWPORT["height"] - 6),  # bottom-right corner
        (_MOBILE_VIEWPORT["width"] // 2, (_KEYBOARD_TOP + _MOBILE_VIEWPORT["height"]) // 2),
    ]
    samples = []
    for x, y in points:
        px = img.getpixel(
            (min(int(x * scale), img.width - 1), min(int(y * scale), img.height - 1))
        )
        samples.append((x, y, px[:3]))
    return samples


def _assert_band_is_dark(samples: list[tuple[int, int, tuple[int, int, int]]], where: str) -> None:
    """Fail if any sampled pixel in the keyboard-exposed band renders light.

    :param samples: ``(x, y, rgb)`` samples from ``_sample_keyboard_band``.
    :param where: Human label for the surface under test (message only).
    """
    light = [(x, y, rgb) for x, y, rgb in samples if sum(rgb) / 3 > _LIGHT_CHANNEL_MEAN]
    assert not light, (
        f"{where}: the strip the software keyboard exposes renders LIGHT in "
        f"dark mode — light pixels at {light} (expected the dark app "
        "background). The region behind/around the keyboard's corners paints "
        "white instead of the dark app background."
    )


def test_managed_embed_dark_keyboard_band_stays_dark(page: Page, embed_host_url: str) -> None:
    """Managed embed, dark, keyboard up: the exposed strip must paint dark.

    On the managed (embedded) build in dark mode, tap the composer so the
    software keyboard opens — the region the app shell vacates for the
    keyboard (visible at/around the keyboard's corners) must render the dark
    background, not white.
    """
    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.emulate_media(color_scheme="dark")
    page.add_init_script(_IOS_SHELL_INIT_SCRIPT)

    _open_keyboard_over_landing(page, embed_host_url)

    # Mechanism probe, surfaced in the failure output for triage: the scope
    # root is what paints the exposed strip in the embed.
    scope_bg = page.evaluate(
        "getComputedStyle(document.querySelector('.omnigent-app')).backgroundColor"
    )
    samples = _sample_keyboard_band(page)
    _assert_band_is_dark(samples, f"managed embed (scope-root background: {scope_bg})")


def test_standalone_dark_keyboard_band_stays_dark(page: Page, live_server: str) -> None:
    """Control: the same journey on the standalone SPA already paints dark.

    Pins the "managed only" dimension of the regression — if this control
    ever fails, the regression is in the shared SPA, not the embed scoping.
    """
    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.emulate_media(color_scheme="dark")
    page.add_init_script(_IOS_SHELL_INIT_SCRIPT)

    _open_keyboard_over_landing(page, f"{live_server}/")

    samples = _sample_keyboard_band(page)
    _assert_band_is_dark(samples, "standalone SPA")
