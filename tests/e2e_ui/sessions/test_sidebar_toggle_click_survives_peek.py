"""A raw pointer click on the sidebar toggle still pins it open during hover-preview.

The real SPA must keep the session URL and leave the sidebar open after the pointer moves."""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

_CONVERSATIONS = 'aside[aria-label="Conversations"]'
_LEFT_CHORD = "Control+Alt+BracketLeft"
_PEEK_CLASS = re.compile(r"(^|\s)is-peek(\s|$)")
# The collapsed-state toggle in the chat header — outside the macOS Electron
# shell this is the ONLY way to reopen a collapsed sidebar with the pointer.
_TOGGLE = "header button.chat-header-sidebar-toggle"


def test_sidebar_toggle_opens_after_peek_appears(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A click on the sidebar toggle pins the sidebar open even mid-peek."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    # Use the composer as readiness signal; the SSE stream prevents network idle.
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)

    conversations = page.locator(_CONVERSATIONS)
    # Collapse the desktop sidebar to expose the header toggle.
    expect(conversations).not_to_have_attribute("data-collapsed", "true")
    page.keyboard.press(
        "Meta+Alt+BracketLeft"
        if page.evaluate("navigator.platform.includes('Mac')")
        else _LEFT_CHORD
    )
    expect(conversations).to_have_attribute("data-collapsed", "true")

    toggle = page.locator(_TOGGLE)
    expect(toggle).to_be_visible()
    box = toggle.bounding_box()
    assert box is not None, "sidebar toggle has no layout box"
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2

    # Wait for the hover-preview before clicking the original pointer target.
    page.mouse.move(cx, cy)
    expect(conversations).to_have_class(_PEEK_CLASS, timeout=5_000)

    page.mouse.down()
    page.mouse.up()

    expect(page).to_have_url(f"{base_url}/c/{session_id}")

    # The sidebar must remain docked after the pointer leaves.
    expect(conversations).not_to_have_class(_PEEK_CLASS)
    expect(conversations).not_to_have_attribute("data-collapsed", "true")
    page.mouse.move(640, 420)
    page.wait_for_timeout(500)
    expect(conversations).not_to_have_attribute("data-collapsed", "true")
