"""Narrow-window layout beside the macOS desktop shell's window controls.

Electron lets the window shrink to 720px, below the SPA's 1181px mobile-layout
breakpoint. The desktop app keeps the desktop layout there. These tests emulate
the macOS shell in Chromium, as
``tests/e2e_ui/sessions/test_session_search.py`` does.
"""

from __future__ import annotations

import os

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.desktop._mac_shell import (
    MAC_SHELL_INIT_SCRIPT,
    WINDOW_CONTROLS_STRIP,
    intersects_window_controls,
)

_WIDE_VIEWPORT = {"width": 1280, "height": 860}
_NARROW_VIEWPORT = {"width": 740, "height": 860}

# Electron applies app-region rects in document order, ignoring stacking, opacity
# and pointer-events, so the last rendered region over a point decides dragging.
_EMPTY_TITLE_BAR_POINTS_SCRIPT = """
(stripHeight) => {
  const appRegion = (element) => getComputedStyle(element).getPropertyValue("-webkit-app-region");
  const regions = [...document.querySelectorAll("*")].flatMap((element) => {
    const region = appRegion(element);
    const rect = element.getBoundingClientRect();
    const rendered =
      getComputedStyle(element).visibility === "visible" && rect.width && rect.height;
    const hasRegion = region === "drag" || region === "no-drag";
    return hasRegion && rendered ? [{ element, region, rect }] : [];
  });
  let empty = 0;
  const undraggable = [];
  for (let x = 4; x < window.innerWidth; x += 16) {
    for (const y of [8, 24, stripHeight - 4]) {
      const isEmpty = document.elementsFromPoint(x, y).every((el) => appRegion(el) !== "no-drag");
      if (!isEmpty) continue;
      empty += 1;
      const covering = regions.filter(
        ({ rect }) => x >= rect.left && x < rect.right && y >= rect.top && y < rect.bottom,
      ).at(-1);
      if (covering?.region !== "drag") {
        const element = covering?.element;
        const coveredBy = element ? element.dataset.testid || element.tagName : null;
        undraggable.push({ x, y, coveredBy });
      }
    }
  }
  return { empty, undraggable };
}
"""


def _undraggable_empty_title_bar_points(page: Page) -> list[dict[str, object]]:
    sample = page.evaluate(_EMPTY_TITLE_BAR_POINTS_SCRIPT, WINDOW_CONTROLS_STRIP["height"])
    assert sample["empty"] > 0, "no empty title-bar points were sampled"
    return sample["undraggable"]


def _hold_for_recording(page: Page) -> None:
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(2000)


def _open_session_in_mac_shell(
    request: pytest.FixtureRequest, base_url: str, session_id: str
) -> Page:
    page: Page = request.getfixturevalue("page")
    page.add_init_script(MAC_SHELL_INIT_SCRIPT)
    page.set_viewport_size(_WIDE_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=30_000)
    expect(page.locator("html")).to_have_attribute("data-electron-mac", "true")
    return page


def _shrink_window(page: Page) -> Locator:
    """Narrow the window below the mobile breakpoint; the docked sidebar must stay."""
    page.set_viewport_size(_NARROW_VIEWPORT)
    # A CSS locator, matching how the collapsed (aria-hidden) sidebar is found elsewhere.
    aside = page.locator('aside[aria-label="Conversations"]')
    _expect_docked_sidebar(page, aside)
    return aside


def _expect_docked_sidebar(page: Page, aside: Locator) -> None:
    expect(aside).not_to_have_attribute("data-collapsed", "true")
    # Docked beside the chat, not the mobile layout's full-window drawer.
    page.wait_for_function(
        """(windowWidth) => {
          const rect = document
            .querySelector('aside[aria-label="Conversations"]')
            .getBoundingClientRect();
          return rect.x === 0 && rect.width > 0 && rect.width < windowWidth / 2;
        }""",
        arg=_NARROW_VIEWPORT["width"],
    )


def test_narrow_window_keeps_the_desktop_layout(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str]
) -> None:
    base_url, session_id = seeded_session
    page = _open_session_in_mac_shell(request, base_url, session_id)
    expect(page.locator("html")).to_have_attribute("data-desktop-app", "true")
    aside = _shrink_window(page)
    _hold_for_recording(page)

    # The title-bar cluster beside the lights stays; the phone header's toggle does not appear.
    title_bar_actions = page.locator(".electron-sidebar-header-actions")
    close_sidebar = title_bar_actions.get_by_role("button", name="Close sidebar", exact=True)
    expect(close_sidebar).to_be_visible()
    assert not intersects_window_controls(close_sidebar.bounding_box()), (
        f"Close sidebar button {close_sidebar.bounding_box()} sits under the window controls"
    )
    expect(page.locator(".chat-header-sidebar-toggle")).to_be_hidden()
    brand = aside.get_by_test_id("sidebar-brand")
    expect(brand).to_be_visible()
    assert not intersects_window_controls(brand.bounding_box()), (
        f"sidebar wordmark {brand.bounding_box()} sits under the window controls"
    )
    expect(
        page.get_by_role("button", name="Search", exact=True).filter(visible=True)
    ).to_have_count(1)

    # Plain Enter sends, as in a wide window; the mobile layout makes it a newline.
    composer = page.get_by_role("textbox", name="Message the agent")
    composer.click(timeout=5_000)
    composer.fill("sent from a narrow window")
    composer.press("Enter")
    expect(composer).to_have_value("")
    expect(page.get_by_text("sent from a narrow window", exact=True).first).to_be_visible(
        timeout=30_000
    )

    close_sidebar.click()
    expect(aside).to_have_attribute("data-collapsed", "true")
    open_sidebar = title_bar_actions.get_by_role("button", name="Open sidebar", exact=True)
    expect(open_sidebar).to_be_visible()
    assert not intersects_window_controls(open_sidebar.bounding_box()), (
        f"Open sidebar button {open_sidebar.bounding_box()} sits under the window controls"
    )
    open_sidebar.click()
    _expect_docked_sidebar(page, aside)


def test_narrow_window_command_palette_clears_window_controls(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str]
) -> None:
    base_url, session_id = seeded_session
    page = _open_session_in_mac_shell(request, base_url, session_id)
    _shrink_window(page)

    page.get_by_role("button", name="Search", exact=True).filter(visible=True).first.click()
    palette = page.get_by_role("dialog", name="Command palette", exact=True)
    search_field = palette.get_by_role("combobox")
    expect(search_field).to_be_visible()
    _hold_for_recording(page)

    assert not intersects_window_controls(search_field.bounding_box()), (
        f"palette search field {search_field.bounding_box()} sits under the window controls"
    )
    # The desktop palette is a centered dialog, not the mobile layout's full-window sheet.
    sheet = palette.bounding_box()
    assert sheet is not None and sheet["width"] < _NARROW_VIEWPORT["width"], (
        f"palette {sheet} spans the {_NARROW_VIEWPORT['width']}px window like the mobile sheet"
    )


def test_narrow_window_settings_back_row_clears_window_controls(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str]
) -> None:
    base_url, session_id = seeded_session
    page = _open_session_in_mac_shell(request, base_url, session_id)
    aside = _shrink_window(page)

    aside.get_by_test_id("sidebar-profile-settings").click()
    back = page.get_by_role("link", name="Back", exact=True)
    expect(back).to_be_visible()
    _hold_for_recording(page)

    assert not intersects_window_controls(back.bounding_box()), (
        f"settings Back link {back.bounding_box()} sits under the window controls"
    )


def test_narrow_window_title_bar_stays_draggable(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str]
) -> None:
    base_url, session_id = seeded_session
    page = _open_session_in_mac_shell(request, base_url, session_id)
    wide_points = _undraggable_empty_title_bar_points(page)
    assert wide_points == [], (
        f"empty title-bar points not draggable at {_WIDE_VIEWPORT['width']}px: {wide_points}"
    )

    _shrink_window(page)
    _hold_for_recording(page)

    narrow_points = _undraggable_empty_title_bar_points(page)
    assert narrow_points == [], (
        f"empty title-bar points not draggable at {_NARROW_VIEWPORT['width']}px: {narrow_points}"
    )
