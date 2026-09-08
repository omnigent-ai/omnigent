"""E2E: the GitHub rail tab must be hidden for non-Git workspaces.

Regression: the workspace rail showed the GitHub tab even when the session's
workspace worktree is not a Git repository, and clicking it opened an unusable
empty panel that just says "This workspace isn't a git repository."

No stubbing: the ``seeded_session`` workspace is the runner's per-session
tmp directory, which genuinely is not a git checkout, so the real
``/resources/github`` endpoint answers ``available: false`` with
``reason: not_a_git_repo`` and the journey runs end-to-end against live code.

Behavior pinned here: once GitHub info resolves to "not a git repo", the
GitHub tab is absent from the rail's tab strip. On the bug (AppShell keying
``railTabsAvailable.github`` off the workspace/Files gate alone) this test
fails; it passes once the tab is hidden.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, Response, expect

from tests.e2e_ui.conftest import open_right_rail


def _is_github_info_response(response: Response) -> bool:
    """Match the session's GitHub info fetch (not ``/changes`` or ``/diff``)."""
    return (
        response.request.method == "GET"
        and response.status == 200
        and re.search(r"/resources/github(?:\?|$)", response.url) is not None
    )


def test_github_tab_hidden_for_non_git_workspace(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A non-git workspace must not surface the GitHub tab in the rail."""
    base_url, session_id = seeded_session

    # Open the session and wait for the SPA's own GitHub-info fetch (the
    # composer status line issues it on load); its payload is the signal a
    # fix must key the tab's visibility on. This also pins the precondition:
    # the seeded workspace really is a non-git worktree (no stubs involved).
    with page.expect_response(_is_github_info_response, timeout=90_000) as info:
        page.goto(f"{base_url}/c/{session_id}")
    payload = info.value.json()
    assert payload.get("available") is False, f"workspace unexpectedly a git repo: {payload}"
    assert payload.get("reason") == "not_a_git_repo", payload

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    # Anchor on tabs that must exist regardless of the fix, so the tab strip
    # has fully rendered before the GitHub tab is sampled: Agents is
    # unconditional, and Files shares the workspace gate the GitHub tab
    # currently (wrongly) reuses.
    expect(rail.get_by_role("tab", name=re.compile(r"^Agents"))).to_be_visible(timeout=30_000)
    expect(rail.get_by_role("tab", name="Files")).to_be_visible(timeout=30_000)

    github_tab = rail.get_by_role("tab", name="GitHub")
    if github_tab.count() > 0:
        # The bug: the tab is present for a non-git workspace. Drive the rest
        # of the reported journey — clicking it opens the dead-end panel —
        # then fail pointedly so the regression is unambiguous.
        github_tab.click()
        expect(rail.get_by_text(re.compile(r"isn.t a git repository"))).to_be_visible(
            timeout=30_000
        )
        pytest.fail(
            "GitHub tab is shown for a non-Git workspace and opens a dead-end "
            "'This workspace isn't a git repository.' panel"
        )

    # Fixed behavior: the tab never materializes once info says not-a-git-repo.
    expect(github_tab).to_have_count(0)
