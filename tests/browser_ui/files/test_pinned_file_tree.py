"""Full-screen Workspace keeps a file tree beside the content in Chromium."""

from __future__ import annotations

import json
import re

from playwright.sync_api import Page, expect

from tests.browser_ui.files.test_file_line_navigation import (
    BrowserSession,
    _mock_markdown_files,
)
from tests.browser_ui.files.test_file_line_navigation import (
    seeded_session as seeded_session_fixture,
)

seeded_session = seeded_session_fixture


def test_full_screen_pins_file_tree(page: Page, seeded_session: BrowserSession) -> None:
    _mock_markdown_files(page, seeded_session, {"src/app.md": "# App", "src/guide.md": "# Guide"})
    seeded_session.contract.route(
        re.compile(r".*/environments/default/filesystem\?.*"),
        lambda route: route.fulfill(
            json={
                "object": "list",
                "has_more": False,
                "data": [{"path": "src", "name": "src", "type": "directory"}],
            }
        ),
    )
    state = [{"id": seeded_session.session_id, "state": {"open": True, "rightRailTab": "files"}}]
    encoded_state = json.dumps(json.dumps(state))
    page.add_init_script(
        f"localStorage.setItem('omnigent:session-workspace-state', {encoded_state});"
    )
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{seeded_session.contract.base_url}/c/{seeded_session.session_id}")
    toolbar = page.get_by_role("toolbar", name="Workspace tabs")
    rail = page.get_by_role("complementary", name="Workspace")
    expect(rail.get_by_text("src/", exact=True)).to_be_visible(timeout=30_000)

    toolbar.get_by_role("button", name="Full screen", exact=True).click()
    tree = page.get_by_test_id("pinned-file-tree")
    # The remounted tree reuses cached listings; its rows must not wait for a refetch.
    expect(tree.get_by_text("src/", exact=True)).to_be_visible(timeout=2_000)
    tree.get_by_text("src/", exact=True).click()
    tree.get_by_text("app.md", exact=True).click()
    expect(page.locator('[data-testid="file-viewer"]:visible')).to_contain_text("App")
    expect(tree).to_be_visible()

    toolbar.get_by_role("button", name="Hide file tree").click()
    expect(tree).to_have_count(0)
    page.reload()
    toolbar.get_by_role("button", name="Full screen", exact=True).click()
    expect(toolbar.get_by_role("button", name="Show file tree")).to_be_visible()
    expect(tree).to_have_count(0)
