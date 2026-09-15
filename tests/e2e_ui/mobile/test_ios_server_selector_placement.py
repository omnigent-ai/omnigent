"""E2E: iOS server selection lives in the sidebar, not the chat header.

In the iOS app the native server-switcher pill used to float top-center over
the WebView, directly inside the chat header band, where it crowded the
floating header controls (sidebar toggle, overflow kebab) and overlapped the
Plan tracker's title row on a notched iPhone. The fix moves server selection
into the navigation drawer (the same in-sidebar picker the desktop shell
uses) and stops requesting the floating pill over the chat surface on shells
that host that picker.

The pill itself is native SwiftUI chrome (``ServerSwitcher`` in
``web/ios/Omnigent/WebShellView.swift``) that Playwright cannot render, but
its placement is a web-visible contract:

* the shell pushes the pill's exact footprint over the bridge
  (``onNativeInsets`` -> ``--omnigent-native-top-bar``, see
  ``web/src/lib/nativeInsets.ts``),
* the web app *requests* the pill shown over the chat surface
  (``setNativeServerSwitcherHidden(false)`` ->
  ``--omnigent-top-bar-visible: 1``) whenever the transcript is frontmost
  (``web/src/hooks/useNativeServerSwitcher.ts``,
  ``web/src/pages/ChatPage.tsx``), and
* the shell exposes the sidebar picker's data over ``getServerPicker`` (the
  same bridge surface the desktop shell provides), which the SPA renders as
  the sidebar's server row (``web/src/shell/SidebarServerPicker.tsx``).

These tests drive the SPA the way the fixed WKWebView shell does — iPhone
viewport, an injected ``window.omnigentNative`` bridge pushing the shell's
real inset metrics plus the server-picker payload, and a notch-sized OS
safe-area inset — and assert both halves of the revised design: while the
chat surface is frontmost the web must not ask the shell to float the server
switcher inside the chat-header band (where it would overlap the Plan
title), and server selection must instead be reachable from the navigation
drawer, switching servers through the shell bridge.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, ViewportSize, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

# Phone-sized viewport: the iOS shell is an iPhone surface, and the narrow
# width is where the header crowding bites.
_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}

# OS safe-area top inset of a notched iPhone (Dynamic Island class), CSS px.
# WKWebView delivers this via env(safe-area-inset-top); Chromium cannot
# emulate env(), so the test injects the same value into the shared fold var
# the layout actually consumes (--omnigent-safe-top, index.css).
_SAFE_TOP_PX = 59

# The native bars' footprints the iOS shell pushes over the bridge — mirror of
# InsetMetrics in web/ios/Omnigent/WebShellView.swift:
#   topBar    = serverSwitcherHeight (28) + serverSwitcherTopPadding (8) = 36
#   bottomBar = barSegmentHeight (34) + barCapsulePadding*2 (8)
#               + barBottomPadding (6) = 48
_SWITCHER_HEIGHT_PX = 28.0
_SWITCHER_TOP_PADDING_PX = 8.0
_TOP_BAR_PX = 36
_BOTTOM_BAR_PX = 48

# Another server the shell remembers, offered by the sidebar picker's
# "Recents" section. Host label as the picker renders it.
_ALT_SERVER_URL = "https://alt-server.example.test:8443/"
_ALT_SERVER_HOST = "alt-server.example.test:8443"

# Stand-in for the iOS WKWebView bridge of a shell that hosts the sidebar
# server picker. Runs before any app script (add_init_script) so nativeApi()
# in nativeBridge.ts sees an iOS shell: `kind` drives isIOSShell(),
# onNativeInsets immediately pushes the shell's real cached footprints
# (exactly as WebShellView re-emits them on each load), the server-picker trio
# mirrors the payload WebShellView pushes from managed config + recents, and
# the rest keep unrelated native calls (badge / notify / view mode) from
# throwing under the stub.
_IOS_SHELL_INIT_SCRIPT = f"""
window.__switcherHiddenCalls = [];
window.__switchServerCalls = [];
window.omnigentNative = {{
  kind: "ios",
  setBadgeCount: function () {{}},
  notify: function () {{ return Promise.resolve(false); }},
  onNotificationActivated: function () {{ return function () {{}}; }},
  onNativeInsets: function (cb) {{
    cb({{ topBar: {_TOP_BAR_PX}, bottomBar: {_BOTTOM_BAR_PX} }});
    return function () {{}};
  }},
  setServerSwitcherHidden: function (hidden) {{
    window.__switcherHiddenCalls.push(hidden);
  }},
  getServerPicker: function () {{
    return Promise.resolve({{
      currentOrigin: location.origin,
      managedServers: [],
      recentServers: [location.origin + "/", "{_ALT_SERVER_URL}"],
    }});
  }},
  switchServer: function (url) {{
    window.__switchServerCalls.push(url);
    return Promise.resolve();
  }},
  openServerSetup: function () {{}},
  setViewMode: function () {{}},
  onViewModeChanged: function () {{ return function () {{}}; }},
}};
"""

# A plan for the session, published through the events route (the same path
# the harness forwarders post to) so the Plan tracker renders without a live
# agent turn — the pattern from chat/test_plan_tracker.py.
_PLAN = [
    {"content": "Read the code", "status": "completed", "activeForm": "Reading the code"},
    {"content": "Write the tracker", "status": "in_progress", "activeForm": "Writing the tracker"},
    {"content": "Ship it", "status": "pending", "activeForm": "Shipping it"},
]


def _publish_todos(base_url: str, session_id: str, todos: list[dict[str, str]]) -> None:
    """Publish the full todo list through the events route.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param todos: Full list; each item is ``{"content", "status", "activeForm"}``.
    :returns: None.
    """
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_todos", "data": {"todos": todos}},
        timeout=10.0,
    )
    resp.raise_for_status()


def _switcher_band(inner_width: float) -> dict[str, float]:
    """Compute the native server-switcher pill's floating band, in CSS px.

    Mirrors ``ServerSwitcherMetrics.maxWidth`` and the ``.padding(.top, 8)`` /
    28 px height placement in ``web/ios/Omnigent/WebShellView.swift``: the
    pill is horizontally centered at ``min(172, max(120, 0.38 * width))`` wide,
    floating ``serverSwitcherTopPadding`` below the OS safe-area inset.

    :param inner_width: The WebView viewport width in CSS px.
    :returns: The band as ``{"left", "top", "right", "bottom"}``.
    """
    width = min(172.0, max(120.0, inner_width * 0.38))
    left = (inner_width - width) / 2
    top = _SAFE_TOP_PX + _SWITCHER_TOP_PADDING_PX
    return {"left": left, "top": top, "right": left + width, "bottom": top + _SWITCHER_HEIGHT_PX}


def _as_rect(box: dict[str, float]) -> dict[str, float]:
    """Convert a Playwright bounding box to a left/top/right/bottom rect.

    :param box: ``{"x", "y", "width", "height"}`` from ``bounding_box()``.
    :returns: The rect as ``{"left", "top", "right", "bottom"}``.
    """
    return {
        "left": box["x"],
        "top": box["y"],
        "right": box["x"] + box["width"],
        "bottom": box["y"] + box["height"],
    }


def _intersects(a: dict[str, float], b: dict[str, float]) -> bool:
    """Whether two left/top/right/bottom rects overlap.

    :param a: First rect.
    :param b: Second rect.
    :returns: True when the rects share any area.
    """
    return (
        a["left"] < b["right"]
        and b["left"] < a["right"]
        and a["top"] < b["bottom"]
        and b["top"] < a["bottom"]
    )


def _open_ios_session(page: Page, base_url: str, session_id: str) -> None:
    """Open the session under the injected iOS shell at an iPhone viewport.

    :param page: Playwright page fixture (fresh context per test).
    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id to open.
    :returns: None. Leaves the chat view loaded with the notch inset applied.
    """
    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")

    # Gate: the SPA recognized the iOS shell (isIOSShell() -> AppShell tag).
    expect(page.locator(".app-shell")).to_have_attribute("data-ios-native", "true")

    # Inject the notch's OS safe-area inset. On device WKWebView supplies it
    # via env(safe-area-inset-top); here it goes straight into the shared fold
    # var every consumer reads (chat-header top, --omnigent-inset-top).
    page.evaluate(
        "() => document.documentElement.style"
        f".setProperty('--omnigent-safe-top', '{_SAFE_TOP_PX}px')"
    )


def test_ios_server_switcher_stays_out_of_chat_header(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The iOS shell's server switcher must not float inside the chat header.

    Drives the reported journey: open a session in the iOS app (injected
    bridge + iPhone viewport + notch inset), let the chat view with a Plan
    render, and check where the server pill floats. With server selection
    moved to the navigation drawer, the web must not request the native pill
    shown inside the chat-header band, nor let its band overlap the Plan
    title row. Before the fix the web requested the pill visible over the
    chat and its band sat inside the header, over the Plan title — the
    crowding in the bug report's screenshot.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    :returns: None.
    """
    base_url, session_id = seeded_session

    # The plan exists BEFORE the page opens, so the snapshot path seeds the
    # tracker on load (no SSE-connect race).
    _publish_todos(base_url, session_id, _PLAN)

    _open_ios_session(page, base_url, session_id)

    tracker = page.locator('[data-testid="plan-tracker"]')
    expect(tracker).to_be_visible(timeout=15_000)
    header = page.locator(".chat-header")
    expect(header).to_be_visible()

    # The web half of "the pill shows over the chat": if the web requests the
    # native switcher visible while the transcript is frontmost,
    # --omnigent-top-bar-visible flips to 1. With the selector moved to the
    # drawer this never happens, the wait times out, and the switcher is
    # treated as not shown.
    switcher_requested = True
    try:
        page.wait_for_function(
            "() => getComputedStyle(document.documentElement)"
            ".getPropertyValue('--omnigent-top-bar-visible').trim() === '1'",
            timeout=10_000,
        )
    except PlaywrightTimeoutError:
        switcher_requested = False

    # Let the layout settle on the state under test before measuring.
    page.wait_for_timeout(500)

    inner_width = float(page.evaluate("() => window.innerWidth"))
    pill = _switcher_band(inner_width)
    header_box = header.bounding_box()
    plan_box = tracker.locator("summary").bounding_box()
    assert header_box is not None and plan_box is not None

    violations: list[str] = []
    if switcher_requested and _intersects(pill, _as_rect(header_box)):
        violations.append(
            f"the server-switcher band {pill} floats inside the chat header "
            f"{_as_rect(header_box)} (selector rendered in the chat header)"
        )
    if switcher_requested and _intersects(pill, _as_rect(plan_box)):
        violations.append(
            f"the server-switcher band {pill} overlaps the Plan title row "
            f"{_as_rect(plan_box)} (the crowding in the bug report's screenshot)"
        )

    assert not violations, (
        "iOS server switcher must not float inside the chat header: " + "; ".join(violations)
    )

    # The web must also have asked the shell to keep the pill hidden — the
    # positive half of the contract, so this can't pass merely because the
    # bridge was never driven.
    hidden_calls = page.evaluate("() => window.__switcherHiddenCalls")
    assert hidden_calls and all(hidden_calls), (
        "the web must only ever request the native switcher hidden on a shell "
        f"with the sidebar picker; got setServerSwitcherHidden calls: {hidden_calls}"
    )


def test_ios_server_selection_lives_in_the_sidebar(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Server selection is reachable from the iOS navigation drawer.

    The revised design's other half: with the floating pill gone, the sidebar
    must carry the server picker (the desktop shell's treatment, adapted to
    the drawer). Opens the drawer from the chat header, picks another recent
    server from the picker's menu, and asserts the switch is requested
    through the shell bridge.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    :returns: None.
    """
    base_url, session_id = seeded_session

    _open_ios_session(page, base_url, session_id)

    # The drawer starts closed on a phone viewport; the chat header's toggle
    # is the way in.
    page.get_by_role("button", name="Open sidebar").click()

    picker = page.get_by_test_id("sidebar-server-picker")
    expect(picker).to_be_visible()
    picker.click()

    # The picker's menu offers the other remembered server; choosing it must
    # ask the shell to switch, with the exact URL the picker payload carried.
    page.get_by_role("menuitem", name=_ALT_SERVER_HOST).click()
    page.wait_for_function(
        "() => window.__switchServerCalls.length > 0",
        timeout=10_000,
    )
    assert page.evaluate("() => window.__switchServerCalls") == [_ALT_SERVER_URL], (
        "selecting a server in the sidebar picker must reach the shell bridge "
        "with the exact URL the picker payload carried"
    )
