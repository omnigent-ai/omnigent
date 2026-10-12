"""E2E: the iOS shell applies safe-area and keyboard layout at iPad widths."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

_IPAD_VIEWPORT = {"width": 1024, "height": 768}
_IPAD_SAFE_AREA = {"top": 24, "left": 0, "bottom": 20, "right": 0}
_KEYBOARD_TOP = 500

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


def test_ios_ipad_keeps_header_and_composer_in_visible_viewport(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Keep floating controls below the status bar and the composer above the keyboard."""
    base_url, session_id = seeded_session
    page.set_viewport_size(_IPAD_VIEWPORT)
    page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
    cdp = page.context.new_cdp_session(page)
    cdp.send("Emulation.setSafeAreaInsetsOverride", {"insets": _IPAD_SAFE_AREA})

    page.goto(f"{base_url}/c/{session_id}")
    app_shell = page.locator(".app-shell")
    expect(app_shell).to_have_attribute("data-ios-native", "true")

    close_sidebar = page.get_by_role("button", name="Close sidebar", exact=True)
    expect(close_sidebar).to_be_visible()
    close_sidebar.click()
    open_sidebar = page.get_by_role("button", name="Open sidebar", exact=True)
    expect(open_sidebar).to_be_visible()

    toggle_box = open_sidebar.bounding_box()
    assert toggle_box is not None
    assert toggle_box["y"] >= _IPAD_SAFE_AREA["top"], (
        f"sidebar toggle starts at y={toggle_box['y']:.0f}, inside the "
        f"{_IPAD_SAFE_AREA['top']}px iPad status-bar safe area"
    )

    composer = page.locator('textarea[aria-label="Message the agent"]')
    expect(composer).to_be_visible(timeout=15_000)
    page.evaluate(
        "height => document.documentElement.style"
        ".setProperty('--omnigent-viewport-height', `${height}px`)",
        _KEYBOARD_TOP,
    )
    expect(app_shell).to_have_css("height", f"{_KEYBOARD_TOP}px")

    composer_box = composer.bounding_box()
    assert composer_box is not None
    composer_bottom = composer_box["y"] + composer_box["height"]
    assert composer_bottom <= _KEYBOARD_TOP + 1, (
        f"composer bottom is y={composer_bottom:.0f}, behind the simulated "
        f"keyboard starting at y={_KEYBOARD_TOP}"
    )


@pytest.mark.parametrize(
    "viewport",
    [{"width": 834, "height": 1210}, {"width": 1210, "height": 834}],
    ids=["portrait", "landscape"],
)
def test_ios_ipad_uses_docked_keyboard_geometry(
    page: Page, seeded_session: tuple[str, str], viewport: dict[str, int]
) -> None:
    """Floating keyboard controls leave the shell full-height; docked keys do not."""
    base_url, session_id = seeded_session
    page.set_viewport_size(viewport)
    page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
    page.add_init_script("""
        let visibleHeight = innerHeight;
        let webHeight = innerHeight;
        const callbacks = new Set();
        window.omnigentNative.getKeyboardViewport = () => ({
            width: innerWidth, height: visibleHeight,
        });
        window.omnigentNative.onKeyboardViewportChanged = callback => {
            callbacks.add(callback);
            return () => callbacks.delete(callback);
        };
        Object.defineProperty(visualViewport, 'height', { get: () => webHeight });
        window.setKeyboardGeometry = (webInset, dockedInset) => {
            webHeight = innerHeight - webInset;
            visibleHeight = innerHeight - dockedInset;
            visualViewport.dispatchEvent(new Event('resize'));
            callbacks.forEach(callback => callback());
        };
    """)
    page.goto(f"{base_url}/c/{session_id}")
    shell = page.locator(".app-shell")
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_visible()
    composer.fill("Keyboard layout regression")
    before = composer.bounding_box()
    assert before is not None

    page.evaluate("setKeyboardGeometry(149, 0)")
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
    expect(shell).to_have_css("height", f"{viewport['height']}px")
    after = composer.bounding_box()
    assert after is not None
    assert abs(after["y"] - before["y"]) <= 1

    keyboard_top = viewport["height"] - 370
    page.evaluate("setKeyboardGeometry(370, 370)")
    expect(shell).to_have_css("height", f"{keyboard_top}px")
    above_keyboard = composer.bounding_box()
    assert above_keyboard is not None
    assert above_keyboard["y"] + above_keyboard["height"] <= keyboard_top

    page.evaluate("setKeyboardGeometry(0, 0)")
    expect(shell).to_have_css("height", f"{viewport['height']}px")
    expect(composer).to_have_value("Keyboard layout regression")
