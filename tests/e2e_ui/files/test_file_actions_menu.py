"""Browser coverage for the shared session file-row actions."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail

_FILE_NAME = "row actions Ω report.txt"
_FILE_CONTENT = "file-row-actions-menu-e2e-content"


def _seed_file(page: Page, base_url: str, session_id: str, request: pytest.FixtureRequest) -> Path:
    """Create a predictable file in this session's workspace."""
    response = page.request.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem"
    )
    assert response.status == 200, response.text()
    target = Path(response.json()["base"]) / _FILE_NAME
    target.write_text(_FILE_CONTENT, encoding="utf-8")
    request.addfinalizer(lambda: target.unlink(missing_ok=True))
    return target


def _row(panel: Locator) -> Locator:
    row = panel.locator('[data-slot="context-menu-trigger"]').filter(has_text=_FILE_NAME).last
    expect(row).to_be_visible(timeout=30_000)
    return row


def test_file_row_context_menu_kebab_info_and_copy(
    page: Page,
    seeded_session: tuple[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Exercise pointer, keyboard, Info, and Copy path entry points."""
    base_url, session_id = seeded_session
    _seed_file(page, base_url, session_id, request)
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base_url)
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    panel = page.get_by_role("complementary", name="Workspace")
    panel.get_by_role("tab", name="Files").click()
    row = _row(panel)

    row.click(button="right")
    expect(page.get_by_role("menuitem", name="Download")).to_be_visible()
    expect(page.get_by_role("menuitem", name="Copy path")).to_be_visible()
    expect(page.get_by_role("menuitem", name="File info")).to_be_visible()
    expect(
        page.get_by_role("menuitem", name=re.compile("Finder|File Explorer|file manager"))
    ).to_have_count(0)
    page.get_by_role("menuitem", name="File info").click()

    info = page.get_by_role("dialog", name="File info")
    expect(info).to_contain_text(_FILE_NAME)
    expect(info).to_contain_text(f"{len(_FILE_CONTENT)} B")
    expect(info).to_contain_text("file")
    expect(info).to_contain_text("Changes")
    info.get_by_role("button", name="Close").click()
    expect(row).to_be_focused()

    row.focus()
    row.press("ContextMenu")
    expect(page.get_by_role("menuitem", name="File info")).to_be_visible()
    page.keyboard.press("Escape")

    kebab = panel.get_by_role("button", name=f"More actions for {_FILE_NAME}")
    kebab.focus()
    kebab.press("Enter")
    expect(page.get_by_role("menuitem", name="Copy path")).to_be_visible()
    page.keyboard.press("Escape")
    expect(kebab).to_be_focused()
    kebab.click()
    page.get_by_role("menuitem", name="Copy path").click()
    page.wait_for_function(
        "expected => navigator.clipboard.readText().then(text => text === expected)",
        arg=_FILE_NAME,
    )
    assert page.evaluate("() => navigator.clipboard.readText()") == _FILE_NAME


@pytest.fixture
def touch_files_page(
    browser: Browser,
    page: Page,
    seeded_session: tuple[str, str],
    request: pytest.FixtureRequest,
) -> tuple[Page, Locator]:
    """Open the Files drawer in a touch-enabled browser context."""
    base_url, session_id = seeded_session
    _seed_file(page, base_url, session_id, request)
    context = browser.new_context(has_touch=True, viewport={"width": 390, "height": 844})
    page = context.new_page()
    page.goto(f"{base_url}/c/{session_id}")
    page.get_by_role("button", name="Conversation actions").click()
    page.get_by_role("menuitem", name="Files", exact=True).click()
    drawer = page.get_by_test_id("files-panel-drawer")
    expect(drawer).to_have_attribute("data-state", "open")
    drawer.get_by_role("searchbox", name="Search all files").click()
    row = _row(drawer)
    request.addfinalizer(context.close)
    return page, row


def _touch_point(row: Locator, *, y_offset: float = 0) -> dict[str, float | int]:
    bounds = row.bounding_box()
    assert bounds is not None, "file row has no touch target bounds"
    return {
        "id": 0,
        "x": bounds["x"] + min(100, bounds["width"] / 3),
        "y": bounds["y"] + bounds["height"] / 2 + y_offset,
    }


def _assert_touch_point_hits(row: Locator, point: dict[str, float | int]) -> None:
    hit_target = row.evaluate(
        """(element, point) => {
            const target = document.elementFromPoint(point.x, point.y);
            return { contains: element.contains(target), target: target?.outerHTML };
        }""",
        point,
    )
    assert hit_target["contains"], hit_target


def test_touch_long_press_opens_menu_without_opening_file(
    touch_files_page: tuple[Page, Locator],
) -> None:
    """A stationary hold opens actions without triggering the row click."""
    page, row = touch_files_page
    before = row.get_attribute("aria-expanded")
    cdp = page.context.new_cdp_session(page)
    point = _touch_point(row)
    _assert_touch_point_hits(row, point)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point]})
    try:
        page.wait_for_timeout(900)
        expect(page.get_by_role("menuitem", name="File info")).to_be_visible()
    finally:
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    expect(row).to_be_visible()
    expect(page.locator('[data-testid="file-viewer"]:visible')).to_have_count(0)
    assert row.evaluate("element => getComputedStyle(element).userSelect") == "none"
    assert row.get_attribute("aria-expanded") == before


def test_touch_scroll_hold_does_not_open_menu(touch_files_page: tuple[Page, Locator]) -> None:
    """Moving a held touch cancels the pending long-press menu."""
    page, row = touch_files_page
    cdp = page.context.new_cdp_session(page)
    point = _touch_point(row)
    _assert_touch_point_hits(row, point)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point]})
    try:
        page.wait_for_timeout(100)
        cdp.send(
            "Input.dispatchTouchEvent",
            {"type": "touchMove", "touchPoints": [_touch_point(row, y_offset=30)]},
        )
        page.wait_for_timeout(750)
        expect(page.get_by_role("menuitem", name="File info")).to_have_count(0)
    finally:
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
