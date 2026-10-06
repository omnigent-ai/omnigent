"""Narrow-window layout of the macOS shell's title-bar strip.

The macOS Electron shell hides the native title bar and the SPA pins a
Search/Settings/toggle cluster beside the traffic lights (the ``[data-electron-mac]``
rules in ``web/src/index.css``). Electron lets the window shrink to 720px, below
the SPA's ``md`` breakpoint (768px), where the phone layout takes over. These tests
emulate the shell in Chromium like ``tests/e2e_ui/sessions/test_session_search.py``
and check that the sidebar header, the settings Back row and the command palette
stay out of the strip the CSS reserves for the window controls.
"""

from __future__ import annotations

import os

import pytest
from playwright.sync_api import Locator, Page, expect

_MAC_SHELL_INIT_SCRIPT = """
    Object.defineProperty(navigator, "platform", { value: "MacIntel" });
    Object.defineProperty(navigator, "userAgentData", { value: { platform: "macOS" } });
    Object.defineProperty(navigator, "userAgent", { value: "Mozilla/5.0 (Macintosh)" });
    window.omnigentDesktop = {
        kind: "electron",
        setBadgeCount() {},
        notify() { return Promise.resolve(false); },
        onNotificationActivated() { return () => {}; },
        getServerPicker() { return Promise.resolve(null); },
        switchServer() { return Promise.resolve(); },
        openServerSetup() {},
    };
"""

# Below the SPA's md breakpoint but above Electron's 720px window minimum.
_NARROW_VIEWPORT = {"width": 740, "height": 820}
_WIDE_VIEWPORT = {"width": 1100, "height": 820}

# The traffic lights are painted by macOS outside the DOM; index.css reserves
# the top-left 5.5rem x 2.25rem of the window for them.
_WINDOW_CONTROLS_STRIP = {"x": 0, "y": 0, "width": 88, "height": 36}


def _intersects(box: dict[str, float] | None, other: dict[str, float]) -> bool:
    if box is None or box["width"] == 0 or box["height"] == 0:
        return False
    return (
        box["x"] < other["x"] + other["width"]
        and other["x"] < box["x"] + box["width"]
        and box["y"] < other["y"] + other["height"]
        and other["y"] < box["y"] + box["height"]
    )


def _hold_for_recording(page: Page) -> None:
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(1500)


def _open_session_in_mac_shell(
    request: pytest.FixtureRequest, base_url: str, session_id: str
) -> Page:
    page: Page = request.getfixturevalue("page")
    page.add_init_script(_MAC_SHELL_INIT_SCRIPT)
    page.set_viewport_size(_WIDE_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=30_000)
    expect(page.locator(".app-shell[data-electron-mac='true']")).to_be_attached()
    return page


def _shrink_window_and_open_sidebar(page: Page) -> Locator:
    # A CSS locator: the collapsed sidebar is aria-hidden, so a role query would not find it.
    aside = page.locator('aside[aria-label="Conversations"]')
    title_bar = page.locator(".electron-sidebar-header-actions")
    title_bar.get_by_role("button", name="Close sidebar", exact=True).click()
    expect(aside).to_have_attribute("data-collapsed", "true")

    page.set_viewport_size(_NARROW_VIEWPORT)
    # With the drawer closed the chat header's own toggle is the way back in.
    open_sidebar = page.get_by_role("button", name="Open sidebar", exact=True)
    expect(open_sidebar).to_be_visible()
    assert not _intersects(open_sidebar.bounding_box(), _WINDOW_CONTROLS_STRIP), (
        f"Open sidebar button {open_sidebar.bounding_box()} sits under the window controls"
    )
    open_sidebar.click()
    expect(aside).not_to_have_attribute("data-collapsed", "true")
    page.wait_for_function(
        "() => document.querySelector('aside[aria-label=\"Conversations\"]')"
        ".getBoundingClientRect().x === 0"
    )
    return aside


def test_narrow_window_sidebar_header_stays_clear_of_window_controls(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str]
) -> None:
    base_url, session_id = seeded_session
    page = _open_session_in_mac_shell(request, base_url, session_id)
    aside = _shrink_window_and_open_sidebar(page)
    _hold_for_recording(page)

    brand = aside.get_by_test_id("sidebar-brand")
    assert not _intersects(brand.bounding_box(), _WINDOW_CONTROLS_STRIP), (
        f"sidebar wordmark {brand.bounding_box()} sits under the window controls"
    )
    expect(
        page.get_by_role("button", name="Search", exact=True).filter(visible=True)
    ).to_have_count(1)


def test_narrow_window_command_palette_clears_window_controls(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str]
) -> None:
    base_url, session_id = seeded_session
    page = _open_session_in_mac_shell(request, base_url, session_id)
    _shrink_window_and_open_sidebar(page)

    page.keyboard.press("Meta+KeyK")
    palette = page.get_by_role("dialog", name="Command palette", exact=True)
    expect(palette).to_be_visible()
    search_field = palette.get_by_role("combobox")
    expect(search_field).to_be_visible()
    _hold_for_recording(page)

    # Let the dialog's zoom-in animation settle so bounding boxes are at rest.
    expect(palette).to_have_css("transform", "none")

    # Below md the palette is a full-screen sheet; in the 640-767px band it must
    # not fall back to the dialog's default 24rem card pinned to the left edge.
    sheet = palette.bounding_box()
    assert sheet is not None and abs(sheet["x"]) < 1, f"palette sheet {sheet} is not flush left"
    assert abs(sheet["width"] - _NARROW_VIEWPORT["width"]) < 1, (
        f"palette sheet {sheet} does not span the {_NARROW_VIEWPORT['width']}px window"
    )
    assert not _intersects(search_field.bounding_box(), _WINDOW_CONTROLS_STRIP), (
        f"palette search field {search_field.bounding_box()} sits under the window controls"
    )


def test_narrow_window_settings_back_row_clears_window_controls(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str]
) -> None:
    base_url, session_id = seeded_session
    page = _open_session_in_mac_shell(request, base_url, session_id)
    aside = _shrink_window_and_open_sidebar(page)

    # The phone drawer floats Settings at its foot; entering Settings swaps the
    # drawer's content for the settings nav, whose Back row starts at the top.
    aside.get_by_test_id("sidebar-settings-float").click()
    back = page.get_by_role("link", name="Back", exact=True)
    expect(back).to_be_visible()
    _hold_for_recording(page)

    assert not _intersects(back.bounding_box(), _WINDOW_CONTROLS_STRIP), (
        f"settings Back link {back.bounding_box()} sits under the window controls"
    )
