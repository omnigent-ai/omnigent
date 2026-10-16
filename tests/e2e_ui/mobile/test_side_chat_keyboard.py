"""Verify the iOS side-chat drawer keeps its composer above the soft keyboard.

A bridge stub stands in for the iOS shell and its keyboard viewport API.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Browser, Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm

_VIEWPORT = {"width": 390, "height": 844}

# Representative iPhone portrait soft-keyboard height in CSS px.
_KEYBOARD_HEIGHT = 336

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'

# Minimal stand-in for the iOS WKWebView bridge, including its keyboard
# viewport API. ``__setKeyboardHeight`` plays UIKit's keyboard-frame updates.
_IOS_SHELL_INIT_SCRIPT = """
(() => {
  const listeners = new Set();
  let keyboardHeight = 0;
  window.omnigentNative = {
    kind: "ios",
    setBadgeCount: function () {},
    notify: function () { return Promise.resolve(false); },
    onNotificationActivated: function () { return function () {}; },
    onOpenPath: function () { return function () {}; },
    onSidebarDrag: function () { return function () {}; },
    setServerSwitcherHidden: function () {},
    setViewMode: function () {},
    onViewModeChanged: function () { return function () {}; },
    setDocumentScrollEnabled: function () {},
    getKeyboardViewport: function () {
      return { width: window.innerWidth, height: window.innerHeight - keyboardHeight };
    },
    onKeyboardViewportChanged: function (callback) {
      listeners.add(callback);
      return function () { listeners.delete(callback); };
    },
  };
  window.__setKeyboardHeight = (height) => {
    keyboardHeight = height;
    for (const callback of [...listeners]) callback();
  };
})();
"""


def _set_keyboard_height(page: Page, height: int) -> None:
    """Publish a keyboard frame, then wait for the app's coalesced frame and its render."""
    page.evaluate(
        """(height) => new Promise((resolve) => {
          window.__setKeyboardHeight(height);
          requestAnimationFrame(() => requestAnimationFrame(resolve));
        })""",
        height,
    )


def test_side_chat_input_stays_above_ios_keyboard(
    browser: Browser,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """With the keyboard up, the focused side-chat input and Send stay visible above it."""
    base_url, session_id = seeded_session
    question = f"kb-side-{session_id}: what changed?"
    reply = "The side chat answered above the keyboard."
    configure_mock_llm(mock_llm_server_url, [{"text": reply}], match=f"kb-side-{session_id}")

    child_ids: list[str] = []

    def track_fork(route: Route) -> None:
        response = route.fetch()
        if response.ok:
            child_ids.append(response.json()["id"])
        route.fulfill(response=response)

    context = browser.new_context(viewport=_VIEWPORT, has_touch=True, is_mobile=True)
    try:
        page = context.new_page()
        page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
        page.route(f"**/v1/sessions/{session_id}/fork", track_fork)
        page.goto(f"{base_url}/c/{session_id}")
        composer = page.get_by_placeholder("Send a message…")
        expect(composer).to_be_visible(timeout=60_000)
        expect(page.locator(".app-shell")).to_have_attribute("data-ios-native", "true")

        composer.fill(f"/side {question}")
        page.get_by_role("button", name="Send", exact=True).click()
        drawer = page.get_by_test_id("side-chats-panel-drawer")
        expect(drawer).to_have_attribute("data-state", "open")
        expect(drawer.locator(_ASSISTANT).filter(has_text=reply)).to_be_visible(timeout=30_000)

        side_input = drawer.get_by_test_id("side-chat-input")
        side_input.tap()
        expect(side_input).to_be_focused()
        _set_keyboard_height(page, _KEYBOARD_HEIGHT)
        keyboard_top = _VIEWPORT["height"] - _KEYBOARD_HEIGHT

        side_input.press_sequentially("typed")
        expect(side_input).to_have_value("typed")
        input_box = side_input.bounding_box()
        send_box = drawer.get_by_test_id("side-chat-send").bounding_box()
        header_box = drawer.get_by_role("heading", name="Side chats").bounding_box()
        assert input_box is not None and send_box is not None and header_box is not None
        print(f"[side-chat-kb] keyboard top {keyboard_top}; input {input_box}; send {send_box}")
        assert input_box["y"] + input_box["height"] <= keyboard_top, (
            f"side-chat input extends under the keyboard (top {keyboard_top}): {input_box}"
        )
        assert send_box["y"] + send_box["height"] <= keyboard_top, (
            f"side-chat Send extends under the keyboard (top {keyboard_top}): {send_box}"
        )
        assert header_box["y"] >= 0, f"drawer header moved off-screen: {header_box}"

        # Dismissing the keyboard returns the composer to the bottom edge.
        _set_keyboard_height(page, 0)
        lowered = side_input.bounding_box()
        assert lowered is not None
        assert lowered["y"] > input_box["y"] + _KEYBOARD_HEIGHT / 2, (
            f"composer did not return to the bottom after the keyboard closed: {lowered}"
        )
    finally:
        context.close()
        for child_id in child_ids:
            httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0).raise_for_status()
