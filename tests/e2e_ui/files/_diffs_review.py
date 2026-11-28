"""Steps for journeys through the tabs sidebar's Diffs review, on a real host's repository.

The review reads its changes through the workspace API, so these journeys change files in
the ``git_session`` repository itself rather than answering the review's requests.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail
from tests.e2e_ui.sessions.test_workspace_sidebar_v2 import _open_app, _pick_comparison, _set_flag

DESKTOP = {"width": 1440, "height": 900}
# Wide enough for the rail to reach 760px.
WIDE = {"width": 1600, "height": 900}


def commit_then_edit(repository: Path, files: dict[str, tuple[str, str]]) -> None:
    """Commit each path's first text, then leave its second as an uncommitted edit."""
    for path, (before, _after) in files.items():
        (repository / path).parent.mkdir(parents=True, exist_ok=True)
        (repository / path).write_text(before)
    git = ["git", "-c", "user.name=Example", "-c", "user.email=dev@example.com"]
    subprocess.run([*git, "add", "--", *files], cwd=repository, check=True)
    subprocess.run(
        [*git, "commit", "-q", "-m", "Add the reviewed files"], cwd=repository, check=True
    )
    for path, (_before, after) in files.items():
        (repository / path).write_text(after)


def open_diffs(
    page: Page, base_url: str, session_id: str, viewport: dict[str, int] = DESKTOP
) -> tuple[Locator, Locator]:
    """Open the session's Diffs tab on its uncommitted changes; returns the sidebar and review."""
    _set_flag(page, True)
    page.set_viewport_size(viewport)
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    sidebar = page.get_by_role("complementary", name="Workspace")
    expect(sidebar.get_by_role("group", name="Workspace history")).to_be_visible(timeout=60_000)
    _open_app(page, sidebar, "Diffs")
    _pick_comparison(page, sidebar, "Uncommitted changes")
    return sidebar, sidebar.get_by_test_id("diff-review")


def follow_link(page: Page, link: str) -> None:
    """Follow an in-app link, as one clicked in the transcript is, without reloading the page.

    A link opened in a new page load can reach the classic sidebar before the server says
    the tabs sidebar is on; these journeys check the review, not that start-up.
    """
    page.evaluate(
        """(link) => {
            history.pushState(null, "", link);
            dispatchEvent(new PopStateEvent("popstate"));
        }""",
        link,
    )


def workspace_tab(sidebar: Locator, name: str) -> Locator:
    """The strip's tab whose name starts with ``name``."""
    return sidebar.get_by_role("tablist", name="Workspace tabs").get_by_role(
        "tab", name=re.compile(rf"^{re.escape(name)}")
    )


def file_header(review: Locator, path: str) -> Locator:
    """A file's header in the review, by its ⋯ menu."""
    return review.get_by_role("button", name=f"More actions for {path}")


def open_find(page: Page, review: Locator) -> Locator:
    """Open the review's find bar from the keyboard; returns its query field."""
    review.get_by_test_id("diff-review-file-count").click()
    page.keyboard.press("ControlOrMeta+f")
    query = page.get_by_test_id("diff-review-find-input")
    expect(query).to_be_focused()
    return query


def choose_in_tree(page: Page, sidebar: Locator, path: str) -> None:
    """Pick ``path`` in the review's changed-files tree, showing the tree first if it's hidden."""
    show = sidebar.get_by_role("button", name="Show changed files")
    if show.count():
        show.click()
    name = path.rsplit("/", 1)[-1]
    sidebar.get_by_role("complementary", name="Changed files").or_(
        page.get_by_role("dialog", name="Changed files")
    ).first.get_by_role("treeitem", name=re.compile(rf"^{re.escape(name)}")).click()
    # A narrow review's tree is a popover, which stays open across picks.
    popover = page.get_by_role("dialog", name="Changed files")
    if popover.count():
        page.keyboard.press("Escape")
        expect(popover).to_be_hidden()
