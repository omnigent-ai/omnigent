"""Narrow-window layout beside the macOS desktop shell's window controls.

Electron lets the window shrink to 480px, below the SPA's 1181px mobile-layout
breakpoint. The desktop app keeps the desktop layout there. These tests emulate
the macOS shell in Chromium, as
``tests/e2e_ui/sessions/test_session_search.py`` does.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail
from tests.e2e_ui.desktop._mac_shell import (
    MAC_SHELL_INIT_SCRIPT,
    WINDOW_CONTROLS_STRIP,
    intersects_window_controls,
)
from tests.e2e_ui.files._files_page import seeded_workspace
from tests.e2e_ui.sessions.test_workspace_sidebar_v2 import _set_flag

_WIDE_VIEWPORT = {"width": 1280, "height": 860}
_NARROW_VIEWPORT = {"width": 480, "height": 860}

# Electron applies app-region rects in document order, ignoring stacking, opacity
# and pointer-events, so the last rendered region over a point decides dragging.
_LAST_APP_REGION_JS = """
  const appRegion = (element) => getComputedStyle(element).getPropertyValue("-webkit-app-region");
  const regions = [...document.querySelectorAll("*")].flatMap((element) => {
    const region = appRegion(element);
    const rect = element.getBoundingClientRect();
    const rendered =
      getComputedStyle(element).visibility === "visible" && rect.width && rect.height;
    const hasRegion = region === "drag" || region === "no-drag";
    return hasRegion && rendered ? [{ element, region, rect }] : [];
  });
  const regionAt = (x, y) => {
    const covering = regions.filter(
      ({ rect }) => x >= rect.left && x < rect.right && y >= rect.top && y < rect.bottom,
    ).at(-1);
    const element = covering?.element;
    const coveredBy = element ? element.dataset.testid || element.tagName : null;
    return { x, y, region: covering?.region ?? null, coveredBy };
  };
"""

_EMPTY_TITLE_BAR_POINTS_SCRIPT = (
    """
(stripHeight) => {"""
    + _LAST_APP_REGION_JS
    + """
  let empty = 0;
  const undraggable = [];
  for (let x = 4; x < window.innerWidth; x += 16) {
    for (const y of [8, 24, stripHeight - 4]) {
      const isEmpty = document.elementsFromPoint(x, y).every((el) => appRegion(el) !== "no-drag");
      if (!isEmpty) continue;
      empty += 1;
      const { region, coveredBy } = regionAt(x, y);
      if (region !== "drag") undraggable.push({ x, y, coveredBy });
    }
  }
  return { empty, undraggable };
}
"""
)

# Samples the maximized rail's tab bar: its tabs and trailing controls, the strip above
# the tabs, and the empty space between the last tab and the first trailing control.
_WORKSPACE_TAB_BAR_REGIONS_SCRIPT = (
    """
({ tabLabels, controlLabels }) => {"""
    + _LAST_APP_REGION_JS
    + """
  const toolbar = document.querySelector('[role="toolbar"][aria-label="Workspace tabs"]');
  const box = (label) => toolbar.querySelector(`[aria-label="${label}"]`)?.getBoundingClientRect();
  const tabs = tabLabels.map(box);
  const controls = controlLabels.map(box).filter(Boolean);
  const emptyStart = Math.max(...tabs.map((rect) => rect.right));
  const emptyEnd = Math.min(...controls.map((rect) => rect.left));
  if (![emptyStart, emptyEnd].every(Number.isFinite)) throw new Error("tab-bar bounds not found");
  const bar = toolbar.getBoundingClientRect();
  const middle = Math.round(bar.top + bar.height / 2);
  const center = (rect) => Math.round(rect.left + rect.width / 2);
  const emptySpace = [];
  for (let x = Math.ceil(emptyStart) + 8; x < emptyEnd - 8; x += 16) {
    emptySpace.push(regionAt(x, middle));
  }
  return {
    tabs: tabs.map((rect) => regionAt(center(rect), middle)),
    controls: controls.map((rect) => regionAt(center(rect), middle)),
    stripAboveTabs: tabs.map((rect) => regionAt(center(rect), bar.top + 8)),
    emptySpace,
  };
}
"""
)

# Samples the tabs sidebar's strip between its last control before Exit full screen and
# Exit full screen itself.
_TABS_SIDEBAR_STRIP_EMPTY_SPACE_SCRIPT = (
    """
() => {"""
    + _LAST_APP_REGION_JS
    + """
  const toolbar = document.querySelector('[role="toolbar"][aria-label="Workspace tabs"]');
  const exit = toolbar.querySelector('[aria-label="Exit full screen"]').getBoundingClientRect();
  const leading = [...toolbar.querySelectorAll("button")]
    .map((button) => button.getBoundingClientRect())
    .filter((rect) => rect.width && rect.right <= exit.left);
  const emptyStart = Math.max(...leading.map((rect) => rect.right));
  if (!Number.isFinite(emptyStart)) throw new Error("no toolbar buttons before Exit full screen");
  const bar = toolbar.getBoundingClientRect();
  const middle = Math.round(bar.top + bar.height / 2);
  const emptySpace = [];
  for (let x = Math.ceil(emptyStart) + 8; x < exit.left - 8; x += 16) {
    emptySpace.push(regionAt(x, middle));
  }
  return emptySpace;
}
"""
)

_WORKSPACE_TAB_FILES = ("tab_bar_notes.md", "tab_bar_design.md")
_TAB_BAR_TRAILING_CONTROLS = ("Open new", "Select panel", "Exit full screen")


def _undraggable_empty_title_bar_points(page: Page) -> list[dict[str, object]]:
    sample = page.evaluate(_EMPTY_TITLE_BAR_POINTS_SCRIPT, WINDOW_CONTROLS_STRIP["height"])
    assert sample["empty"] > 0, "no empty title-bar points were sampled"
    return sample["undraggable"]


def _hold_for_recording(page: Page) -> None:
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(2000)


def _expect_chat_header_after_exiting_full_screen(page: Page, rail: Locator) -> None:
    rail.get_by_role("button", name="Exit full screen", exact=True).click()
    expect(page.locator("header.chat-header")).to_be_visible()


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
          return rect.x === 0 && rect.width > 0 && rect.width <= windowWidth / 2;
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


@pytest.fixture
def session_with_workspace_files(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    files = {name: f"# {name}\n" for name in _WORKSPACE_TAB_FILES}
    with seeded_workspace(seeded_session, files) as session:
        yield session


def test_maximized_workspace_tab_bar_empty_space_drags_the_window(
    request: pytest.FixtureRequest, session_with_workspace_files: tuple[str, str]
) -> None:
    base_url, session_id = session_with_workspace_files
    page = _open_session_in_mac_shell(request, base_url, session_id)
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    for name in _WORKSPACE_TAB_FILES:
        rail.get_by_role("tab", name="Files", exact=True).click()
        rail.get_by_role("button", name=re.compile(rf"^{re.escape(name)}\b")).click()
        expect(page.get_by_role("button", name=f"Close {name}", exact=True).first).to_be_visible()
    page.get_by_role("button", name="Full screen", exact=True).click()
    expect(page.locator('aside[aria-label="Workspace"][data-maximized]')).to_be_visible()
    _hold_for_recording(page)

    regions = page.evaluate(
        _WORKSPACE_TAB_BAR_REGIONS_SCRIPT,
        {
            "tabLabels": list(_WORKSPACE_TAB_FILES),
            "controlLabels": list(_TAB_BAR_TRAILING_CONTROLS),
        },
    )
    assert all(point["region"] == "no-drag" for point in regions["tabs"]), (
        f"open tabs would move the window instead of selecting: {regions['tabs']}"
    )
    assert len(regions["controls"]) == len(_TAB_BAR_TRAILING_CONTROLS), regions["controls"]
    assert all(point["region"] == "no-drag" for point in regions["controls"]), (
        f"trailing controls would move the window instead of clicking: {regions['controls']}"
    )
    assert all(point["region"] == "drag" for point in regions["stripAboveTabs"]), (
        f"strip above the tabs does not drag the window: {regions['stripAboveTabs']}"
    )
    assert regions["emptySpace"], "no empty tab-bar space was sampled"
    undraggable = [point for point in regions["emptySpace"] if point["region"] != "drag"]
    assert undraggable == [], f"empty tab-bar space does not drag the window: {undraggable}"
    _expect_chat_header_after_exiting_full_screen(page, rail)


def test_maximized_tabs_sidebar_strip_empty_space_drags_the_window(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str]
) -> None:
    base_url, session_id = seeded_session
    _set_flag(request.getfixturevalue("page"), True)
    page = _open_session_in_mac_shell(request, base_url, session_id)
    open_right_rail(page)
    rail = page.locator('aside[aria-label="Workspace"][data-workspace-design="tabs"]')
    rail.get_by_role("button", name="Full screen", exact=True).click()
    expect(rail).to_have_attribute("data-maximized", "true")
    _hold_for_recording(page)

    empty_space = page.evaluate(_TABS_SIDEBAR_STRIP_EMPTY_SPACE_SCRIPT)
    assert empty_space, "no empty tab-bar space was sampled"
    undraggable = [point for point in empty_space if point["region"] != "drag"]
    assert undraggable == [], f"empty tab-bar space does not drag the window: {undraggable}"
    _expect_chat_header_after_exiting_full_screen(page, rail)
