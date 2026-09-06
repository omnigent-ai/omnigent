"""E2E: the read-only GitHub rail tab, driven entirely from stubbed responses.

The GitHub tab's data comes from the runner-backed resource endpoints
(``/v1/sessions/{id}/resources/github*``), which normally shell out to ``gh``
and ``git`` in the workspace. Here every one of those endpoints is intercepted
with ``page.route`` and answered with canned JSON, so the test exercises the
*frontend* — the PR header, the CI-check pills, and the folder-tree sidebar —
without a real ``gh``/``git`` (which a CI workspace has no PR for anyway).

Three behaviours are pinned:

1. Opening the GitHub rail tab renders the associated PR (title + number), its
   CI checks as labeled pills, and the branch-vs-base file tree — with a
   single-child directory chain (``src`` → ``app``) compacted into one row.
2. The composer status line's ``#<pr>`` link opens that tab.
3. A host predating the ``/resources/github`` route 404s "Resource 'github'
   not found", which the panel renders as an actionable "update your host"
   empty state rather than the generic "unavailable" one.

None sends a message, so all stay fast and LLM-free.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

_PR_NUMBER = 4242

# GET /resources/github — repo/branch/base + the associated PR and CI summary.
_INFO = {
    "object": "session.github.info",
    "available": True,
    "gh_available": True,
    "authenticated": True,
    "branch": "feature/github-tab",
    "base_ref": "main",
    "repo": {"name_with_owner": "acme/app"},
    "pr": {
        "number": _PR_NUMBER,
        "title": "Add the GitHub tab",
        "state": "OPEN",
        "url": "https://example.com/pr/4242",
        "is_draft": False,
        "author": "octocat",
        "base_ref": "main",
        "head_ref": "feature/github-tab",
        "checks": {
            "passing": 3,
            "failing": 1,
            "pending": 0,
            "total": 4,
            "runs": [
                {"name": "unit", "bucket": "passing", "url": None},
                {"name": "lint", "bucket": "passing", "url": None},
                {"name": "types", "bucket": "passing", "url": None},
                {"name": "e2e", "bucket": "failing", "url": None},
            ],
        },
    },
}

# GET /resources/github/changes — files changed vs the base. ``src`` → ``app``
# is a single-child chain the sidebar compacts into one "src/app" row.
_CHANGES = {
    "object": "list",
    "has_more": False,
    "data": [
        {
            "object": "session.github.changed_file",
            "path": "src/app/main.py",
            "name": "main.py",
            "status": "modified",
            "lines_added": 10,
            "lines_removed": 2,
        },
        {
            "object": "session.github.changed_file",
            "path": "README.md",
            "name": "README.md",
            "status": "created",
            "lines_added": 5,
            "lines_removed": 0,
        },
    ],
}

# GET /resources/github/diff — the whole PR as one unified-diff patch.
_PR_DIFF = {
    "object": "session.github.pr_diff",
    "patch": (
        "diff --git a/src/app/main.py b/src/app/main.py\n"
        "index e69de29..4b825dc 100644\n"
        "--- a/src/app/main.py\n"
        "+++ b/src/app/main.py\n"
        "@@ -1,2 +1,3 @@\n"
        " line1\n"
        "+added line\n"
        " line2\n"
    ),
}


def _stub_github(page: Page) -> None:
    """Answer the runner-backed GitHub endpoints with canned JSON — no real
    ``gh``/``git`` runs. Register before navigating so the first fetch is caught.

    The four patterns are non-overlapping: ``/github`` and ``/github/diff`` end
    at the query/string boundary, so they never swallow ``/github/changes`` or
    the per-file ``/github/diff/<path>``.
    """
    page.route(re.compile(r"/resources/github(?:\?|$)"), lambda r: r.fulfill(json=_INFO))
    page.route(re.compile(r"/resources/github/changes"), lambda r: r.fulfill(json=_CHANGES))
    page.route(re.compile(r"/resources/github/diff(?:\?|$)"), lambda r: r.fulfill(json=_PR_DIFF))
    page.route(
        re.compile(r"/resources/github/diff/"),
        lambda r: r.fulfill(
            json={
                "object": "session.github.file_diff",
                "path": "src/app/main.py",
                "before": "line1\nline2\n",
                "after": "line1\nadded line\nline2\n",
            }
        ),
    )


def test_github_tab_shows_pr_checks_and_file_tree(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The GitHub tab renders the PR, its CI pills, and the compacted file tree."""
    base_url, session_id = seeded_session
    _stub_github(page)
    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name="GitHub").click()

    # PR header: title + number.
    expect(rail.get_by_text("Add the GitHub tab")).to_be_visible(timeout=30_000)
    expect(rail.get_by_text(f"#{_PR_NUMBER}")).to_be_visible()

    # CI checks on their own line as labeled pills; a zero bucket shows nothing.
    expect(rail.get_by_text("Checks")).to_be_visible()
    expect(rail.get_by_text(re.compile(r"3\s*passed"))).to_be_visible()
    expect(rail.get_by_text(re.compile(r"1\s*failed"))).to_be_visible()

    # Sidebar file tree: the src → app single-child chain compacts into one
    # "src/app" folder row (exact match — the diff section header carries the
    # full path and would match a substring).
    expect(rail.get_by_role("button", name="src/app", exact=True)).to_be_visible()
    expect(rail.get_by_role("button", name=re.compile(r"main\.py")).first).to_be_visible()


def test_composer_pr_link_opens_github_tab(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The composer status line's #<pr> link opens the GitHub tab."""
    base_url, session_id = seeded_session
    _stub_github(page)
    page.goto(f"{base_url}/c/{session_id}")

    # The link appears once GitHub info resolves (a PR is associated).
    pr_link = page.get_by_test_id("composer-pr-link")
    expect(pr_link).to_be_visible(timeout=30_000)
    expect(pr_link).to_contain_text(f"#{_PR_NUMBER}")

    # Clicking it opens the rail on the GitHub tab with the PR loaded.
    pr_link.click()
    rail = page.get_by_role("complementary", name="Workspace")
    expect(rail.get_by_text("Add the GitHub tab")).to_be_visible(timeout=30_000)


def _stub_github_outdated_host(page: Page) -> None:
    """404 the info endpoint with the message an outdated host returns.

    A host predating the ``/resources/github`` route has no such resource, so
    its generic lookup 404s "Resource 'github' not found". The status MUST be
    set explicitly (``fulfill`` defaults to 200), and the body carries the exact
    message the client keys on (``githubNotFoundReason``). Only ``/resources/
    github`` needs stubbing: an unavailable payload resolves no base ref, so the
    changes/diff queries stay disabled and never fire.
    """
    page.route(
        re.compile(r"/resources/github(?:\?|$)"),
        lambda r: r.fulfill(
            status=404,
            headers={"content-type": "application/json"},
            body=json.dumps({"error": {"message": "Resource 'github' not found"}}),
        ),
    )


def test_github_tab_prompts_to_update_outdated_host(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """An outdated host's 404 renders the "update your host" empty state.

    Pins the full old-host chain end to end: the 404 body → ``githubNotFoundReason``
    → the ``host_outdated`` state → the actionable empty state, rather than the
    generic "GitHub isn't available" one.
    """
    base_url, session_id = seeded_session
    _stub_github_outdated_host(page)
    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name="GitHub").click()

    expect(rail.get_by_text("Update your host to use GitHub")).to_be_visible(timeout=30_000)
    # The hint names the version floor so the user knows what to update to.
    expect(rail.get_by_text(re.compile(r"0\.13\.0 or later"))).to_be_visible()
