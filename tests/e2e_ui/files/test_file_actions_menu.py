"""Browser coverage for the shared session file-row actions."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail

_FILE_NAME = "row actions Ω report.txt"
_FILE_CONTENT = "file-row-actions-menu-e2e-content"
_TOUCH_FOLDER = "touch actions folder"


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
    """Exercise pointer, keyboard, Info, and relative-path copy entry points."""
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
    expect(page.get_by_role("menuitem", name="Copy relative path")).to_be_visible()
    expect(page.get_by_role("menuitem", name="File info")).to_be_visible()
    expect(
        page.get_by_role("menuitem", name=re.compile("Finder|File Explorer|file manager"))
    ).to_have_count(0)
    page.get_by_role("menuitem", name="File info").click()

    info = page.get_by_role("dialog", name="File info")
    expect(info).to_contain_text(_FILE_NAME)
    expect(info).to_contain_text(f"{len(_FILE_CONTENT)} B")
    expect(info).to_contain_text("File (.txt)")
    expect(info).to_contain_text("Changes")
    info.get_by_role("button", name="Close").click()
    expect(panel.get_by_role("button", name=_FILE_NAME, exact=True)).to_be_focused()

    kebab = panel.get_by_role("button", name=f"More actions for {_FILE_NAME}")
    kebab.focus()
    kebab.press("ContextMenu")
    expect(page.get_by_role("menuitem", name="File info")).to_be_visible()
    page.keyboard.press("Escape")
    expect(kebab).to_be_focused()

    kebab.focus()
    kebab.press("Enter")
    expect(page.get_by_role("menuitem", name="Copy relative path")).to_be_visible()
    page.keyboard.press("Escape")
    expect(kebab).to_be_focused()
    kebab.click()
    page.get_by_role("menuitem", name="File info").click()
    info.get_by_role("button", name="Close").click()
    expect(kebab).to_be_focused()
    kebab.click()
    page.get_by_role("menuitem", name="Copy relative path").click()
    page.wait_for_function(
        "expected => navigator.clipboard.readText().then(text => text === expected)",
        arg=_FILE_NAME,
    )
    assert page.evaluate("() => navigator.clipboard.readText()") == _FILE_NAME


def test_info_copy_tooltip_stays_within_dialog_at_desktop_and_mobile_widths(
    page: Page,
    seeded_session: tuple[str, str],
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    """The copy tooltip remains fully visible inside Info at both panel widths."""
    base_url, session_id = seeded_session
    _seed_file(page, base_url, session_id, request)
    screenshots = tmp_path / "tooltip-bounds"
    screenshots.mkdir()
    failures: list[str] = []

    def capture_tooltip(view: str, state: str, info: Locator) -> None:
        button = info.get_by_role("button", name=f"Copy path: {_FILE_NAME}")
        if state == "hover":
            button.hover()
        else:
            button.focus()
        tooltip = page.get_by_role("tooltip")
        expect(tooltip).to_have_text("Copy path")
        page.wait_for_timeout(700)
        page.screenshot(path=str(screenshots / f"{view}-{state}.png"))
        dialog_box = info.bounding_box()
        tooltip_box = tooltip.bounding_box()
        assert dialog_box is not None and tooltip_box is not None
        if (
            tooltip_box["x"] < dialog_box["x"]
            or tooltip_box["x"] + tooltip_box["width"] > dialog_box["x"] + dialog_box["width"]
        ):
            failures.append(f"{view}-{state}: dialog={dialog_box}, tooltip={tooltip_box}")

    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    panel = page.get_by_role("complementary", name="Workspace")
    panel.get_by_role("tab", name="Files").click()
    row = _row(panel)
    row.click(button="right")
    page.get_by_role("menuitem", name="File info").click()
    info = page.get_by_role("dialog", name="File info")
    capture_tooltip("desktop", "hover", info)
    capture_tooltip("desktop", "focus", info)

    page.set_viewport_size({"width": 360, "height": 740})
    page.goto(f"{base_url}/c/{session_id}")
    page.get_by_role("button", name="Conversation actions").click()
    page.get_by_role("menuitem", name="Files", exact=True).click()
    drawer = page.get_by_test_id("files-panel-drawer")
    drawer.get_by_role("searchbox", name="Search all files").click()
    row = _row(drawer)
    row.click(button="right")
    page.get_by_role("menuitem", name="File info").click()
    info = page.get_by_role("dialog", name="File info")
    capture_tooltip("mobile", "hover", info)
    capture_tooltip("mobile", "focus", info)

    assert not failures, "copy tooltip must fit within Info dialog bounds: " + "; ".join(failures)


def test_size_does_not_overlap_actions_on_keyboard_focus_or_open_menu(
    page: Page,
    seeded_session: tuple[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Tree, search, and Changes rows keep their size clear of visible actions."""
    base_url, session_id = seeded_session
    _seed_file(page, base_url, session_id, request)
    page.route(
        re.compile(
            rf"/v1/sessions/{re.escape(session_id)}/resources/environments/[^/]+/changes(\?|$)"
        ),
        lambda route: route.fulfill(
            json={
                "data": [
                    {
                        "path": _FILE_NAME,
                        "name": _FILE_NAME,
                        "status": "created",
                        "bytes": len(_FILE_CONTENT),
                        "modified_at": None,
                        "lines_added": None,
                        "lines_removed": None,
                    }
                ]
            }
        ),
    )
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    panel = page.get_by_role("complementary", name="Workspace")
    panel.get_by_role("tab", name=re.compile("^Files")).click()

    def check_row(row: Locator) -> None:
        page.mouse.move(0, 0)
        row.locator('button:not([aria-label^="More actions for"])').first.focus()
        kebab = row.get_by_role("button", name=re.compile("^More actions for"))
        _assert_size_does_not_overlap_kebab(row)
        kebab.click()
        page.mouse.move(0, 0)
        expect(page.get_by_role("menuitem", name="File info")).to_be_visible()
        _assert_size_does_not_overlap_kebab(row)
        page.keyboard.press("Escape")

    row = page.locator('[data-slot="context-menu-trigger"]').filter(has_text=_FILE_NAME).last
    expect(row).to_be_visible(timeout=30_000)
    check_row(row)

    search = panel.get_by_role("searchbox", name="Search all files")
    search.fill(_FILE_NAME)
    search_row = (
        page.locator('[data-slot="context-menu-trigger"]').filter(has_text=_FILE_NAME).last
    )
    expect(search_row).to_be_visible(timeout=30_000)
    check_row(search_row)

    panel.get_by_role("tab", name=re.compile("^Changes")).click()
    changes_row = (
        page.locator('[data-slot="context-menu-trigger"]').filter(has_text=_FILE_NAME).last
    )
    expect(changes_row).to_be_visible(timeout=30_000)
    check_row(changes_row)


def test_fine_pointer_actions_slot_stays_stable_and_size_tracks_visible_actions(
    page: Page,
    seeded_session: tuple[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Hover and keyboard actions don't resize rows or leave the size hidden."""
    base_url, session_id = seeded_session
    _seed_file(page, base_url, session_id, request)
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    panel = page.get_by_role("complementary", name="Workspace")
    panel.get_by_role("tab", name=re.compile("^Files")).click()
    row = page.locator('[data-slot="context-menu-trigger"]').filter(has_text=_FILE_NAME).last
    expect(row).to_be_visible(timeout=30_000)
    kebab = row.get_by_role("button", name=re.compile("^More actions for"))
    size = row.locator("span.text-sm.text-muted-foreground").filter(
        has_text=re.compile(r"\d+(?:\.\d+)?\s+[KMGT]?B")
    )

    def action_slot_width() -> float:
        return row.evaluate(
            """row => {
              const kebab = row.querySelector('button[aria-label^="More actions for"]');
              const slot = kebab?.closest('span.absolute')?.parentElement;
              return slot?.getBoundingClientRect().width ?? -1;
            }"""
        )

    page.mouse.move(0, 0)
    expect(size).to_be_visible()
    idle_width = action_slot_width()
    row.hover()
    expect(kebab).to_have_css("opacity", "1")
    expect(size).to_have_css("visibility", "hidden")
    assert action_slot_width() == idle_width

    page.mouse.move(0, 0)
    row.locator("button").first.focus()
    expect(size).to_be_visible()
    expect(kebab).to_have_css("opacity", "1")
    assert action_slot_width() == idle_width
    _assert_size_does_not_overlap_kebab(row)

    page.keyboard.press("Tab")
    assert row.evaluate("row => !!row.querySelector(':focus-visible')")
    expect(size).to_have_css("visibility", "hidden")
    assert action_slot_width() == idle_width

    kebab.click()
    expect(page.get_by_role("menuitem", name="File info")).to_be_visible()
    expect(size).to_have_css("visibility", "hidden")
    assert action_slot_width() == idle_width
    page.keyboard.press("Escape")


def test_fine_pointer_inline_action_controls_have_spacing(
    page: Page,
    seeded_session: tuple[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Download, Copy, and kebab controls keep clear spacing on fine pointers."""
    base_url, session_id = seeded_session
    _seed_file(page, base_url, session_id, request)
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    panel = page.get_by_role("complementary", name="Workspace")
    panel.get_by_role("tab", name=re.compile("^Files")).click()
    row = _row(panel)
    row.hover()

    targets = [
        row.get_by_role("button", name=re.compile(r"^Download ")),
        row.get_by_role("button", name=re.compile(r"^Copy path:")),
        row.get_by_role("button", name=f"More actions for {_FILE_NAME}"),
    ]
    boxes = [target.bounding_box() for target in targets]
    assert all(box is not None for box in boxes), "all file action targets should be rendered"
    measured = [box for box in boxes if box is not None]

    def controls_have_spacing(left: dict[str, float], right: dict[str, float]) -> bool:
        left_small = left["width"] < 24 or left["height"] < 24
        right_small = right["width"] < 24 or right["height"] < 24
        if not left_small and not right_small:
            return True
        left_center = (left["x"] + left["width"] / 2, left["y"] + left["height"] / 2)
        right_center = (right["x"] + right["width"] / 2, right["y"] + right["height"] / 2)
        if left_small and right_small:
            return (right_center[0] - left_center[0]) ** 2 + (
                right_center[1] - left_center[1]
            ) ** 2 >= 24**2
        small_center, target, target_center = (
            (left_center, right, right_center) if left_small else (right_center, left, left_center)
        )
        dx = max(abs(small_center[0] - target_center[0]) - target["width"] / 2, 0)
        dy = max(abs(small_center[1] - target_center[1]) - target["height"] / 2, 0)
        return dx**2 + dy**2 >= 12**2

    assert controls_have_spacing(measured[0], measured[1]), (
        f"Download and Copy inline controls lack spacing: {measured}"
    )
    assert controls_have_spacing(measured[1], measured[2]), (
        f"Copy and kebab inline controls lack spacing: {measured}"
    )


@pytest.fixture
def touch_files_page(
    browser: Browser,
    page: Page,
    seeded_session: tuple[str, str],
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> tuple[Page, Locator]:
    """Open the Files drawer in a touch-enabled browser context."""
    base_url, session_id = seeded_session
    seeded_file = _seed_file(page, base_url, session_id, request)
    folder = seeded_file.parent / _TOUCH_FOLDER
    folder.mkdir()
    (folder / "inside.txt").write_text("folder hold", encoding="utf-8")
    request.addfinalizer(lambda: shutil.rmtree(folder, ignore_errors=True))
    context = browser.new_context(
        has_touch=True,
        viewport={"width": 390, "height": 844},
        record_video_dir=tmp_path,
    )
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


def test_touch_long_press_release_does_not_toggle_folder(
    touch_files_page: tuple[Page, Locator],
) -> None:
    """Releasing a folder hold leaves its expansion state unchanged."""
    page, _ = touch_files_page
    folder = page.locator("button[aria-expanded]").filter(has_text=_TOUCH_FOLDER).last
    expect(folder).to_be_visible()
    before = folder.get_attribute("aria-expanded")
    assert before == "false"
    point = _touch_point(folder)
    row = folder.locator("xpath=..")
    _assert_touch_point_hits(row, point)
    cdp = page.context.new_cdp_session(page)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point]})
    try:
        page.wait_for_timeout(900)
        expect(page.get_by_role("menuitem", name="Browse folder")).to_be_visible()
    finally:
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    assert folder.get_attribute("aria-expanded") == before


def _assert_size_does_not_overlap_kebab(row: Locator) -> None:
    expect(row.locator('button[aria-label^="More actions for"]')).to_have_css("opacity", "1")
    layout = row.evaluate(
        """row => {
          const kebab = row.querySelector('button[aria-label^="More actions for"]');
          const sizes = [...row.querySelectorAll('span.text-sm.text-muted-foreground')];
          const size = sizes.find(element =>
            /\\d+(?:\\.\\d+)?\\s+[KMGT]?B/.test(element.textContent || '')
          );
          if (!kebab || !size) return null;
          const a = size.getBoundingClientRect();
          const b = kebab.getBoundingClientRect();
          return {
            visibility: getComputedStyle(size).visibility,
            opacity: getComputedStyle(kebab).opacity,
            overlap: a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top,
          };
        }"""
    )
    assert layout is not None, "expected a size label and row kebab"
    assert layout["visibility"] == "hidden" or not layout["overlap"], layout


def test_coarse_pointer_idle_keeps_size_visible_beside_kebab(
    touch_files_page: tuple[Page, Locator],
) -> None:
    """At rest on a coarse pointer, the visible kebab has clear space."""
    page, row = touch_files_page
    assert page.evaluate("() => matchMedia('(pointer: coarse)').matches")
    layout = row.evaluate(
        """row => {
          const kebab = row.querySelector('button[aria-label^="More actions for"]');
          const size = [...row.querySelectorAll('span.text-sm.text-muted-foreground')]
            .find(element => /\\d+(?:\\.\\d+)?\\s+[KMGT]?B/.test(element.textContent || ''));
          if (!kebab || !size) return null;
          const a = size.getBoundingClientRect();
          const b = kebab.getBoundingClientRect();
          return {
            visibility: getComputedStyle(size).visibility,
            overlap: a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top,
            width: b.width,
            height: b.height,
            leftTarget: document.elementFromPoint(b.left - 2, b.top + b.height / 2)?.tagName,
          };
        }"""
    )
    assert layout is not None, "expected a size label and row kebab"
    assert layout["visibility"] == "visible" and not layout["overlap"], layout
    assert layout["width"] >= 24 and layout["height"] >= 24, layout
    assert layout["leftTarget"] != "BUTTON", layout


def test_touch_scroll_hold_does_not_open_menu(touch_files_page: tuple[Page, Locator]) -> None:
    """Moving a held touch cancels the pending long-press menu."""
    page, row = touch_files_page
    cdp = page.context.new_cdp_session(page)
    point = _touch_point(row)
    _assert_touch_point_hits(row, point)

    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point]})
    try:
        page.wait_for_timeout(900)
        expect(page.get_by_role("menuitem", name="File info")).to_be_visible()
    finally:
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.keyboard.press("Escape")
    expect(page.get_by_role("menuitem", name="File info")).to_have_count(0)

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
