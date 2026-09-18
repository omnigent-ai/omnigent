"""E2E: the whole file-explorer journey outside the workspace survives a
slash-merging front door.

The deployed app is served through a proxy that percent-decodes ``%2F`` and
merges the resulting ``//`` to a single ``/``. A wire form that marks an
absolute browse location with a leading ``%2F`` therefore arrives at the
server workspace-relative and the explorer breaks. The existing regression
test pins the re-rooted *listing* and *search*; this one pins the rest of the
journey a user actually drives after re-rooting the tree to a directory
above/outside the working folder:

1. the outside directory's contents list (not "No files in workspace"),
2. lazily expanding one of its subdirectories lists the nested entries, and
3. clicking one of its files opens the viewer with the real file content.

Each step issues its own filesystem request for an absolute path, so each is
a separate way the explorer can regress behind the proxy.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail
from tests.e2e_ui.files.test_files_panel_header import (
    _bind_host_with_listing,
    _simulate_frontdoor_slash_merge,
)

_FILE_BODY = "proof the outside file opened through the merged route"
_NESTED_BODY = "proof the lazy expand listed the nested entry"


@pytest.fixture
def _drop_routes(page: Page) -> Iterator[None]:
    """Unroute before the page closes so a teardown replay cannot error the
    next test's setup (mirrors the fixture in ``test_files_panel_header``)."""
    yield
    page.unroute_all(behavior="ignoreErrors")


def test_outside_browse_expand_and_open_survive_a_slash_merging_proxy(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    _drop_routes: None,
) -> None:
    base_url, session_id = seeded_session

    outside = tmp_path / "outside-root"
    nested = outside / "nested"
    nested.mkdir(parents=True)
    (outside / "notes.txt").write_text(f"{_FILE_BODY}\n")
    (nested / "deeper.txt").write_text(f"{_NESTED_BODY}\n")

    _bind_host_with_listing(page, session_id, entry=outside)
    _simulate_frontdoor_slash_merge(page, session_id)
    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Files")).click()

    path_button = rail.get_by_test_id("browse-location-path")
    expect(path_button).to_be_visible(timeout=30_000)
    path_button.click()

    picker = page.get_by_test_id("workspace-picker")
    expect(picker).to_be_visible(timeout=15_000)
    picker.get_by_test_id(f"workspace-picker-entry-{outside.name}").click()

    expect(path_button).to_contain_text(outside.name, timeout=30_000)
    expect(rail.get_by_role("button", name="notes.txt", exact=True)).to_be_visible(timeout=30_000)
    expect(rail.get_by_text("No files in workspace")).to_have_count(0)
    page.keyboard.press("Escape")

    dir_row = rail.get_by_role("button", name="nested/", exact=True)
    expect(dir_row).to_be_visible(timeout=30_000)
    dir_row.click()
    expect(rail.get_by_role("button", name="deeper.txt", exact=True)).to_be_visible(timeout=30_000)

    rail.get_by_role("button", name="notes.txt", exact=True).click()
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible(timeout=30_000)
    expect(file_viewer.get_by_text(_FILE_BODY).first).to_be_visible(timeout=30_000)
