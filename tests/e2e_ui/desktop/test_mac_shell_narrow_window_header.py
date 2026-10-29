"""Narrow-window layout beside the macOS desktop shell's window controls.

Electron lets the window shrink to 720px, below the SPA's 768px phone-layout
breakpoint. These tests emulate the macOS shell in Chromium, as
``tests/e2e_ui/sessions/test_session_search.py`` does.
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

_WIDE_VIEWPORT = {"width": 1280, "height": 860}
_NARROW_VIEWPORT = {"width": 740, "height": 860}

# macOS paints the traffic lights outside the DOM; index.css reserves this strip for them.
_WINDOW_CONTROLS_STRIP = {"x": 0, "y": 0, "width": 88, "height": 48}


def _intersects_window_controls(box: dict[str, float] | None) -> bool:
    assert box is not None and box["width"] > 0 and box["height"] > 0, f"no rendered box: {box}"
    strip = _WINDOW_CONTROLS_STRIP
    return (
        box["x"] < strip["x"] + strip["width"]
        and strip["x"] < box["x"] + box["width"]
        and box["y"] < strip["y"] + strip["height"]
        and strip["y"] < box["y"] + box["height"]
    )


def _hold_for_recording(page: Page) -> None:
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(2000)


def _open_session_in_mac_shell(
    request: pytest.FixtureRequest, base_url: str, session_id: str
) -> Page:
    page: Page = request.getfixturevalue("page")
    page.add_init_script(_MAC_SHELL_INIT_SCRIPT)
    page.set_viewport_size(_WIDE_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=30_000)
    expect(page.locator("html")).to_have_attribute("data-electron-mac", "true")
    return page


def _shrink_window_and_open_sidebar(page: Page) -> Locator:
    # A CSS locator: the collapsed sidebar is aria-hidden, so a role query would miss it.
    aside = page.locator('aside[aria-label="Conversations"]')
    page.locator(".electron-sidebar-header-actions").get_by_role(
        "button", name="Close sidebar", exact=True
    ).click()
    expect(aside).to_have_attribute("data-collapsed", "true")

    page.set_viewport_size(_NARROW_VIEWPORT)
    open_sidebar = page.get_by_role("button", name="Open sidebar", exact=True)
    expect(open_sidebar).to_be_visible()
    assert not _intersects_window_controls(open_sidebar.bounding_box()), (
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
    expect(brand).to_be_visible()
    assert not _intersects_window_controls(brand.bounding_box()), (
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

    page.get_by_role("button", name="Search", exact=True).filter(visible=True).first.click()
    palette = page.get_by_role("dialog", name="Command palette", exact=True)
    search_field = palette.get_by_role("combobox")
    expect(search_field).to_be_visible()
    expect(palette).to_have_css("transform", "none")
    _hold_for_recording(page)

    assert not _intersects_window_controls(search_field.bounding_box()), (
        f"palette search field {search_field.bounding_box()} sits under the window controls"
    )
    sheet = palette.bounding_box()
    assert sheet is not None and abs(sheet["x"]) < 1, f"palette sheet {sheet} is not flush left"
    assert abs(sheet["width"] - _NARROW_VIEWPORT["width"]) < 1, (
        f"palette sheet {sheet} does not span the {_NARROW_VIEWPORT['width']}px window"
    )


def test_narrow_window_settings_back_row_clears_window_controls(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str]
) -> None:
    base_url, session_id = seeded_session
    page = _open_session_in_mac_shell(request, base_url, session_id)
    aside = _shrink_window_and_open_sidebar(page)

    aside.get_by_test_id("sidebar-settings-float").click()
    back = page.get_by_role("link", name="Back", exact=True)
    expect(back).to_be_visible()
    _hold_for_recording(page)

    assert not _intersects_window_controls(back.bounding_box()), (
        f"settings Back link {back.bounding_box()} sits under the window controls"
    )


def test_narrowing_window_collapses_open_sidebar(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str]
) -> None:
    base_url, session_id = seeded_session
    page = _open_session_in_mac_shell(request, base_url, session_id)
    aside = page.locator('aside[aria-label="Conversations"]')
    expect(aside).not_to_have_attribute("data-collapsed", "true")

    page.set_viewport_size(_NARROW_VIEWPORT)
    _hold_for_recording(page)

    expect(aside).to_have_attribute("data-collapsed", "true")
    composer = page.get_by_role("textbox", name="Message the agent")
    composer.click(timeout=5_000)
    expect(composer).to_be_focused()
