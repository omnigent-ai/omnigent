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
2. Composer metadata stays aligned and visually grouped, and its PR link opens
   GitHub on desktop and mobile with one or several PRs at different text sizes.
3. A host predating the ``/resources/github`` route 404s "Resource 'github'
   not found", which the panel renders as an actionable "update your host"
   empty state rather than the generic "unavailable" one.

None sends a message, so all stay fast and LLM-free.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry, open_right_rail

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
        # The Summary tab renders the description (markdown) and comments.
        "body": "## Summary\n\nAdds the GitHub tab to the workspace rail.",
        "comments": [
            {
                "author": "octocat",
                "body": "Nice work!",
                "created_at": "2026-09-05T07:32:02Z",
                "url": "https://example.com/pr/4242#c1",
            }
        ],
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


def _stub_github(page: Page, *, pr_count: int = 1) -> None:
    """Answer the runner-backed GitHub endpoints with canned JSON — no real
    ``gh``/``git`` runs. Register before navigating so the first fetch is caught.

    The four patterns are non-overlapping: ``/github`` and ``/github/diff`` end
    at the query/string boundary, so they never swallow ``/github/changes`` or
    the per-file ``/github/diff/<path>``.
    """
    info = _INFO
    if pr_count > 1:
        info = {
            **_INFO,
            "tracking_available": True,
            "selected_pr_url": _INFO["pr"]["url"],
            "prs": [
                {
                    "url": f"https://example.com/pr/{_PR_NUMBER + index}",
                    "host": "example.com",
                    "repository": "acme/app",
                    "number": _PR_NUMBER + index,
                    "relationship": "created",
                }
                for index in range(pr_count)
            ],
        }
    page.route(re.compile(r"/resources/github(?:\?|$)"), lambda r: r.fulfill(json=info))
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


def test_github_tab_shows_summary_checks_and_file_tree(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The GitHub tab lands on Summary; Changes shows the compacted file tree."""
    base_url, session_id = seeded_session
    _stub_github(page)
    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name="GitHub").click()

    # PR header (shared across both inner tabs): title, number, and state.
    expect(rail.get_by_text("Add the GitHub tab")).to_be_visible(timeout=30_000)
    expect(rail.get_by_text(f"#{_PR_NUMBER}")).to_be_visible()
    expect(rail.get_by_label("Pull request status: Open")).to_be_visible()

    # CI checks on their own line as labeled pills; a zero bucket shows nothing.
    expect(rail.get_by_text("Checks")).to_be_visible()
    expect(rail.get_by_text(re.compile(r"3\s*passed"))).to_be_visible()
    expect(rail.get_by_text(re.compile(r"1\s*failed"))).to_be_visible()

    # Summary is the default tab: the PR description + a comment render there.
    expect(rail.get_by_text(re.compile(r"Adds the GitHub tab"))).to_be_visible()
    expect(rail.get_by_text("Nice work!")).to_be_visible()

    # Switching to Changes reveals the sidebar file tree. Scope to the inner
    # "Pull request" tablist — the rail's own tab bar also has a "Changes" tab.
    # The src → app single-child chain compacts into one "src/app" folder row
    # (exact — the diff section header carries the full path and would substring).
    rail.get_by_role("tablist", name="Pull request").get_by_role("tab", name="Changes").click()
    expect(rail.get_by_role("button", name="src/app", exact=True)).to_be_visible()
    expect(rail.get_by_role("button", name=re.compile(r"main\.py")).first).to_be_visible()


@pytest.mark.parametrize(
    "viewport_width",
    [1280, pytest.param(390, marks=pytest.mark.browser_context_args(has_touch=True))],
    ids=["desktop", "mobile"],
)
@pytest.mark.parametrize(
    "font_size", [11, 13, 18], ids=["small-font", "default-font", "large-font"]
)
@pytest.mark.parametrize("pr_count", [1, 2], ids=["single-pr", "multiple-pr"])
def test_composer_pr_link_opens_github_tab(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    viewport_width: int,
    font_size: int,
    pr_count: int,
) -> None:
    """Aligned, grouped metadata uses caption text and opens the appropriate GitHub surface."""
    base_url, session_id = seeded_session
    is_mobile = viewport_width < 768
    workspace = "/workspace/demo-app"
    branch = "feature/pr-link"

    def session_details(route: Route) -> None:
        response = fetch_with_retry(route)
        snapshot = response.json()
        snapshot.update(
            workspace=workspace,
            git_branch=branch,
            host_id="composer-pr-host",
            context_window=1_000_000,
            last_total_tokens=660_000,
        )
        route.fulfill(response=response, json=snapshot)

    page.route(re.compile(rf"/v1/sessions/{session_id}(?:\?.*)?$"), session_details)
    page.route(
        "**/v1/hosts/composer-pr-host/worktrees?*",
        lambda route: route.fulfill(
            json={
                "data": [{"path": workspace, "branch": branch, "is_main": True, "detached": False}]
            }
        ),
    )
    _stub_github(page, pr_count=pr_count)
    page.add_init_script(f"localStorage.setItem('omnigent:ui-font-size', '{font_size}')")
    page.set_viewport_size({"width": viewport_width, "height": 844 if is_mobile else 900})
    page.goto(f"{base_url}/c/{session_id}")

    pr_link = page.get_by_test_id("composer-pr-link")
    expect(pr_link).to_be_visible(timeout=30_000)
    expect(pr_link).to_have_accessible_name(f"#{_PR_NUMBER}" if pr_count == 1 else "2 PRs")
    expect(page.get_by_test_id("composer-workspace-dir")).to_have_text("demo-app")
    expect(page.get_by_test_id("composer-git-branch")).to_have_text(branch)
    context = page.get_by_test_id("composer-context-ring")
    expect(context).to_have_text("66%")
    bar = page.get_by_test_id("composer-workspace-controls")
    expect(bar.get_by_test_id("background-task-pill")).to_have_count(0)
    expect(bar.get_by_test_id("subagent-task-pill")).to_have_count(0)
    bar_bounds = bar.bounding_box()
    assert bar_bounds is not None
    font_sizes = {}
    centers = {}
    bounds = {}
    for test_id in (
        "composer-pr-link",
        "composer-workspace-dir",
        "composer-git-branch",
        "composer-context-ring",
    ):
        indicator = page.get_by_test_id(test_id)
        label = indicator.locator("span").last
        expect(label).to_be_visible()
        font_sizes[test_id] = label.evaluate("el => parseFloat(getComputedStyle(el).fontSize)")
        for part, element in (("label", label), ("icon", indicator.locator("svg").first)):
            rect = element.bounding_box()
            assert rect is not None
            bounds[f"{test_id}.{part}"] = rect
            centers[f"{test_id}.{part}"] = rect["y"] + rect["height"] / 2
    pr_bounds, context_bounds = pr_link.bounding_box(), context.bounding_box()
    assert pr_bounds is not None and context_bounds is not None
    group_gap = context_bounds["x"] - pr_bounds["x"] - pr_bounds["width"]
    pair_gaps = {}
    for test_id in ("composer-pr-link", "composer-context-ring"):
        icon, label = bounds[f"{test_id}.icon"], bounds[f"{test_id}.label"]
        pair_gaps[test_id] = label["x"] - icon["x"] - icon["width"]
    painted_right_edges = {
        "composer-pr-link": pr_link.locator("path").evaluate(
            "path => path.getBoundingClientRect().right"
        ),
        "composer-context-ring": context.locator("circle").first.evaluate("""circle => {
            const stroke = parseFloat(getComputedStyle(circle).strokeWidth);
            return circle.getBoundingClientRect().right + stroke * circle.getScreenCTM().a / 2;
        }"""),
    }
    painted_gaps = {
        test_id: bounds[f"{test_id}.label"]["x"] - right
        for test_id, right in painted_right_edges.items()
    }
    print(f"Composer fonts ({viewport_width}px, {font_size}px preference): {font_sizes}")
    print(f"Composer centers: {centers}; group gap: {group_gap}; pair gaps: {pair_gaps}")
    print(f"Visible icon-to-label gaps: {painted_gaps}")
    bar.screenshot(path=tmp_path / "workspace-bar.png", animations="disabled")
    page.screenshot(path=tmp_path / "composer-pr-link.png", animations="disabled")

    # Mobile has a full-screen drawer, not the desktop workspace tab strip.
    if is_mobile:
        pr_link.tap()
    else:
        pr_link.click()
    panel = (
        page.get_by_test_id("github-panel-drawer")
        if is_mobile
        else page.get_by_role("complementary", name="Workspace")
    )
    try:
        if is_mobile:
            expect(panel).to_have_attribute("data-state", "open")
        else:
            expect(panel.get_by_role("tab", name="GitHub")).to_have_attribute(
                "aria-selected", "true"
            )
        expect(panel.get_by_text("Add the GitHub tab", exact=True)).to_be_visible(timeout=30_000)
        expect(panel.get_by_label("Pull request status: Open")).to_be_visible()
        expect(panel.get_by_text("Nice work!", exact=True)).to_be_visible()
        if pr_count > 1:
            expect(panel.get_by_role("combobox", name="Session pull request")).to_have_text(
                f"example.com/acme/app #{_PR_NUMBER}"
            )
    finally:
        page.screenshot(path=tmp_path / "composer-pr-link-after-click.png", animations="disabled")

    if is_mobile:
        panel.get_by_role("button", name="Close", exact=True).click()
        expect(panel).to_have_attribute("data-state", "closed")
        expect(pr_link).to_be_in_viewport()

    expected_font_size = font_size * 0.9 * (14 / 13 if is_mobile else 1)
    for test_id, actual_font_size in font_sizes.items():
        assert actual_font_size == pytest.approx(expected_font_size, abs=0.01), (
            f"{test_id} should use the caption size at {font_size}px preference: {font_sizes}"
        )
    reference_center = centers["composer-workspace-dir.icon"]
    assert bar_bounds["height"] == pytest.approx(37, abs=0.1)
    assert reference_center == pytest.approx(bar_bounds["y"] + 19, abs=0.5)
    for name, center in centers.items():
        assert center == pytest.approx(reference_center, abs=0.5), (name, centers)
    for name, pair_gap in pair_gaps.items():
        assert pair_gap == pytest.approx(4, abs=0.1), (name, pair_gaps)
        assert group_gap > pair_gap, (group_gap, pair_gaps)
    for name, painted_gap in painted_gaps.items():
        assert painted_gap == pytest.approx(4, abs=0.1), (name, painted_gaps)
    assert group_gap == pytest.approx(8, abs=0.1)


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
