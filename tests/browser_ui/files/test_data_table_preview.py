"""A CSV opens as a table in real Chromium: sticky header, sorting, filtering, phone width."""

from __future__ import annotations

from playwright.sync_api import Locator, Page, expect

from tests.browser_ui.files.test_file_line_navigation import (
    BrowserSession,
    _mock_markdown_files,
)
from tests.browser_ui.files.test_file_line_navigation import (
    seeded_session as seeded_session_fixture,
)

seeded_session = seeded_session_fixture

_FILE_PATH = "src/regional_sales.csv"
_COLUMNS = ["region", "units", "revenue", "closed", "account manager", "notes"]
_ROWS = [
    [
        f"Region {index:03}",
        str(index * 7 % 50),
        f"{index * 1013 % 90000}.50",
        "2026-03-01",
        "Kim Lee",
        '"a, b"',
    ]
    for index in range(1, 201)
]
_CSV = "\n".join([",".join(_COLUMNS), *(",".join(row) for row in _ROWS)])


def _open_table(page: Page, session: BrowserSession, width: int, height: int) -> Locator:
    """Open the CSV at the given viewport size; return its table."""
    _mock_markdown_files(page, session, {_FILE_PATH: _CSV})
    base_url, session_id = session
    page.set_viewport_size({"width": width, "height": height})
    page.goto(f"{base_url}/c/{session_id}?file={_FILE_PATH}")
    table = page.get_by_role("table", name=_FILE_PATH)
    expect(table).to_be_visible(timeout=30_000)
    return table


def _scroller(table: Locator) -> Locator:
    return table.locator("xpath=ancestor::*[@data-preview-scroll][1]")


def test_a_csv_opens_as_a_sortable_filterable_table(
    page: Page, seeded_session: BrowserSession
) -> None:
    """The table keeps its header in view, resizes, sorts, filters, and hands over to Source."""
    table = _open_table(page, seeded_session, 1500, 900)
    viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(viewer.get_by_test_id("data-table-summary")).to_contain_text(
        "200 rows · 6 columns · comma-separated"
    )

    header = table.get_by_role("columnheader").first
    top_before = header.bounding_box()["y"]
    _scroller(table).evaluate("element => { element.scrollTop = 2000; }")
    expect(table.get_by_role("cell", name="Region 001")).not_to_be_in_viewport()
    assert abs(header.bounding_box()["y"] - top_before) < 2, "the header scrolled away"
    expect(viewer.get_by_test_id("data-table-summary")).to_be_in_viewport()

    units = table.get_by_role("columnheader").nth(1)
    width_before = units.bounding_box()["width"]
    handle = table.get_by_role("separator", name="Resize units").bounding_box()
    page.mouse.move(handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2)
    page.mouse.down()
    page.mouse.move(handle["x"] + 120, handle["y"] + handle["height"] / 2, steps=5)
    page.mouse.up()
    assert units.bounding_box()["width"] > width_before + 100, "dragging didn't widen the column"

    table.get_by_role("button", name="Sort by units").click()
    expect(table.get_by_role("columnheader").nth(1)).to_have_attribute("aria-sort", "ascending")
    table.get_by_role("textbox", name="Filter region").fill("Region 01")
    expect(viewer.get_by_text("10 matching")).to_be_visible()

    viewer.get_by_role("button", name="View source").click()
    expect(viewer.locator(".monaco-editor")).to_be_visible(timeout=30_000)
    expect(table).to_have_count(0)


def test_a_wide_csv_scrolls_sideways_at_phone_width(
    page: Page, seeded_session: BrowserSession
) -> None:
    """At phone width the table scrolls inside the viewer, not the page."""
    table = _open_table(page, seeded_session, 390, 844)
    scroller = _scroller(table)
    assert scroller.evaluate("element => element.scrollWidth > element.clientWidth")
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth"), (
        "the page itself scrolls sideways"
    )
