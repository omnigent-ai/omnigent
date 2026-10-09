"""Workspace tab navigation in Chromium with a sealed mock backend."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, BrowserType, Page, expect

from tests.browser_ui.files.test_file_line_navigation import (
    BrowserSession,
    _mock_markdown_files,
)
from tests.browser_ui.files.test_file_line_navigation import (
    seeded_session as seeded_session_fixture,
)

seeded_session = seeded_session_fixture

# Gutter a plain scroller gets in this browser; zero means scrollbars are hidden.
_SCROLLBAR_PROBE_JS = """() => {
    const probe = document.createElement('div');
    probe.style.cssText = 'position:fixed;width:100px;height:40px;overflow-x:scroll';
    document.body.append(probe);
    const gutter = probe.offsetHeight - probe.clientHeight;
    probe.remove();
    return gutter;
}"""


@pytest.fixture(scope="module")
def browser(
    browser_type: BrowserType, browser_type_launch_args: dict[str, Any]
) -> Iterator[Browser]:
    # Headless Chromium hides scrollbars by default while a user's browser draws
    # them; a module-scoped browser keeps that change off the shared session browser.
    launch_args = {
        **browser_type_launch_args,
        "ignore_default_args": [
            *browser_type_launch_args.get("ignore_default_args", []),
            "--hide-scrollbars",
        ],
    }
    scrollbar_browser = browser_type.launch(**launch_args)
    yield scrollbar_browser
    scrollbar_browser.close()


@pytest.mark.parametrize("width", [240, 360, 472, 900])
def test_workspace_tab_overflow(
    page: Page, seeded_session: BrowserSession, width: int, tmp_path: Path
) -> None:
    files = {f"src/long-panel-name-{index}.md": f"# Panel {index}" for index in range(12)}
    paths = list(files)
    _mock_markdown_files(page, seeded_session, files)
    state = [
        {
            "id": seeded_session.session_id,
            "state": {
                "open": True,
                "widthPx": width,
                "rightRailTab": "files",
                "openFiles": paths,
                "selectedFilePath": paths[0],
            },
        }
    ]
    encoded_state = json.dumps(json.dumps(state))
    page.add_init_script(
        f"localStorage.setItem('omnigent:session-workspace-state', {encoded_state});"
    )
    page.set_viewport_size({"width": 1800, "height": 1000})
    page.goto(f"{seeded_session.contract.base_url}/c/{seeded_session.session_id}")
    toolbar = page.get_by_role("toolbar", name="Workspace tabs")
    viewport = toolbar.locator("[data-workspace-tabs-viewport]")
    expect(viewport).to_be_visible(timeout=30_000)
    assert page.evaluate(_SCROLLBAR_PROBE_JS) > 0, "this browser must draw scrollbars"
    fixed_panel_tabs = toolbar.locator('[data-workspace-tab="changes"]')
    if width < 400:
        # Narrow rails move the fixed panels into the picker to leave room for tabs.
        expect(fixed_panel_tabs).to_be_hidden()
    else:
        expect(fixed_panel_tabs).to_be_visible()
    expect(toolbar.get_by_role("button", name="Scroll tabs right")).to_be_enabled()
    expect(toolbar.get_by_role("button", name="Scroll tabs left")).to_be_disabled()
    assert viewport.evaluate("el => getComputedStyle(el).scrollbarWidth") == "none"
    assert viewport.evaluate("el => el.offsetHeight === el.clientHeight")
    assert (
        abs(page.get_by_role("complementary", name="Workspace").bounding_box()["width"] - width)
        < 1
    )
    # Every navigation control stays within its slot, including narrow Electron rails.
    assert viewport.evaluate("""el => {
        const slot = el.parentElement.getBoundingClientRect();
        const next = el.parentElement.nextElementSibling.getBoundingClientRect();
        return el.clientWidth >= 80 && slot.right <= next.left;
    }""")

    toolbar.get_by_role("button", name="Scroll tabs right").click()
    page.wait_for_function(
        "document.querySelector('[data-workspace-tabs-viewport]').scrollLeft > 0"
    )
    expect(toolbar.get_by_role("button", name="Scroll tabs left")).to_be_enabled()
    toolbar.get_by_role("button", name="Scroll tabs left").click()
    page.wait_for_function(
        "document.querySelector('[data-workspace-tabs-viewport]').scrollLeft === 0"
    )

    viewport.hover()
    page.keyboard.down("Control")
    page.mouse.wheel(0, 150)
    page.keyboard.up("Control")
    page.wait_for_function(
        "document.querySelector('[data-workspace-tabs-viewport]').scrollLeft > 0"
    )
    offset = viewport.evaluate("el => el.scrollLeft")
    page.mouse.wheel(150, 0)
    page.wait_for_function(
        "offset => document.querySelector('[data-workspace-tabs-viewport]').scrollLeft > offset",
        arg=offset,
    )
    # A plain vertical wheel over the strip also moves it sideways.
    offset = viewport.evaluate("el => el.scrollLeft")
    page.mouse.wheel(0, 150)
    page.wait_for_function(
        "offset => document.querySelector('[data-workspace-tabs-viewport]').scrollLeft > offset",
        arg=offset,
    )

    toolbar.get_by_role("button", name="Select panel").click()
    expect(page.get_by_role("menuitem", name=paths[0], exact=True)).to_have_attribute(
        "aria-current", "true"
    )
    page.screenshot(path=tmp_path / f"omnigent-tab-picker-{width}.png")
    page.get_by_role("menuitem", name=paths[-1], exact=True).click()
    expect(page.locator('[data-testid="file-viewer"]:visible')).to_contain_text("Panel 11")
    active_tab = viewport.locator('[role="button"][aria-current="true"]')
    assert active_tab.evaluate(
        """el => {
            const a = el.getBoundingClientRect();
            const v = el.closest('[data-workspace-tabs-viewport]').getBoundingClientRect();
            const visibleWidth = Math.min(a.right, v.right) - Math.max(a.left, v.left);
            return visibleWidth >= Math.min(a.width, v.width) - 1;
        }"""
    )
    page.screenshot(path=tmp_path / f"omnigent-tabs-{width}.png")

    # Resize through fullscreen and back with the last tab selected.
    toolbar.get_by_role("button", name="Full screen", exact=True).click()
    toolbar.get_by_role("button", name="Exit full screen", exact=True).click()
    expect(active_tab).to_be_in_viewport()

    # The picker remains keyboard accessible and offers fixed navigation panels.
    toolbar.get_by_role("button", name="Select panel").focus()
    page.keyboard.press("Enter")
    page.get_by_role("menuitem", name="Changes", exact=True).focus()
    page.keyboard.press("Enter")
    expect(toolbar.locator('[data-workspace-tab="changes"]')).to_have_attribute(
        "data-state", "active"
    )
    # Fullscreen removes overflow arrows when all tabs fit after closing most files.
    for path in paths[1:]:
        # The picker can reveal a tab without requiring it to be initially visible.
        toolbar.get_by_role("button", name="Select panel").click()
        page.get_by_role("menuitem", name=path, exact=True).click()
        viewport.get_by_role("button", name=f"Close {path.split('/')[-1]}", exact=True).click()
    if width == 472:
        # A lone tab fits the full slot but not the slot minus two arrows.
        assert viewport.evaluate(
            "el => el.firstElementChild.clientWidth <= el.parentElement.clientWidth"
        )
        expect(toolbar.get_by_role("button", name="Scroll tabs right")).to_have_count(0)
    toolbar.get_by_role("button", name="Full screen", exact=True).click()
    expect(toolbar.get_by_role("button", name="Scroll tabs right")).to_have_count(0)
    expect(toolbar.get_by_role("button", name="Scroll tabs left")).to_have_count(0)
