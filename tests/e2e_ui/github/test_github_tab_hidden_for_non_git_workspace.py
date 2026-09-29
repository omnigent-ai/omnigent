"""E2E: the GitHub rail tab stays visible for non-Git workspaces.

Previously the tab was hidden when the runner reported not_a_git_repo, leaving
users no way to see why GitHub wasn't working.  Now the tab is always shown
(matching the Files gate) and the panel renders a "Not a git repository" empty
state instead.

The /resources/github endpoint is stubbed via ``page.route`` so the test does
not need a real git checkout — it pins the frontend behaviour for the
not_a_git_repo payload directly.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

_NOT_A_GIT_REPO = {
    "object": "session.github.info",
    "available": False,
    "reason": "not_a_git_repo",
}


def _stub_not_a_git_repo(page: Page) -> None:
    """Answer /resources/github with not_a_git_repo — no real git checkout needed."""
    page.route(
        re.compile(r"/resources/github(?:\?|$)"),
        lambda r: r.fulfill(json=_NOT_A_GIT_REPO),
    )


def test_github_tab_shows_empty_state_for_non_git_workspace(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """GitHub tab is visible and shows the 'Not a git repository' empty state."""
    base_url, session_id = seeded_session
    _stub_not_a_git_repo(page)
    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    # The GitHub tab must be present — the panel handles the empty state.
    github_tab = rail.get_by_role("tab", name="GitHub")
    expect(github_tab).to_be_visible(timeout=30_000)

    # Clicking it must render the empty state, not crash or show a blank panel.
    github_tab.click()
    expect(rail.get_by_text(re.compile(r"Not a git repository"))).to_be_visible(timeout=30_000)
