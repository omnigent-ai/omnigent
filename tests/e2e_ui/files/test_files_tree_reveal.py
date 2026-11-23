"""E2E: the tabs sidebar's Files tree reveals the viewed file from its header and ⌘K.

With ``workspace_sidebar_v2`` on, a file opened far down a large folder stays selected in
the tree beside it. After the reader scrolls away or closes its folders, the tree
header's Reveal Active File brings it back, and so does ⌘K's Reveal Active File in Files
Tree. From a folder that doesn't hold the file, the tree returns to the workspace to show
it, and on a narrow page ⌘K opens the hidden tree to reveal it. Seeded through the
environment's shell endpoint (no agent run); when a reveal is offered is in the unit
tests (``revealActiveFile``, ``FilesPageTree``).
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests._helpers.polling import poll_until
from tests.e2e_ui.files._files_page import open_workspace, run_palette
from tests.e2e_ui.sessions.test_workspace_sidebar_v2 import _open_app

# Hundreds of folders sort before the target's, so its row starts far below the fold.
_FILLER_FOLDERS = 400
_TARGET_FOLDER = "packages/widgets"
_TARGET = f"{_TARGET_FOLDER}/OWNERS"

_SEED_SCRIPT = f"""
set -e
mkdir -p {_TARGET_FOLDER}
seq -w 0 {_FILLER_FOLDERS - 1} | while read n; do
  mkdir -p "packages/d$n"
  : > "packages/d$n/BUILD"
done
echo "owner-sentinel" > {_TARGET}
pwd
"""


@pytest.fixture
def large_tree_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Seed ``packages/`` with hundreds of folders and the target file after them."""
    base_url, session_id = seeded_session
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/shell",
        json={"command": _SEED_SCRIPT, "timeout": 300},
        timeout=330,
    )
    response.raise_for_status()
    result = response.json()
    assert result["exit_code"] == 0, result
    workspace = Path(result["stdout"].strip().splitlines()[-1])
    try:
        yield (base_url, session_id)
    finally:
        shutil.rmtree(workspace / "packages", ignore_errors=True)


def _row(container: Locator, name: str) -> Locator:
    return container.get_by_role("treeitem", name=name, exact=True)


def _scroller(container: Locator) -> Locator:
    return container.locator("[data-file-tree-scroll]")


def _row_in_view(container: Locator, name: str) -> bool:
    """Whether the row is rendered and lies wholly inside the tree's viewport."""
    row = _row(container, name)
    if row.count() == 0:
        return False
    box = _scroller(container).bounding_box()
    row_box = row.bounding_box()
    return (
        box is not None
        and row_box is not None
        and row_box["y"] >= box["y"] - 1
        and row_box["y"] + row_box["height"] <= box["y"] + box["height"] + 1
    )


def _expect_row_in_view(container: Locator, name: str) -> None:
    """The row is rendered, selected, and lies wholly inside the tree's viewport."""
    expect(_row(container, name)).to_have_attribute("aria-selected", "true", timeout=30_000)
    poll_until(lambda: _row_in_view(container, name), timeout_s=10.0, interval_s=0.1)


def _scroll_to_top(container: Locator) -> None:
    # The tree keeps its current row mounted, so "out of view" is the check, not "gone".
    _scroller(container).evaluate("element => { element.scrollTop = 0; }")
    poll_until(lambda: not _row_in_view(container, "OWNERS"), timeout_s=10.0, interval_s=0.1)


def _open_owners(tree: Locator) -> None:
    """Open the target from the tree, past the hundreds of folders that sort before it."""
    packages = _row(tree, "packages")
    expect(packages).to_be_visible(timeout=30_000)
    if packages.get_attribute("aria-expanded") != "true":
        packages.click()
    expect(_row(tree, "d000")).to_be_visible(timeout=30_000)
    _scroller(tree).evaluate("element => { element.scrollTop = element.scrollHeight; }")
    widgets = _row(tree, "widgets")
    expect(widgets).to_be_visible(timeout=30_000)
    widgets.click()
    owners = _row(tree, "OWNERS")
    expect(owners).to_be_visible(timeout=30_000)
    owners.click()


def test_the_viewed_file_is_revealed_from_the_tree_header_and_the_palette(
    page: Page, large_tree_session: tuple[str, str]
) -> None:
    base_url, session_id = large_tree_session
    sidebar = open_workspace(page, base_url, session_id)
    _open_app(page, sidebar, "Files")
    # A full-screen workspace docks the tree beside the file.
    page.get_by_role("button", name="Full screen", exact=True).click()
    expect(_row(sidebar, "packages")).to_be_visible(timeout=30_000)
    reveal = sidebar.get_by_role("button", name="Reveal Active File", exact=True)
    expect(reveal).to_have_attribute("aria-disabled", "true")

    # The file opened from the tree stays selected and in view beside it.
    place = sidebar.locator('[data-testid="workspace-files-place"]:visible')
    _open_owners(place)
    expect(place.get_by_text("owner-sentinel")).to_be_visible(timeout=30_000)
    _expect_row_in_view(place, "OWNERS")
    reveal = place.get_by_role("button", name="Reveal Active File", exact=True)
    expect(reveal).not_to_have_attribute("aria-disabled", "true")

    # Scrolled back to the top, the header's button brings the file back into view.
    _scroll_to_top(place)
    reveal.click()
    _expect_row_in_view(place, "OWNERS")

    # ⌘K runs the same command, even with the folder above the file closed meanwhile.
    _scroll_to_top(place)
    # A click on the folder's row toggles it.
    _row(place, "packages").click()
    expect(_row(place, "packages")).to_have_attribute("aria-expanded", "false")
    run_palette(page, "Reveal Active File in Files Tree")
    _expect_row_in_view(place, "OWNERS")

    # From a folder that doesn't hold the file, the tree returns to the workspace to show it.
    _scroll_to_top(place)
    _row(place, "d000").dblclick()
    expect(_row(place, "BUILD")).to_be_visible(timeout=30_000)
    expect(_row(place, "packages")).to_have_count(0)
    reveal.click()
    _expect_row_in_view(place, "OWNERS")
    expect(_row(place, "packages")).to_be_visible()


def test_the_palette_opens_a_hidden_tree_to_reveal_the_file_on_a_narrow_page(
    page: Page, large_tree_session: tuple[str, str]
) -> None:
    base_url, session_id = large_tree_session
    sidebar = open_workspace(page, base_url, session_id)
    _open_app(page, sidebar, "Files")
    expect(_row(sidebar, "packages")).to_be_visible(timeout=30_000)

    # The narrow page's tree is a popover, closed while the file shows.
    place = sidebar.locator('[data-testid="workspace-files-place"]:visible')
    _open_owners(place)
    expect(place.get_by_text("owner-sentinel")).to_be_visible(timeout=30_000)
    expect(page.get_by_role("dialog", name="Workspace files")).to_have_count(0)

    run_palette(page, "Reveal Active File in Files Tree")
    tree = page.get_by_role("dialog", name="Workspace files")
    expect(tree).to_be_visible(timeout=30_000)
    _expect_row_in_view(tree, "OWNERS")
