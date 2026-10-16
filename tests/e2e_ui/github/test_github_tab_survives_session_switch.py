"""The GitHub rail tab must survive switching away to another session and back.

Journey: in session A the user opens the right Workspace rail and selects the
GitHub tab (the gh-integration view). They switch to session B via the sidebar
and browse its Files tab, then switch back to A. The rail must come back on the
GitHub tab — the selected rail tab is persisted per session.

Failure mode this guards: the persisted ``rightRailTab`` is validated against a
runtime allowlist on read; if that allowlist drifts behind the ``RightRailTab``
union (as it did when the GitHub tab shipped), the stored ``"github"`` is
silently dropped and the session-switch restore effect never re-selects it,
leaving the rail on whatever tab the previous session showed.

Like ``test_github_tab.py``, the runner-backed GitHub resource endpoints are
stubbed with ``page.route`` so the tab is visible for both seeded sessions
without a real ``gh``/``git`` PR in the CI workspace. No message is sent, so the
test is fast and LLM-free.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

_PR_NUMBER = 4321

# GET /resources/github — a git checkout with an associated PR, so the GitHub
# tab is visible and its panel renders unmistakable content for the assertions.
_INFO = {
    "object": "session.github.info",
    "available": True,
    "gh_available": True,
    "authenticated": True,
    "branch": "feature/rail-tab",
    "base_ref": "main",
    "repo": {"name_with_owner": "acme/app"},
    "pr": {
        "number": _PR_NUMBER,
        "title": "Persist the GitHub rail tab",
        "state": "OPEN",
        "url": "https://example.com/pr/4321",
        "is_draft": False,
        "author": "octocat",
        "base_ref": "main",
        "head_ref": "feature/rail-tab",
        "checks": {
            "passing": 1,
            "failing": 0,
            "pending": 0,
            "total": 1,
            "runs": [{"name": "unit", "bucket": "passing", "url": None}],
        },
    },
}

# GET /resources/github/changes — one changed file so the panel's tree renders.
_CHANGES = {
    "object": "list",
    "has_more": False,
    "data": [
        {
            "object": "session.github.changed_file",
            "path": "README.md",
            "name": "README.md",
            "status": "modified",
            "lines_added": 1,
            "lines_removed": 0,
        }
    ],
}

# GET /resources/github/diff — the PR as one unified-diff patch.
_PR_DIFF = {
    "object": "session.github.pr_diff",
    "patch": (
        "diff --git a/README.md b/README.md\n"
        "index e69de29..4b825dc 100644\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1,1 +1,2 @@\n"
        " line1\n"
        "+added line\n"
    ),
}


def _stub_github(page: Page) -> None:
    """Answer the runner-backed GitHub endpoints with canned JSON.

    The patterns carry no session id, so both seeded sessions resolve as a git
    checkout with a PR and both show the GitHub tab. Registered before the
    first navigation so the initial fetch is caught.
    """
    page.route(re.compile(r"/resources/github(?:\?|$)"), lambda r: r.fulfill(json=_INFO))
    page.route(re.compile(r"/resources/github/changes"), lambda r: r.fulfill(json=_CHANGES))
    page.route(re.compile(r"/resources/github/diff(?:\?|$)"), lambda r: r.fulfill(json=_PR_DIFF))
    page.route(
        re.compile(r"/resources/github/diff/"),
        lambda r: r.fulfill(
            json={
                "object": "session.github.file_diff",
                "path": "README.md",
                "before": "line1\n",
                "after": "line1\nadded line\n",
            }
        ),
    )


def test_github_tab_survives_session_switch(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Switching A → B → A restores session A's rail to the GitHub tab.

    Failure mode this catches: the per-session ``rightRailTab: "github"`` is
    dropped by the sanitize allowlist on read, so returning to A leaves the
    rail on whatever tab session B was showing (Files) instead of the GitHub
    view the user left A on.
    """
    base_url, session_a, session_b = seeded_session_pair
    _stub_github(page)

    # Session A: open the rail and select the GitHub tab; the PR renders.
    page.goto(f"{base_url}/c/{session_a}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    github_tab = rail.get_by_role("tab", name="GitHub")
    expect(github_tab).to_be_visible(timeout=30_000)
    github_tab.click()
    expect(github_tab).to_have_attribute("aria-selected", "true")
    expect(rail.get_by_text("Persist the GitHub rail tab")).to_be_visible(timeout=30_000)

    # Switch to session B via the sidebar (client-side nav, like a user) and
    # browse its Files tab — B persists Files as its own rail tab.
    page.locator(f'a[href="/c/{session_b}"]').click()
    expect(page).to_have_url(re.compile(re.escape(session_b)), timeout=30_000)
    files_tab = rail.get_by_role("tab", name="Files")
    expect(files_tab).to_be_visible(timeout=30_000)
    files_tab.click()
    expect(files_tab).to_have_attribute("aria-selected", "true")

    # Switch back to session A: the rail must restore to the GitHub tab.
    page.locator(f'a[href="/c/{session_a}"]').click()
    expect(page).to_have_url(re.compile(re.escape(session_a)), timeout=30_000)
    expect(github_tab).to_be_visible(timeout=30_000)
    expect(github_tab).to_have_attribute("aria-selected", "true", timeout=10_000)
    expect(rail.get_by_text("Persist the GitHub rail tab")).to_be_visible(timeout=10_000)
