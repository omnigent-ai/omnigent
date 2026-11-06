"""Android WebView shell: web-layer feature detection and the safe-area fold.

The native Android shell (``web/android``) loads the SPA and injects
``window.omnigentNative = {kind: "android", ...}``. The web layer feature-detects
it (``isAndroidShell()`` in ``web/src/lib/nativeBridge.ts``) and, when true, the
``AppShell`` tags its root with ``data-android-native="true"``
(``web/src/shell/AppShell.tsx``). That attribute gates the Android-specific
chrome in ``index.css`` — most importantly the safe-area fold: unlike iOS,
Android WebView reports ``env(safe-area-inset-*)`` as 0, so the shell injects the
OS-measured inset as ``--omnigent-android-safe-area-*`` and ``index.css`` folds
it into the shared ``--omnigent-safe-top/bottom`` with ``max()``.

The e2e_ui harness runs the SPA in a plain Chromium browser, not the Android
WebView, so ``isAndroidShell()`` is false by default. To exercise the shell path
end-to-end we inject a minimal ``window.omnigentNative`` stub via
``add_init_script`` *before any app script runs* — the same feature-detection
stubbing the desktop shell tests use (``sessions/test_pinned_session_hotkeys.py``
injects ``window.omnigentDesktop``).

These cover the chain the ``nativeBridge`` unit tests can't reach end to end:
the injected bridge -> ``isAndroidShell()`` -> the ``AppShell``
``data-android-native`` attribute -> the ``index.css`` ``max()`` fold that lets
the injected OS inset reach the layout's shared vars.
"""

from __future__ import annotations

import io
from typing import Literal

import httpx
import pytest
from PIL import Image
from playwright.sync_api import Page, ViewportSize, expect

# A phone-sized viewport: the Android shell is a mobile surface, and the narrow
# width is where the sidebar behaves as an overlay drawer (the
# ``[data-android-native]`` drawer rules this change adds). The
# ``data-android-native`` tag itself is viewport-independent.
_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}
_UNFOLDED_VIEWPORT: ViewportSize = {"width": 840, "height": 900}

# Minimal stand-in for the Android WebView bridge (``web/android``'s
# ``NativeBridgeScript``). Runs before any app script on every navigation
# (``add_init_script``), so ``nativeApi()`` in ``nativeBridge.ts`` — which now
# accepts ``kind === "android"`` — sees a native shell. Every method is a guarded
# no-op: ``kind`` is what ``isAndroidShell()`` keys off, and the rest keep
# unrelated native calls (badge / notify / inset subscription) from throwing
# under the stub.
_ANDROID_SHELL_INIT_SCRIPT = """
window.omnigentNative = {
  kind: "android",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  onNativeInsets: function () { return function () {}; },
};
"""

# Read the *resolved* ``--omnigent-safe-top`` in pixels. ``getComputedStyle`` on a
# custom property returns its declared text (the ``max()`` expression), so instead
# size a throwaway probe by ``var(--omnigent-safe-top)`` and read its computed
# height, which resolves the fold.
_READ_SAFE_TOP_PX = """
() => {
  const probe = document.createElement('div');
  probe.style.cssText =
    'position:absolute;visibility:hidden;pointer-events:none;height:var(--omnigent-safe-top)';
  document.body.appendChild(probe);
  const px = getComputedStyle(probe).height;
  probe.remove();
  return px;
}
"""


def test_android_shell_tags_root_and_folds_os_inset(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Under the injected Android bridge, the SPA tags its root and folds the inset.

    Asserts (1) the app-shell carries ``data-android-native="true"`` — i.e.
    ``isAndroidShell()`` -> ``AppShell`` wiring fires — and (2) the OS inset the
    shell injects as ``--omnigent-android-safe-area-top`` reaches the shared
    ``--omnigent-safe-top`` through the ``index.css`` ``max()`` fold (which is 0
    in a plain browser, where ``env(safe-area-inset-top)`` is also 0).

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, session_id = seeded_session

    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.add_init_script(_ANDROID_SHELL_INIT_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")

    shell = page.locator(".app-shell")
    expect(shell).to_have_attribute("data-android-native", "true")

    # No native inset injected yet: env(safe-area-inset-top) is 0 in a plain
    # browser and the Android var is unset, so the fold resolves to 0.
    assert page.evaluate(_READ_SAFE_TOP_PX) == "0px"

    # The native layer pushes the measured OS inset as
    # --omnigent-android-safe-area-top; index.css folds it into
    # --omnigent-safe-top via max(), so the layout reads the real inset.
    page.evaluate(
        "() => document.documentElement.style"
        ".setProperty('--omnigent-android-safe-area-top', '40px')"
    )
    assert page.evaluate(_READ_SAFE_TOP_PX) == "40px"


def test_no_android_tag_or_fold_in_plain_browser(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A plain browser tab (no bridge) gets neither the tag nor the fold.

    Without the ``window.omnigentNative`` stub, ``isAndroidShell()`` is false, so
    the app-shell must NOT carry ``data-android-native`` and the
    ``--omnigent-android-safe-area-*`` fold must contribute nothing — the gate
    that keeps the Android chrome off the plain web app. This is the half of the
    contract only an end-to-end browser run can prove.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, session_id = seeded_session

    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")

    shell = page.locator(".app-shell")
    expect(shell).to_be_visible()
    assert shell.get_attribute("data-android-native") is None

    # The web app never injects --omnigent-android-safe-area-*, so with
    # env(safe-area-inset-top) also 0 here the shared inset stays 0.
    assert page.evaluate(_READ_SAFE_TOP_PX) == "0px"


# A solid marker block prepended to scrolled content, so a status-bar band
# screenshot shows whether any content paints there.
_PREPEND_MARKER = """
([container, height]) => {
  const marker = document.createElement("div");
  marker.dataset.statusBarMarker = "true";
  marker.style.cssText = `height:${height}px;flex:none;background:rgb(255,0,0)`;
  container.prepend(marker);
}
"""


def _marker_rows(page: Page, top: int, bottom: int) -> list[int]:
    """Return the viewport rows in ``[top, bottom)`` where the red marker paints."""
    viewport = page.viewport_size
    assert viewport is not None
    band = Image.open(
        io.BytesIO(
            page.screenshot(
                clip={"x": 0, "y": top, "width": viewport["width"], "height": bottom - top}
            )
        )
    ).convert("RGB")
    # Through a mask ramp or the header glass the marker blends but stays red-dominant.
    return sorted(
        {
            top + row * (bottom - top) // band.height
            for row in range(band.height)
            for column in range(band.width)
            if (pixel := band.getpixel((column, row)))[0] - max(pixel[1], pixel[2]) > 60
        }
    )


def _assert_status_bar_blends(page: Page, status_bar_height: int) -> None:
    """Assert the status-bar band continues the color of the header row below it."""
    header_box = page.locator("header.chat-header").first.bounding_box()
    assert header_box is not None
    # The header glass gradient is strongest at its leading edge.
    column = int(header_box["x"]) + 8

    def strip_color(row: int) -> tuple[int, ...]:
        strip = Image.open(
            io.BytesIO(page.screenshot(clip={"x": column, "y": row, "width": 12, "height": 2}))
        ).convert("RGB")
        pixels = [strip.getpixel((x, y)) for x in range(strip.width) for y in range(strip.height)]
        return tuple(sum(channel) // len(pixels) for channel in zip(*pixels, strict=True))

    band, below = strip_color(status_bar_height // 2), strip_color(status_bar_height + 4)
    assert max(abs(a - b) for a, b in zip(band, below, strict=True)) <= 4, (
        f"the status bar reads as a separate band: {band} above {below}"
    )


_COLOR_SCHEMES = pytest.mark.parametrize("color_scheme", ["light", "dark"])
_SHELL_VIEWPORTS = pytest.mark.parametrize(
    "viewport", [_MOBILE_VIEWPORT, _UNFOLDED_VIEWPORT], ids=["phone", "unfolded"]
)


@_COLOR_SCHEMES
@_SHELL_VIEWPORTS
def test_android_chat_keeps_content_out_of_status_bar(
    page: Page,
    seeded_session: tuple[str, str],
    viewport: ViewportSize,
    color_scheme: Literal["light", "dark"],
) -> None:
    """An Android chat paints no content in the status bar or top fade.

    The Pixel Fold inner display crosses the 48rem breakpoint. At rest the status
    bar must blend into the header glass and the first transcript content must
    start below the fade; scrolled, nothing may show in the status-bar band.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    :param viewport: Phone or unfolded-foldable viewport.
    :param color_scheme: Light or dark system theme.
    """
    base_url, session_id = seeded_session
    status_bar_height = 48

    page.emulate_media(color_scheme=color_scheme)
    page.set_viewport_size(viewport)
    page.add_init_script(_ANDROID_SHELL_INIT_SCRIPT)
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {
                "agent": "hello_world",
                "response_id": "android_statusbar_scroll",
                "text": "Short transcript",
            },
        },
        timeout=10.0,
    ).raise_for_status()
    page.goto(f"{base_url}/c/{session_id}")

    expect(page.locator(".app-shell")).to_have_attribute("data-android-native", "true")
    expect(page.locator('[data-testid="message-bubble"][data-role="assistant"]')).to_be_visible()
    page.evaluate(
        "height => document.documentElement.style"
        ".setProperty('--omnigent-android-safe-area-top', `${height}px`)",
        status_bar_height,
    )

    _assert_status_bar_blends(page, status_bar_height)
    # Light glass blends with the canvas, so also check it reaches the screen top.
    glass_top = page.locator("header.session-header-glass").evaluate(
        "header => header.getBoundingClientRect().top"
        " + parseFloat(getComputedStyle(header, '::before').top)"
    )
    assert glass_top <= 0, f"the header glass starts {glass_top}px below the screen top"

    # Status bar + server-switcher footprint + the 32px fade ramp.
    switcher_footprint = page.evaluate(
        "() => ['margin', 'height'].reduce((total, part) => total + parseFloat("
        "getComputedStyle(document.documentElement)"
        ".getPropertyValue(`--omnigent-android-switcher-${part}`)), 0)"
    )
    fade_end = status_bar_height + int(switcher_footprint) + 32
    content = page.locator(".chat-conversation-content")
    page.evaluate(_PREPEND_MARKER, [content.element_handle(), 40])
    assert _marker_rows(page, 0, fade_end) == [], "at-rest transcript starts inside the top fade"

    page.locator("[data-status-bar-marker]").evaluate(
        "marker => { marker.style.height = '3000px'; }"
    )
    scroll_top = content.evaluate(
        "element => {"
        "let scroller = element.parentElement;"
        "while (scroller && scroller.scrollHeight <= scroller.clientHeight + 1) {"
        "scroller = scroller.parentElement; }"
        "scroller.scrollTop = 1000;"
        "return scroller.scrollTop;"
        "}"
    )
    assert scroll_top > 0, "the transcript did not scroll"
    marker_rows = _marker_rows(page, 0, status_bar_height)
    assert marker_rows == [], f"scrolled transcript paints in the status bar at rows {marker_rows}"


@_COLOR_SCHEMES
@_SHELL_VIEWPORTS
def test_android_scrolled_settings_page_keeps_status_bar_blank(
    page: Page,
    live_server: str,
    viewport: ViewportSize,
    color_scheme: Literal["light", "dark"],
) -> None:
    """Scrolled Settings content never paints in the status bar; the title stays intact.

    Follow the Android journey into Settings / Appearance at phone and unfolded
    widths, scroll the page under the status bar, and check the band pixels.

    :param page: Playwright page fixture (fresh context per test).
    :param live_server: Base URL of the shared test server.
    :param viewport: Phone or unfolded-foldable viewport.
    :param color_scheme: Light or dark system theme.
    """
    status_bar_height = 40
    page.emulate_media(color_scheme=color_scheme)
    page.set_viewport_size(viewport)
    page.add_init_script(_ANDROID_SHELL_INIT_SCRIPT)
    page.goto(f"{live_server}/settings/appearance")

    expect(page.locator(".app-shell")).to_have_attribute("data-android-native", "true")
    title = page.get_by_role("heading", name="Appearance", exact=True)
    expect(title).to_be_visible()
    page.evaluate(
        "height => document.documentElement.style"
        ".setProperty('--omnigent-android-safe-area-top', `${height}px`)",
        status_bar_height,
    )

    _assert_status_bar_blends(page, status_bar_height)

    settings_page = page.locator(".settings-page-scroll")
    page.evaluate(_PREPEND_MARKER, [settings_page.locator(":scope > div").element_handle(), 3000])
    scroll_top = settings_page.evaluate(
        "element => { element.scrollTop = 1000; return element.scrollTop; }"
    )
    assert scroll_top > 0, "Settings / Appearance did not scroll"
    marker_rows = _marker_rows(page, 0, status_bar_height)
    assert marker_rows == [], f"scrolled Settings paints in the status bar at rows {marker_rows}"

    if viewport is _MOBILE_VIEWPORT:
        # The fixed phone title must paint: hiding it has to change its pixels.
        box = title.bounding_box()
        assert box is not None
        shown_title = page.screenshot(clip=box)
        title.evaluate("element => { element.style.visibility = 'hidden'; }")
        assert page.screenshot(clip=box) != shown_title, "the Settings title is hidden"


# Bridge stub that also CAPTURES the notification-activation callback the SPA
# registers (``useIdleNotifications`` -> ``onNativeNotificationActivated``), so a
# test can fire it the way the Android shell does when its badge notification is
# tapped. The real shell calls the callback with the notification's stored
# ``navigatePath``.
_ANDROID_ACTIVATION_INIT_SCRIPT = """
window.__omnigentActivations = [];
window.omnigentNative = {
  kind: "android",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function (cb) {
    window.__omnigentActivate = cb;
    return function () { delete window.__omnigentActivate; };
  },
  onNativeInsets: function () { return function () {}; },
};
"""


def test_badge_notification_activation_navigates_to_target(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Tapping the Android badge notification routes the SPA to its target.

    The chain under test is the one the unit tests can't reach end to end: the
    shell's ``onNotificationActivated`` callback (registered by
    ``useIdleNotifications``) receives the badge's ``navigatePath`` and the SPA
    navigates there in place — no reload, sidebar-closed mobile layout intact.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, session_id = seeded_session

    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.add_init_script(_ANDROID_ACTIVATION_INIT_SCRIPT)
    page.goto(f"{base_url}/")

    # The SPA registers its activation callback on mount.
    page.wait_for_function("() => typeof window.__omnigentActivate === 'function'")

    # Fire the callback the way the shell does for a single-unread badge tap.
    page.evaluate("path => window.__omnigentActivate(path)", f"/c/{session_id}")
    page.wait_for_url(f"**/c/{session_id}")
    expect(page.locator(".app-shell")).to_have_attribute("data-android-native", "true")


def test_sidebar_open_param_reveals_drawer_and_strips_itself(
    page: Page,
    live_server: str,
) -> None:
    """``?sidebar=open`` (the multi-unread badge target) opens the drawer once.

    A multi-unread badge notification targets ``/?sidebar=open`` so the tap
    lands on the session list instead of a bare composer. The param must open
    the phone-width drawer and then strip itself from the URL (one-shot,
    ``replace``), so a later in-app navigation doesn't re-trigger it.

    :param page: Playwright page fixture (fresh context per test).
    :param live_server: Base URL of the shared test server.
    """
    base_url = live_server

    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.add_init_script(_ANDROID_SHELL_INIT_SCRIPT)
    drawer = page.locator('aside[aria-label="Conversations"]')

    # Control: without the param the phone-width drawer starts closed
    # (data-collapsed; CSS keeps the off-canvas box technically "visible",
    # so the attribute is the reliable open/closed signal).
    page.goto(f"{base_url}/")
    expect(drawer).to_have_attribute("data-collapsed", "true")

    page.goto(f"{base_url}/?sidebar=open")
    expect(drawer).not_to_have_attribute("data-collapsed", "true")
    page.wait_for_function("() => !window.location.search.includes('sidebar')")
