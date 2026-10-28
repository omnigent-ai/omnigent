"""Workspace tab navigation in Chromium with a sealed mock backend."""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.files.test_file_line_navigation import (
    BrowserSession,
    _mock_markdown_files,
)
from tests.browser_ui.files.test_file_line_navigation import (
    seeded_session as seeded_session_fixture,
)

seeded_session = seeded_session_fixture


@pytest.mark.parametrize("width", [240, 360, 472, 900])
def test_workspace_tab_overflow(page: Page, seeded_session: BrowserSession, width: int) -> None:
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
    # Overflowing tabs scroll by wheel only; there are no arrow buttons.
    assert viewport.evaluate("el => el.scrollWidth > el.clientWidth")
    expect(toolbar.get_by_role("button", name="Scroll tabs right")).to_have_count(0)
    expect(toolbar.get_by_role("button", name="Scroll tabs left")).to_have_count(0)
    assert viewport.evaluate("el => getComputedStyle(el).scrollbarWidth") == "none"
    assert viewport.evaluate("el => el.offsetHeight === el.clientHeight")
    assert (
        abs(page.get_by_role("complementary", name="Workspace").bounding_box()["width"] - width)
        < 1
    )
    # The tabs stay clear of the trailing controls, including narrow Electron rails.
    assert viewport.evaluate("""el => {
        const slot = el.getBoundingClientRect();
        const next = el.nextElementSibling.getBoundingClientRect();
        return el.clientWidth >= 80 && slot.right <= next.left;
    }""")

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

    toolbar.get_by_role("button", name="Select panel").click()
    expect(page.get_by_role("menuitem", name=paths[0], exact=True)).to_have_attribute(
        "aria-current", "true"
    )
    page.screenshot(path=f"/tmp/omnigent-tab-picker-{width}.png")
    page.get_by_role("menuitem", name=paths[-1], exact=True).click()
    expect(page.locator('[data-testid="file-viewer"]:visible')).to_contain_text("Panel 11")
    # Radix refocuses the trigger from a timer queued after the menu unmounts; a
    # mouse pick must skip that or the tooltip re-opens. Give that timer a short,
    # bounded window before asserting that focus and the tooltip stayed away.
    expect(page.get_by_role("menu")).to_have_count(0)
    page.mouse.move(500, 500)
    page.wait_for_timeout(100)
    expect(toolbar.get_by_role("button", name="Select panel")).not_to_be_focused()
    expect(page.get_by_role("tooltip")).to_have_count(0)
    active_tab = viewport.locator('[role="button"][aria-current="true"]')
    assert active_tab.evaluate(
        """el => {
            const a = el.getBoundingClientRect();
            const v = el.closest('[data-workspace-tabs-viewport]').getBoundingClientRect();
            const visibleWidth = Math.min(a.right, v.right) - Math.max(a.left, v.left);
            return visibleWidth >= Math.min(a.width, v.width) - 1;
        }"""
    )
    page.screenshot(path=f"/tmp/omnigent-tabs-{width}.png")

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
    for path in paths[1:]:
        # The picker can reveal a tab without requiring it to be initially visible.
        toolbar.get_by_role("button", name="Select panel").click()
        page.get_by_role("menuitem", name=path, exact=True).click()
        viewport.get_by_role("button", name=f"Close {path.split('/')[-1]}", exact=True).click()
    first_close = f"Close {paths[0].split('/')[-1]}"
    expect(viewport.get_by_role("button", name=first_close, exact=True)).to_be_in_viewport()
