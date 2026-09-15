"""E2E: native shells — the pinned Plan header must stay clear of the floating controls.

The iOS WKWebView shell shifts the floating chat header down by the OS
safe-area inset (the ``[data-ios-native] .chat-header`` rule in ``index.css``),
but the in-chat Plan tracker (``ChatPlanAccordion``) clears the header with a
fixed ``mt-14`` tuned for the web header pinned at ``top: 0``. On a notched
iPhone the safe-area shift pushes the header's floating controls — the left
sidebar-toggle pill and the right session-actions kebab — down into the Plan
bar, so they render on top of it instead of above it.

User journey covered: open a session with an active plan in the iOS app →
the Plan progress header is pinned at the top → the floating controls on the
left and right overlap the blue bar.

The Android shell shifts the header by the same rule (keyed off
``--omnigent-android-safe-area-top``, since Android's WebView reports
``env(safe-area-inset-top)`` as 0), so the journey is exercised under both
shells.

The e2e_ui harness runs the SPA in plain Chromium, so each shell is emulated
the same way ``test_android_shell.py`` emulates Android: a minimal
``window.omnigentNative = {kind: ..., ...}`` bridge is injected before any
app script runs, which makes ``isIOSShell()`` / ``isAndroidShell()`` true and
the ``AppShell`` tag its root ``data-ios-native`` / ``data-android-native`` —
activating the exact ``index.css`` rules the real shells use. The notch is
emulated per platform: on iOS via CDP ``Emulation.setSafeAreaInsetsOverride``
(so ``env(safe-area-inset-top)`` resolves to a real 47px iPhone-13-class
inset), on Android by injecting ``--omnigent-android-safe-area-top`` the way
the native layer does.

The plan is published straight through the Sessions events route (the same
harness-agnostic path the native forwarders post to), mirroring
``chat/test_plan_tracker.py``, so no live agent turn is needed.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import FloatRect, Locator, Page, expect

_TRACKER = '[data-testid="plan-tracker"]'

# iPhone 13-class portrait viewport (matches Playwright's "iPhone 13" device
# profile, so a recorder run with ``--device "iPhone 13"`` films pixel-exact)
# with the device's 47px notch inset at the top and 34px home-indicator inset
# at the bottom.
_IPHONE_VIEWPORT = {"width": 390, "height": 664}
_IPHONE_SAFE_AREA = {"top": 47, "left": 0, "bottom": 34, "right": 0}

# Minimal stand-in for the iOS WKWebView bridge (``web/ios``'s injected
# ``window.omnigentNative``). Runs before any app script on every navigation
# (``add_init_script``), so ``nativeApi()`` in ``nativeBridge.ts`` sees a
# native iOS shell. ``kind`` is what ``isIOSShell()`` keys off; every other
# member is a guarded no-op so unrelated native calls (badge / notify /
# insets / view-mode / sidebar-drag subscriptions) don't throw under the stub.
_IOS_SHELL_INIT_SCRIPT = """
window.omnigentNative = {
  kind: "ios",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  onNativeInsets: function () { return function () {}; },
  onSidebarDrag: function () { return function () {}; },
  onViewModeChanged: function () { return function () {}; },
  setViewMode: function () {},
  setServerSwitcherHidden: function () {},
  setSidebarOpen: function () {},
};
"""

# Minimal stand-in for the Android WebView bridge (``web/android``'s
# ``NativeBridgeScript``), mirroring ``test_android_shell.py``: ``kind`` is
# what ``isAndroidShell()`` keys off; the rest keep unrelated native calls
# from throwing under the stub.
_ANDROID_SHELL_INIT_SCRIPT = """
window.omnigentNative = {
  kind: "android",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  onNativeInsets: function () { return function () {}; },
};
"""

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


def _box(locator: Locator) -> FloatRect:
    """Return the element's bounding box, failing loudly when it has none.

    :param locator: A locator resolved to exactly one visible element.
    :returns: The element's bounding box.
    """
    box = locator.bounding_box()
    assert box is not None, f"element {locator} has no bounding box"
    return box


def _intersection(a: FloatRect, b: FloatRect) -> tuple[float, float]:
    """Return the (horizontal, vertical) overlap in px between two boxes.

    :param a: First bounding box.
    :param b: Second bounding box.
    :returns: ``(x_overlap, y_overlap)``; both positive iff the boxes intersect.
    """
    x_overlap = min(a["x"] + a["width"], b["x"] + b["width"]) - max(a["x"], b["x"])
    y_overlap = min(a["y"] + a["height"], b["y"] + b["height"]) - max(a["y"], b["y"])
    return (x_overlap, y_overlap)


def _assert_clear_of_tracker(control: Locator, label: str, tracker_box: FloatRect) -> None:
    """Assert a floating header control does not overlap the Plan bar.

    A ≤1px touch is tolerated (border rounding / antialiasing); anything more
    is the reported overlap.

    :param control: The floating control's locator.
    :param label: Human-readable control name for the failure message.
    :param tracker_box: The Plan tracker's bounding box.
    :returns: None.
    """
    control_box = _box(control)
    x_overlap, y_overlap = _intersection(control_box, tracker_box)
    assert not (x_overlap > 1.0 and y_overlap > 1.0), (
        f"{label} overlaps the pinned Plan header by "
        f"{x_overlap:.0f}x{y_overlap:.0f}px: control at "
        f"(x={control_box['x']:.0f}, y={control_box['y']:.0f}, "
        f"w={control_box['width']:.0f}, h={control_box['height']:.0f}), "
        f"plan bar at (x={tracker_box['x']:.0f}, y={tracker_box['y']:.0f}, "
        f"w={tracker_box['width']:.0f}, h={tracker_box['height']:.0f})"
    )


@pytest.mark.parametrize("shell", ["ios", "android"])
def test_native_shell_plan_header_clears_floating_controls(
    page: Page,
    seeded_session: tuple[str, str],
    shell: str,
) -> None:
    """Under a native shell with a top OS inset, the pinned Plan bar and the
    floating header controls must not overlap.

    Emulates the shell (bridge stub → ``data-ios-native`` /
    ``data-android-native``) on a phone-sized viewport with a real safe-area
    inset, seeds a plan through the events route so the Plan header pins at
    the top, and asserts the left sidebar-toggle pill and the right
    session-actions kebab both stay clear of the Plan bar's bounding box.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    :param shell: Which native shell to emulate, ``"ios"`` or ``"android"``.
    :returns: None.
    """
    base_url, session_id = seeded_session

    page.set_viewport_size(_IPHONE_VIEWPORT)
    if shell == "ios":
        page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
        # Emulate the notch: env(safe-area-inset-*) resolves to the device
        # inset, which [data-ios-native] .chat-header folds into its top
        # offset.
        cdp = page.context.new_cdp_session(page)
        cdp.send("Emulation.setSafeAreaInsetsOverride", {"insets": _IPHONE_SAFE_AREA})
    else:
        page.add_init_script(_ANDROID_SHELL_INIT_SCRIPT)

    # A plan exists before the page opens (the report's "session with an
    # active plan"): the snapshot cache seeds the tracker on load.
    _publish_todos(base_url, session_id, _PLAN)
    page.goto(f"{base_url}/c/{session_id}")

    # The shell marker must be live, or the safe-area header shift under
    # test never applies and the assertion would vacuously pass.
    expect(page.locator(".app-shell")).to_have_attribute(f"data-{shell}-native", "true")

    if shell == "android":
        # Android's WebView reports env(safe-area-inset-top) as 0; the native
        # layer measures the OS inset and injects it as a CSS var, which
        # index.css folds into --omnigent-safe-top (see test_android_shell.py).
        page.evaluate(
            "() => document.documentElement.style"
            f".setProperty('--omnigent-android-safe-area-top', '{_IPHONE_SAFE_AREA['top']}px')"
        )

    tracker = page.locator(_TRACKER)
    expect(tracker).to_be_visible(timeout=15_000)
    expect(tracker).to_contain_text("Plan")

    # Left floating control: the sidebar-toggle pill (the sidebar starts
    # collapsed on a phone, so the pill is the only way to open it).
    sidebar_toggle = page.get_by_role("button", name="Open sidebar")
    expect(sidebar_toggle).to_be_visible()
    # Right floating control: the session-actions kebab. Owner-managed
    # sessions render the conversation-actions trigger; sessions without an
    # owner-managed menu render the fallback kebab. Exactly one exists.
    kebab = page.get_by_test_id("header-conversation-actions").or_(
        page.get_by_test_id("session-actions-menu")
    )
    expect(kebab).to_be_visible()

    # Hold the pinned state briefly so the failure is observable in a
    # recording before the geometry assertions run.
    page.wait_for_timeout(1_500)

    tracker_box = _box(tracker)
    _assert_clear_of_tracker(sidebar_toggle, "the sidebar-toggle pill", tracker_box)
    _assert_clear_of_tracker(kebab, "the session-actions kebab", tracker_box)
