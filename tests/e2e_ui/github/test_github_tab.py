"""E2E: the read-only Pull Requests rail tab, driven entirely from stubbed responses.

The tab's data comes from the runner-backed resource endpoints
(``/v1/sessions/{id}/resources/github*``), which normally shell out to ``gh``
and ``git`` in the workspace. Here every one of those endpoints is intercepted
with ``page.route`` and answered with canned JSON, so the test exercises the
*frontend* — the PR header, the CI-check pills, and the folder-tree sidebar —
without a real ``gh``/``git`` (which a CI workspace has no PR for anyway).

Three behaviours are pinned:

1. Opening the Pull Requests rail tab renders the associated PR (title + number), its
   CI checks as labeled pills, and the branch-vs-base file tree — with a
   single-child directory chain (``src`` → ``app``) compacted into one row.
2. Composer metadata stays aligned and visually grouped, and its PR link opens
   the tab on desktop and mobile with one or several PRs at different text sizes.
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
from playwright.sync_api import Locator, Page, Route, expect

from tests._helpers.pull_request_ui import open_session_pull_requests
from tests.e2e_ui.conftest import (
    fetch_with_retry,
    open_right_rail,
    stub_session_pull_requests,
    workspace_bar_needs_collapse,
)

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
        "reviewers": [
            {"login": "alice", "kind": "user", "state": "APPROVED"},
            {"login": "platform", "kind": "team", "state": "PENDING"},
        ],
        "base_ref": "main",
        "head_ref": "feature/github-tab",
        "checks": {
            "passing": 3,
            "failing": 1,
            "pending": 0,
            "cancelled": 1,
            "skipped": 1,
            "total": 6,
            "runs": [
                {"name": "unit", "bucket": "passing", "url": None},
                {"name": "lint", "bucket": "passing", "url": None},
                {"name": "types", "bucket": "passing", "url": None},
                {"name": "e2e", "bucket": "failing", "url": None},
                {"name": "superseded build", "bucket": "cancelled", "url": None},
                {"name": "optional deploy", "bucket": "skipped", "url": None},
            ],
        },
        # The Summary tab renders the description (markdown); comments get their own tab.
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
    # The server's stored list matches the live payload it would have answered.
    stub_session_pull_requests(page, lambda: info["prs"], lambda _url: None)
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


@pytest.mark.parametrize("entry", ["rail", "canvas"])
def test_github_tab_shows_summary_checks_and_file_tree(
    page: Page,
    seeded_session: tuple[str, str],
    entry: str,
) -> None:
    """The Pull Requests tab lands on Summary; Comments and Changes are one click away."""
    base_url, session_id = seeded_session
    _stub_github(page)
    if entry == "canvas":
        from tests.e2e_ui.sessions.test_canvas_page import _stub_server_info

        _stub_server_info(page, canvas=True, session_pull_requests=True)
    page.goto(f"{base_url}/{'canvas/' if entry == 'canvas' else ''}c/{session_id}")
    rail = page.get_by_role("complementary", name="Workspace")
    if entry == "canvas":
        open_session_pull_requests(page, mobile=False)
    else:
        open_right_rail(page)
        rail.get_by_role("tab", name="Pull Requests").click()

    # PR header (shared across both inner tabs): title, number, and state.
    expect(rail.get_by_text("Add the GitHub tab")).to_be_visible(timeout=30_000)
    expect(rail.get_by_role("list", name="Pull requests")).to_have_count(0)
    expect(rail.get_by_role("button", name=re.compile("All pull requests"))).to_have_count(0)
    expect(rail.get_by_text(f"#{_PR_NUMBER}")).to_be_visible()
    expect(rail.get_by_label("Pull request status: Open")).to_be_visible()
    expect(rail.get_by_role("listitem", name="alice: Approved")).to_be_visible()
    expect(rail.get_by_role("listitem", name="Team platform: Pending")).to_be_visible()

    # The provider belongs to the request, so its glyph sits beside the title.
    icon = rail.get_by_title("GitHub", exact=True).bounding_box()
    title = rail.get_by_text("Add the GitHub tab").bounding_box()
    assert icon is not None and title is not None
    assert abs(icon["y"] - title["y"]) < 10

    # CI checks on their own line as labeled pills; a zero bucket shows nothing.
    expect(rail.get_by_text("Checks")).to_be_visible()
    expect(rail.get_by_text(re.compile(r"3\s*passed"))).to_be_visible()
    expect(rail.get_by_text(re.compile(r"1\s*failed"))).to_be_visible()
    for label, job in [("cancelled", "superseded build"), ("skipped", "optional deploy")]:
        pill = rail.get_by_role("button", name=f"1 {label}", exact=True)
        expect(pill).to_be_visible()
        expect(pill).to_have_class(re.compile(r"\bbg-muted\b"))
        expect(pill).to_have_class(re.compile(r"\btext-muted-foreground\b"))
        pill.hover()
        expect(page.get_by_text(job, exact=True)).to_be_visible()
        expect(page.get_by_text("e2e", exact=True)).not_to_be_visible()
        rail.get_by_text("Checks", exact=True).hover()
        expect(page.get_by_text(job, exact=True)).not_to_be_visible()

    # Summary is the default tab: the PR description renders there.
    expect(rail.get_by_text(re.compile(r"Adds the GitHub tab"))).to_be_visible()
    expect(rail.get_by_text("Nice work!")).to_have_count(0)

    # Comments have their own tab, labeled with the count. Scope to the inner
    # "Pull request" tablist — the rail's own tab bar also has a "Changes" tab.
    pr_tabs = rail.get_by_role("tablist", name="Pull request")
    pr_tabs.get_by_role("tab", name="Comments 1", exact=True).click()
    expect(rail.get_by_text("Nice work!")).to_be_visible()

    # Switching to Changes reveals the sidebar file tree. The src → app
    # single-child chain compacts into one "src/app" folder row (exact — the
    # diff section header carries the full path and would substring).
    pr_tabs.get_by_role("tab", name="Changes").click()
    expect(rail.get_by_role("button", name="src/app", exact=True)).to_be_visible()
    expect(rail.get_by_role("button", name=re.compile(r"main\.py")).first).to_be_visible()

    rail.get_by_role("tablist", name="Pull request").get_by_role("tab", name="Summary").click()
    expect(rail.get_by_role("listitem", name="alice: Approved")).to_be_visible()


@pytest.mark.parametrize("entry", ["rail", "composer-desktop", "composer-mobile"])
def test_pr_diff_retry_keeps_associations(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path, entry: str
) -> None:
    base_url, session_id = seeded_session
    _stub_github(page, pr_count=2)
    failed = True

    def diff(route: Route) -> None:
        if failed:
            route.fulfill(
                status=400, json={"error": {"message": "GitHub could not load the PR diff."}}
            )
        else:
            route.fulfill(json=_PR_DIFF)

    page.route(re.compile(r"/resources/github/diff(?:\?|$)"), diff)
    mobile = entry == "composer-mobile"
    page.set_viewport_size({"width": 390 if mobile else 1280, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    panel = (
        page.get_by_test_id("github-panel-drawer")
        if mobile
        else page.get_by_role("complementary", name="Workspace")
    )
    if entry == "rail":
        open_right_rail(page)
        panel.get_by_role("tab", name="Pull Requests").click()
    else:
        open_session_pull_requests(page, mobile=mobile)
    panel.get_by_role(
        "button", name=f"example.com/acme/app #{_PR_NUMBER} — Add the GitHub tab", exact=True
    ).click()
    panel.get_by_role("tablist", name="Pull request", exact=True).get_by_role(
        "tab", name="Changes", exact=True
    ).click()
    expect(panel.get_by_role("alert")).to_have_text("GitHub could not load the PR diff.")
    expect(panel.get_by_role("button", name="All pull requests (2)")).to_be_visible()
    expect(panel.get_by_text("No changes vs base.")).to_have_count(0)
    page.screenshot(path=str(tmp_path / f"pr-retry-{entry}-error.png"))
    failed = False
    panel.get_by_role("button", name="Refresh", exact=True).click()
    expect(panel.get_by_role("alert")).to_have_count(0)
    expect(panel.locator("diffs-container")).to_have_count(1)
    expect(panel.get_by_role("button", name="All pull requests (2)")).to_be_visible()
    page.screenshot(path=str(tmp_path / f"pr-retry-{entry}-recovered.png"))


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
                "data": [
                    {
                        "path": workspace,
                        "branch": branch,
                        "is_main": False,
                        "detached": False,
                    }
                ]
            }
        ),
    )
    _stub_github(page, pr_count=pr_count)
    page.add_init_script(f"localStorage.setItem('omnigent:ui-font-size', '{font_size}')")
    page.set_viewport_size({"width": viewport_width, "height": 844 if is_mobile else 900})
    page.goto(f"{base_url}/c/{session_id}")

    if is_mobile:
        expect(page.get_by_test_id("composer-workspace-controls")).to_have_count(0)
        pr_link = page.get_by_role(
            "button", name=re.compile(r"^(?:Conversation|Session) actions$")
        )
        pr_link.click()
        expect(page.get_by_text("demo-app", exact=True)).to_be_visible()
        page.get_by_role("menuitem", name="Pull Requests", exact=True).click()
    else:
        pr_link = page.get_by_test_id("composer-pr-link")
        expect(pr_link).to_be_visible(timeout=30_000)
        expect(pr_link).to_have_accessible_name(f"#{_PR_NUMBER}" if pr_count == 1 else "2 PRs")
        expect(page.get_by_test_id("composer-workspace-dir")).to_have_text("demo-app")
        expect(page.get_by_test_id("composer-git-branch")).to_have_text(branch)
        context = page.get_by_test_id("composer-context-ring")
        expect(context).to_have_accessible_name("66% of context used")
        bar = page.get_by_test_id("composer-workspace-controls")
        expect(bar.get_by_test_id("background-task-pill")).to_have_count(0)
        expect(bar.get_by_test_id("subagent-task-pill")).to_have_count(0)
        bar_bounds = bar.bounding_box()
        assert bar_bounds is not None
        # When the labels cannot all show in full, the bar collapses to icons (never
        # ellipses); the collapse must be justified by the expanded layout not fitting.
        collapsed = bar.get_attribute("data-labels") == "collapsed"
        assert collapsed == workspace_bar_needs_collapse(bar), (
            viewport_width,
            font_size,
            pr_count,
        )
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
            parts = [("icon", indicator.locator("svg").first)]
            # Only the directory and branch text collapse; the PR number and the
            # context percentage stay visible in a crowded bar.
            if collapsed and test_id in ("composer-workspace-dir", "composer-git-branch"):
                expect(label).to_be_hidden()
            elif test_id != "composer-context-ring":
                expect(label).to_be_visible()
                font_sizes[test_id] = label.evaluate(
                    "el => parseFloat(getComputedStyle(el).fontSize)"
                )
                parts.insert(0, ("label", label))
            for part, element in parts:
                rect = element.bounding_box()
                assert rect is not None
                bounds[f"{test_id}.{part}"] = rect
                centers[f"{test_id}.{part}"] = rect["y"] + rect["height"] / 2
        pr_bounds, context_bounds = pr_link.bounding_box(), context.bounding_box()
        assert pr_bounds is not None and context_bounds is not None
        group_gap = context_bounds["x"] - pr_bounds["x"] - pr_bounds["width"]
        pair_gaps = {}
        for test_id in ("composer-pr-link",):
            icon, label = bounds[f"{test_id}.icon"], bounds[f"{test_id}.label"]
            pair_gaps[test_id] = label["x"] - icon["x"] - icon["width"]
        print(f"Composer fonts ({viewport_width}px, {font_size}px preference): {font_sizes}")
        print(f"Composer centers: {centers}; group gap: {group_gap}; pair gaps: {pair_gaps}")
        bar.screenshot(path=tmp_path / "workspace-bar.png", animations="disabled")
        page.screenshot(path=tmp_path / "composer-pr-link.png", animations="disabled")

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
            expect(panel.get_by_role("tab", name="Pull Requests")).to_have_attribute(
                "aria-selected", "true"
            )
        if pr_count > 1:
            panel.get_by_role(
                "button",
                name=f"example.com/acme/app #{_PR_NUMBER} — Add the GitHub tab",
                exact=True,
            ).click()
        expect(panel.get_by_text("Add the GitHub tab", exact=True)).to_be_visible(timeout=30_000)
        expect(panel.get_by_label("Pull request status: Open")).to_be_visible()
        expect(
            panel.get_by_role("tablist", name="Pull request").get_by_role(
                "tab", name="Comments 1", exact=True
            )
        ).to_be_visible()
        if pr_count > 1:
            expect(panel.get_by_role("button", name="All pull requests (2)")).to_be_visible()
        else:
            expect(panel.get_by_role("list", name="Pull requests")).to_have_count(0)
            expect(
                panel.get_by_role("button", name=re.compile("All pull requests"))
            ).to_have_count(0)
    finally:
        page.screenshot(path=tmp_path / "composer-pr-link-after-click.png", animations="disabled")

    if is_mobile:
        panel.get_by_role("button", name="Close", exact=True).click()
        expect(panel).to_have_attribute("data-state", "closed")
        expect(pr_link).to_be_in_viewport()
        return

    expected_font_size = font_size - 1
    for test_id, actual_font_size in font_sizes.items():
        assert actual_font_size == pytest.approx(expected_font_size, abs=0.01), (
            f"{test_id} should use the caption size at {font_size}px preference: {font_sizes}"
        )
    reference_center = centers["composer-workspace-dir.icon"]
    pr_center = centers["composer-pr-link.icon"]
    if not is_mobile:
        assert bar_bounds["height"] == pytest.approx(37, abs=0.1)
        assert reference_center == pytest.approx(bar_bounds["y"] + 19, abs=0.5)
        assert pr_center == pytest.approx(reference_center, abs=0.5)
    for name, center in centers.items():
        row_center = (
            pr_center
            if name.startswith(("composer-pr-link.", "composer-context-ring."))
            else reference_center
        )
        assert center == pytest.approx(row_center, abs=0.5), (name, centers)
    for name, pair_gap in pair_gaps.items():
        assert pair_gap == pytest.approx(4, abs=0.1), (name, pair_gaps)
    directory_bounds = page.get_by_test_id("composer-workspace-dir").bounding_box()
    branch_bounds = page.get_by_test_id("composer-git-branch").bounding_box()
    assert directory_bounds is not None and branch_bounds is not None
    selector_gaps = [
        branch_bounds["x"] - directory_bounds["x"] - directory_bounds["width"],
    ]
    if abs(pr_center - reference_center) <= 0.5:
        selector_gaps.append(pr_bounds["x"] - branch_bounds["x"] - branch_bounds["width"])
    else:
        assert is_mobile
        assert pr_bounds["y"] >= branch_bounds["y"] + branch_bounds["height"]
        assert pr_bounds["x"] == pytest.approx(directory_bounds["x"], abs=0.5)
    for rect in (directory_bounds, branch_bounds, pr_bounds, context_bounds):
        assert rect["x"] >= bar_bounds["x"]
        assert rect["x"] + rect["width"] <= bar_bounds["x"] + bar_bounds["width"]
        assert rect["y"] >= bar_bounds["y"]
        assert rect["y"] + rect["height"] <= bar_bounds["y"] + bar_bounds["height"]
    expected_selector_gap = 2 if is_mobile else 8
    for gap in selector_gaps:
        assert gap == pytest.approx(expected_selector_gap, abs=0.1), selector_gaps
    assert context_bounds["x"] >= pr_bounds["x"] + pr_bounds["width"]
    assert group_gap > 0


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
    generic "Pull requests aren't available" one.
    """
    base_url, session_id = seeded_session
    _stub_github_outdated_host(page)
    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name="Pull Requests").click()

    expect(rail.get_by_text("Update your host to use the Pull Requests tab")).to_be_visible(
        timeout=30_000
    )
    # The hint names the version floor so the user knows what to update to.
    expect(rail.get_by_text(re.compile(r"0\.13\.0 or later"))).to_be_visible()


def test_no_pr_keeps_linking_when_another_provider_discovery_fails(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """A secondary remote's warning leaves the GitHub empty state actionable."""
    base_url, session_id = seeded_session
    page.route(
        re.compile(r"/resources/github(?:\?|$)"),
        lambda route: route.fulfill(
            json={
                **_INFO,
                "pr": None,
                "prs": [],
                "tracking_available": True,
                "discovery_warnings": ["GitLab discovery failed. Refresh to retry."],
            }
        ),
    )
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name="Pull Requests").click()
    expect(rail.get_by_text("No open PR for", exact=False)).to_be_visible(timeout=30_000)
    expect(rail.get_by_role("status")).to_have_text("GitLab discovery failed. Refresh to retry.")
    rail.get_by_role("button", name="Link a PR", exact=True).click()
    expect(rail.get_by_role("textbox", name="Pull request URL")).to_be_visible()


@pytest.mark.parametrize("entry", ["rail", "composer-desktop", "composer-mobile", "canvas"])
def test_post_top_level_pr_comment(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path, entry: str
) -> None:
    """Comments and push history share a feed through every PR-panel entry point."""
    base_url, session_id = seeded_session
    _stub_github(page, pr_count=2)
    pr_url = _INFO["pr"]["url"]
    comments = [
        {
            **_INFO["pr"]["comments"][0],
            "body": f"Earlier comment {index + 1}",
            "url": f"{pr_url}#comment-{index}",
            "created_at": "2026-10-25T06:00:00Z" if index == 29 else "2026-09-05T07:32:02Z",
        }
        for index in range(30)
    ]
    info = {
        **_INFO,
        "tracking_available": True,
        "selected_pr_url": pr_url,
        "capabilities": {"comment_posting": True},
        "selected_account": "octocat",
        "pr": {
            **_INFO["pr"],
            "comments": comments,
            "timeline_events": [
                {
                    "id": "commit-1",
                    "kind": "commit",
                    "author": "octocat",
                    "created_at": "2026-10-24T12:00:00Z",
                    "url": "https://example.com/acme/app/commit/" + "a" * 40,
                    "sha": "a" * 40,
                    "message": "Address review feedback",
                },
                {
                    "id": "force-push-1",
                    "kind": "force_push",
                    "author": "octocat",
                    "created_at": "2026-10-25T12:00:00Z",
                    "url": "https://example.com/acme/app/commit/" + "b" * 40,
                    "sha": "b" * 40,
                    "previous_sha": "a" * 40,
                },
            ],
        },
    }
    page.route(re.compile(r"/resources/github(?:\?|$)"), lambda route: route.fulfill(json=info))
    posts = []

    def post_comment(route: Route) -> None:
        posts.append(route.request.post_data_json)
        comment = {
            "author": "octocat",
            "body": posts[-1]["body"],
            "created_at": "2026-10-26T12:00:00Z",
            "url": pr_url + "#issuecomment-99",
        }
        info["pr"]["comments"].append(comment)
        route.fulfill(json={"object": "session.github.pr_comment", "comment": comment})

    page.route(re.compile(r"/resources/github/comments$"), post_comment)
    mobile = entry == "composer-mobile"
    page.set_viewport_size({"width": 390 if mobile else 1280, "height": 900})
    if entry == "canvas":
        from tests.e2e_ui.sessions.test_canvas_page import _stub_server_info

        _stub_server_info(page, canvas=True, session_pull_requests=True)
    page.goto(f"{base_url}/{'canvas/' if entry == 'canvas' else ''}c/{session_id}")
    panel = (
        page.get_by_test_id("github-panel-drawer")
        if mobile
        else page.get_by_role("complementary", name="Workspace")
    )
    if entry == "rail":
        open_right_rail(page)
        panel.get_by_role("tab", name="Pull Requests").click()
    else:
        open_session_pull_requests(page, mobile=mobile)
    panel.get_by_role(
        "button", name="example.com/acme/app #4242 — Add the GitHub tab", exact=True
    ).click()
    tabs = panel.get_by_role("tablist", name="Pull request", exact=True)
    tabs.get_by_role("tab", name="Comments 30", exact=True).click()
    textbox = panel.get_by_role("textbox", name="New PR comment")
    post_button = panel.get_by_role("button", name="Post comment", exact=True)
    expect(textbox).to_be_in_viewport()
    expect(post_button).to_be_in_viewport()
    feed = panel.get_by_role("region", name="PR comments", exact=True)
    assert feed.evaluate("el => el.scrollHeight > el.clientHeight")
    composer_top = textbox.evaluate("el => el.getBoundingClientRect().top")
    feed.evaluate("el => { el.scrollTop = el.scrollHeight; }")
    expect(panel.get_by_text("Earlier comment 30", exact=True)).to_be_in_viewport()
    rows = feed.get_by_role("listitem")
    expect(rows).to_have_count(32)
    expect(rows.nth(29)).to_contain_text("committed")
    expect(rows.nth(29)).to_contain_text("Address review feedback")
    expect(
        rows.nth(29).get_by_role("link", name="Open commit aaaaaaa on GitHub", exact=True)
    ).to_have_attribute("href", "https://example.com/acme/app/commit/" + "a" * 40)
    expect(rows.nth(30)).to_contain_text("Earlier comment 30")
    expect(rows.nth(31)).to_contain_text("force-pushed")
    expect(rows.nth(31)).to_contain_text("bbbbbbb")
    expect(
        rows.nth(31).get_by_role("link", name="Open commit bbbbbbb on GitHub", exact=True)
    ).to_have_attribute("href", "https://example.com/acme/app/commit/" + "b" * 40)
    page.screenshot(path=str(tmp_path / f"push-history-{entry}.png"))
    assert textbox.evaluate("el => el.getBoundingClientRect().top") == pytest.approx(
        composer_top, abs=1
    )
    feed.evaluate("el => { el.scrollTop = 0; }")
    expect(panel.get_by_text("Earlier comment 1", exact=True)).to_be_in_viewport()
    textbox.fill("Preview paragraph.\n\n" * 80)
    panel.get_by_role("tab", name="Preview", exact=True).click()
    expect(post_button).to_be_in_viewport()
    assert feed.evaluate("el => el.clientHeight") > 0
    if mobile:
        page.set_viewport_size({"width": 390, "height": 540})
        expect(post_button).to_be_in_viewport()
        assert feed.evaluate("el => el.clientHeight") > 0
        page.set_viewport_size({"width": 390, "height": 900})
    panel.get_by_role("tab", name="Write", exact=True).click()
    textbox.fill("**Thanks for reviewing!**")
    tabs.get_by_role("tab", name="Summary", exact=True).click()
    tabs.get_by_role("tab", name="Comments 30", exact=True).click()
    expect(textbox).to_have_value("**Thanks for reviewing!**")
    panel.get_by_role("tab", name="Preview", exact=True).click()
    expect(panel.get_by_text("Thanks for reviewing!", exact=True)).to_be_visible()
    page.screenshot(path=str(tmp_path / f"comment-{entry}-preview.png"))
    panel.get_by_role("button", name="Post comment", exact=True).click()
    expect(tabs.get_by_role("tab", name="Comments 31", exact=True)).to_be_visible()
    feed.evaluate("el => { el.scrollTop = el.scrollHeight; }")
    expect(panel.get_by_text("Thanks for reviewing!", exact=True)).to_be_visible()
    panel.get_by_role("tab", name="Write", exact=True).click()
    expect(textbox).to_have_value("")
    panel.get_by_role("button", name="Refresh", exact=True).click()
    expect(panel.get_by_text("Thanks for reviewing!", exact=True)).to_be_visible()
    expect(rows).to_have_count(33)
    expect(rows.nth(31)).to_contain_text("force-pushed")
    expect(rows.nth(32)).to_contain_text("Thanks for reviewing!")
    feed.evaluate("el => { el.scrollTop = el.scrollHeight; }")
    expect(panel.get_by_text("Thanks for reviewing!", exact=True)).to_be_in_viewport()
    expect(post_button).to_be_in_viewport()
    assert posts == [{"pr_url": pr_url, "body": "**Thanks for reviewing!**"}]
    page.screenshot(path=str(tmp_path / f"comment-{entry}-posted.png"))


@pytest.mark.parametrize("entry", ["rail", "composer-desktop", "composer-mobile", "canvas"])
def test_reply_to_inline_review_conversation(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path, entry: str
) -> None:
    """Reply and start line/range conversations through each shared-panel surface."""
    base_url, session_id = seeded_session
    page.emulate_media(color_scheme="dark" if entry in {"composer-mobile", "canvas"} else "light")
    if entry in {"composer-desktop", "canvas"}:
        palette = "gruvbox" if entry == "composer-desktop" else "github"
        page.add_init_script(
            'localStorage.setItem("omnigent:ui-theme-palette", '
            f"JSON.stringify({json.dumps(palette)}))"
        )
    _stub_github(page, pr_count=2)
    pr_url = _INFO["pr"]["url"]
    thread = {
        "id": "thread-1",
        "path": "src/app/main.py",
        "side": "RIGHT",
        "line": 2,
        "start_line": None,
        "subject_type": "LINE",
        "resolved": False,
        "outdated": False,
        "can_reply": True,
        "diff_hunk": "@@ -1,2 +1,3 @@\n line1\n+added line\n line2",
        "comments": {
            "data": [
                {
                    "id": "root",
                    "author": "alice",
                    "body": (
                        "Why add this line?\n\n```suggestion\nimproved line\nreturn result\n```"
                    ),
                    "created_at": "2026-10-26T12:00:00Z",
                    "url": pr_url + "#discussion_r1",
                }
            ],
            "has_more": False,
            "next_cursor": None,
        },
    }
    threads = {
        "data": [
            thread,
            {
                **thread,
                "id": "old-side",
                "side": "LEFT",
                "line": 1,
                "comments": {
                    **thread["comments"],
                    "data": [
                        {
                            **thread["comments"]["data"][0],
                            "id": "old-root",
                            "body": "Old-side context",
                        }
                    ],
                },
            },
            {
                **thread,
                "id": "outdated",
                "path": "removed.py",
                "resolved": True,
                "outdated": True,
                "original_line": 2,
                "original_start_line": None,
                "comments": {
                    **thread["comments"],
                    "data": [{**thread["comments"]["data"][0], "id": "outdated-root"}],
                },
            },
        ],
        "has_more": False,
        "next_cursor": None,
        "head_sha": "head",
        "base_sha": "base",
    }
    info = {
        **_INFO,
        "tracking_available": True,
        "selected_pr_url": pr_url,
        "selected_account": "octocat",
        "capabilities": {
            "review_threads": True,
            "review_thread_replies": True,
            "review_thread_creation": True,
        },
        "pr": {**_INFO["pr"], "head_sha": "head", "base_sha": "base"},
    }
    thread_reads = []
    pending_reads: list[Route] = []
    hold_threads = True

    def read_info(route: Route) -> None:
        route.fulfill(json=info)

    def read_threads(route: Route) -> None:
        thread_reads.append(route.request.url)
        if hold_threads:
            pending_reads.append(route)
        else:
            route.fulfill(json=threads)

    posts = []

    def reply(route: Route) -> None:
        posts.append(route.request.post_data_json)
        comment = {
            "id": "reply",
            "author": "octocat",
            "body": posts[-1]["body"],
            "created_at": "2026-10-28T12:00:00Z",
            "url": pr_url + "#discussion_r2",
        }
        thread["comments"]["data"].append(comment)
        route.fulfill(json={"thread_id": thread["id"], "comment": comment})

    new_posts = []

    def create_thread(route: Route) -> None:
        payload = route.request.post_data_json
        new_posts.append(payload)
        identity = f"new-thread-{len(new_posts)}"
        created = {
            **thread,
            **payload["anchor"],
            "id": identity,
            "comments": {
                **thread["comments"],
                "data": [
                    {
                        **thread["comments"]["data"][0],
                        "id": identity + "-root",
                        "author": "octocat",
                        "body": payload["body"],
                    }
                ],
            },
        }
        threads["data"].append(created)
        route.fulfill(json={"thread": created})

    page.route(re.compile(r"/resources/github(?:\?|$)"), read_info)
    page.route(
        re.compile(r"/resources/github/diff(?:\?|$)"),
        lambda route: route.fulfill(json={**_PR_DIFF, "head_sha": "head", "base_sha": "base"}),
    )
    page.route(re.compile(r"/resources/github/review-threads/thread-1/replies$"), reply)
    page.route(re.compile(r"/resources/github/review-threads$"), create_thread)
    page.route(re.compile(r"/resources/github/review-threads\?"), read_threads)
    mobile = entry == "composer-mobile"
    page.set_viewport_size({"width": 390 if mobile else 1440, "height": 900})
    if entry == "canvas":
        from tests.e2e_ui.sessions.test_canvas_page import _stub_server_info

        _stub_server_info(page, canvas=True, session_pull_requests=True)
    page.goto(f"{base_url}/{'canvas/' if entry == 'canvas' else ''}c/{session_id}")
    panel = (
        page.get_by_test_id("github-panel-drawer")
        if mobile
        else page.get_by_role("complementary", name="Workspace")
    )
    if entry == "rail":
        open_right_rail(page)
        panel.get_by_role("tab", name="Pull Requests").click()
    else:
        open_session_pull_requests(page, mobile=mobile)
    panel.get_by_role(
        "button", name="example.com/acme/app #4242 — Add the GitHub tab", exact=True
    ).click()
    tabs = panel.get_by_role("tablist", name="Pull request", exact=True)
    tabs.get_by_role("tab", name="Comments", exact=False).click()
    top_comment = panel.get_by_role("region", name="PR comments").get_by_role("listitem").first
    comment_background = top_comment.evaluate(
        "element => getComputedStyle(element).backgroundColor"
    )
    comment_tint = top_comment.evaluate("element => getComputedStyle(element).backgroundImage")
    tabs.get_by_role("tab", name="Changes", exact=False).click()
    loading = panel.get_by_role("status").filter(has_text="Loading review threads")
    expect(loading).to_be_visible()
    layout_toggle = panel.get_by_role("button", name=re.compile(r"Switch to (split|unified) view"))
    loading_box, toggle_box = loading.bounding_box(), layout_toggle.bounding_box()
    assert loading_box and toggle_box
    assert loading_box["y"] + loading_box["height"] / 2 == pytest.approx(
        toggle_box["y"] + toggle_box["height"] / 2, abs=1
    )
    expect(loading.locator("svg")).to_have_class(re.compile("animate-spin"))
    page.screenshot(path=str(tmp_path / f"inline-{entry}-loading.png"))
    hold_threads = False
    pending_reads.pop().fulfill(json=threads)
    expect(loading).to_have_count(0)
    expect(panel.get_by_text(re.compile(r"^\d+\+? review conversations?$"))).to_have_count(0)
    conversation = panel.locator('[data-review-thread="thread-1"]')
    expect(conversation).to_be_visible()
    file = panel.locator('[data-github-file="src/app/main.py"]')
    diff = file.locator("diffs-container").first
    gutter = diff.locator(":scope > [data-diff] > [data-code] > [data-gutter]")
    rows = diff.locator(":scope > [data-diff] > [data-code] > [data-content]")
    first_line = gutter.locator('[data-column-number="1"]')
    last_line = gutter.locator('[data-column-number="3"]')
    original_line = last_line.element_handle()
    assert original_line
    first_box, last_box = first_line.bounding_box(), last_line.bounding_box()
    middle_box = gutter.locator('[data-column-number="2"]').bounding_box()
    assert first_box and last_box and middle_box
    page.mouse.move(last_box["x"] + 8, last_box["y"] + last_box["height"] / 2)
    page.mouse.down()
    expect(last_line).to_have_attribute("data-selected-line", "single")
    page.mouse.move(first_box["x"] + 8, first_box["y"] + first_box["height"] / 2, steps=5)
    expect(rows.locator(":scope > [data-line][data-selected-line]")).to_have_count(3)
    expect(gutter.locator(":scope > [data-column-number][data-selected-line]")).to_have_count(3)
    assert original_line.evaluate("element => element.isConnected")
    page.mouse.move(middle_box["x"] + 8, middle_box["y"] + middle_box["height"] / 2)
    expect(rows.locator(":scope > [data-line][data-selected-line]")).to_have_count(2)
    page.mouse.move(first_box["x"] + 8, first_box["y"] + first_box["height"] / 2)
    expect(rows.locator(":scope > [data-line][data-selected-line]")).to_have_count(3)
    expect(file.get_by_role("button", name="Comment on selected lines")).to_have_count(0)
    colors = page.evaluate("""() => {
        const probe = document.createElement('span');
        document.body.append(probe);
        const colors = ['sidebar-active', 'sidebar-active-foreground', 'input']
            .map(token => {
                probe.style.backgroundColor = `var(--${token})`;
                return getComputedStyle(probe).backgroundColor;
            });
        probe.remove();
        return colors;
    }""")
    for selected in rows.locator(":scope > [data-line][data-selected-line]").all():
        expect(selected).to_have_css("background-color", colors[0])
    expect(first_line).to_have_css("background-color", colors[0])
    expect(first_line).to_have_css("color", colors[1])
    expect(first_line).to_have_css("box-shadow", f"{colors[1]} -3px 0px 0px 0px inset")
    page.screenshot(path=str(tmp_path / f"inline-{entry}-drag-selection.png"))
    page.mouse.up()
    expect(file.get_by_role("button", name="Comment on selected lines")).to_have_count(0)
    expect(conversation).to_have_css("background-color", comment_background)
    expect(conversation).to_have_css("background-image", comment_tint)
    assert conversation.evaluate("element => !!element.closest('diffs-container')")
    assert (
        conversation.evaluate("element => element.closest('[slot]').getAttribute('slot')")
        == "annotation-additions-2"
    )
    old_side = panel.locator('[data-review-thread="old-side"]')
    expect(old_side).to_be_visible()
    assert (
        old_side.evaluate("element => element.closest('[slot]').getAttribute('slot')")
        == "annotation-deletions-1"
    )
    panel.get_by_role("button", name="Next unresolved conversation", exact=True).click()
    expect(conversation).to_be_focused()
    expect(conversation.get_by_text("Why add this line?", exact=True)).to_be_visible()
    suggestion = conversation.get_by_role("region", name="Suggested change", exact=True)
    expect(suggestion.locator('[data-line][data-line-type="change-deletion"]')).to_have_text(
        "added line"
    )
    expect(suggestion.locator('[data-line][data-line-type="change-addition"]')).to_have_text(
        ["improved line", "return result"]
    )
    expect(suggestion.locator("[data-line-number-content]")).to_have_text(["2", "2", "3"])
    expect(suggestion.locator("[data-diff-span]").first).to_be_visible()
    expect(suggestion.get_by_text("return", exact=True)).to_be_visible()
    assert suggestion.evaluate("element => element.scrollWidth <= element.clientWidth")
    textbox = conversation.get_by_role("textbox", name="Reply to review conversation")
    expect(conversation.get_by_role("tab", name="Write", exact=True)).to_have_count(0)
    page.screenshot(path=str(tmp_path / f"inline-{entry}-compact.png"))
    textbox.focus()
    expect(textbox).to_be_focused()
    expect(textbox).to_have_css("outline-style", "none")
    expect(textbox).to_have_css("border-color", colors[2])
    textbox.fill("**This handles empty input.**")
    conversation.get_by_role("button", name="Cancel", exact=True).click()
    expect(conversation.get_by_role("tab", name="Write", exact=True)).to_have_count(0)
    expect(textbox).to_have_value("**This handles empty input.**")
    page.keyboard.press("Tab")
    expect(textbox).to_be_focused()
    expect(conversation.get_by_role("tab", name="Write", exact=True)).to_be_visible()
    expect(textbox).to_have_value("**This handles empty input.**")
    tabs.get_by_role("tab", name="Summary", exact=True).click()
    tabs.get_by_role("tab", name="Changes", exact=False).click()
    expect(textbox).to_have_value("**This handles empty input.**")
    panel.get_by_role("button", name=re.compile(r"^All pull requests")).click()
    panel.get_by_role(
        "button", name="example.com/acme/app #4242 — Add the GitHub tab", exact=True
    ).click()
    tabs.get_by_role("tab", name="Changes", exact=False).click()
    expect(textbox).to_have_value("**This handles empty input.**")
    layout = panel.get_by_role("button", name=re.compile(r"Switch to (split|unified) view"))
    layout.click()
    expect(textbox).to_have_value("**This handles empty input.**")
    page.wait_for_function("""() => {
        const threads = document.querySelectorAll('[data-review-thread="thread-1"]');
        return [...threads].some(element => element.getBoundingClientRect().width >= 240);
    }""")
    layout.click()
    if mobile:
        textbox.fill("Long Markdown preview.\n\n" * 80)
        conversation.get_by_role("tab", name="Preview", exact=True).click()
        post = conversation.get_by_role("button", name="Reply", exact=True)
        post.scroll_into_view_if_needed()
        expect(post).to_be_in_viewport()
        page.set_viewport_size({"width": 390, "height": 540})
        post.scroll_into_view_if_needed()
        expect(post).to_be_in_viewport()
        page.set_viewport_size({"width": 390, "height": 900})
        conversation.get_by_role("tab", name="Write", exact=True).click()
        textbox.fill("**This handles empty input.**")
    conversation.get_by_role("tab", name="Preview", exact=True).click()
    expect(conversation.get_by_text("This handles empty input.", exact=True)).to_be_visible()
    page.screenshot(path=str(tmp_path / f"inline-{entry}-preview.png"))
    conversation.get_by_role("button", name="Reply", exact=True).click()
    expect(conversation.locator("li")).to_have_count(2)
    expect(conversation.get_by_role("tab", name="Write", exact=True)).to_have_count(0)
    hold_threads = True
    panel.get_by_role("button", name="Refresh", exact=True).click()
    expect(loading).to_be_visible()
    expect(conversation.locator("li")).to_have_count(2)
    hold_threads = False
    for pending in pending_reads:
        pending.fulfill(json=threads)
    pending_reads.clear()
    expect(loading).to_have_count(0)
    expect(conversation.get_by_text("This handles empty input.", exact=True)).to_be_visible()
    assert posts == [{"pr_url": pr_url, "body": "**This handles empty input.**"}]
    assert thread_reads
    fallback = panel.locator('[data-review-thread="outdated"]')
    expect(fallback.get_by_role("button", expanded=False)).to_be_visible()
    fallback.get_by_role("button", expanded=False).click()
    expect(fallback.get_by_text("Original code context", exact=True)).to_be_visible()
    expect(fallback.get_by_text("Original context", exact=True)).to_be_visible()
    conversation.scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / f"inline-{entry}-posted.png"))

    line = file.locator("diffs-container").first.locator(
        ":scope > [data-diff] > [data-code] > [data-gutter] "
        '> [data-column-number="2"][data-line-type="change-addition"]'
    )
    line.locator("[data-line-number-content]").click()
    plus = file.get_by_role("button", name="Start a new review conversation", exact=True)
    expect(plus).to_be_visible()
    line_box, plus_box = line.bounding_box(), plus.bounding_box()
    assert line_box and plus_box
    assert plus_box["x"] + plus_box["width"] / 2 == pytest.approx(
        line_box["x"] + line_box["width"], abs=2
    )
    assert plus_box["y"] + plus_box["height"] / 2 == pytest.approx(
        line_box["y"] + line_box["height"] / 2, abs=1
    )
    page.screenshot(path=str(tmp_path / f"inline-{entry}-gutter.png"))
    plus.click()
    draft = file.get_by_role("region", name="New review conversation", exact=True)
    expect(draft.get_by_text("New conversation · New line 2", exact=True)).to_be_visible()
    expect(draft).to_have_css("background-color", comment_background)
    expect(draft).to_have_css("background-image", comment_tint)
    line.hover()
    expect(plus).to_have_count(0)
    draft.get_by_role("button", name="Close comment composer", exact=True).click()
    expect(draft).to_have_count(0)
    line.hover()
    expect(plus).to_be_visible()
    plus.click()
    expect(draft.get_by_text("New conversation · New line 2", exact=True)).to_be_visible()
    expect(plus).to_have_count(0)
    comment_input = draft.get_by_role("textbox", name="New review comment")
    expect(comment_input).to_be_focused()
    expect(comment_input).to_have_css("outline-style", "none")
    expect(comment_input).to_have_css("border-color", colors[2])
    draft.get_by_role("textbox", name="New review comment").fill("**Separate conversation.**")
    page.screenshot(path=str(tmp_path / f"inline-{entry}-composer.png"))
    tabs.get_by_role("tab", name="Summary", exact=True).click()
    tabs.get_by_role("tab", name="Changes", exact=False).click()
    expect(draft.get_by_role("textbox", name="New review comment")).to_have_value(
        "**Separate conversation.**"
    )
    draft.get_by_role("tab", name="Preview", exact=True).click()
    expect(draft.get_by_text("Separate conversation.", exact=True)).to_be_visible()
    draft.scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / f"inline-{entry}-new-comment.png"))
    draft.get_by_role("button", name="Post comment", exact=True).click()
    created = file.locator('[data-review-thread="new-thread-1"]')
    expect(created.get_by_text("Separate conversation.", exact=True)).to_be_visible()
    expect(conversation.locator("li")).to_have_count(2)
    assert (
        created.evaluate("element => element.closest('[slot]').getAttribute('slot')")
        == "annotation-additions-2"
    )

    split = panel.get_by_role("button", name="Switch to split view", exact=True)
    if split.count():
        split.click()
    file.locator(
        '[data-code][data-deletions] [data-column-number="1"] [data-line-number-content]'
    ).click()
    file.locator(
        '[data-code][data-deletions] [data-column-number="2"] [data-line-number-content]'
    ).click(modifiers=["Shift"])
    old_line = file.locator('[data-code][data-deletions] [data-column-number="2"]')
    expect(plus).to_be_visible()
    line_box, plus_box = old_line.bounding_box(), plus.bounding_box()
    assert line_box and plus_box
    assert plus_box["x"] + plus_box["width"] / 2 == pytest.approx(
        line_box["x"] + line_box["width"], abs=2
    )
    plus.click()
    expect(draft.get_by_text("New conversation · Old lines 1–2", exact=True)).to_be_visible()
    expect(plus).to_have_count(0)
    draft.get_by_role("textbox", name="New review comment").fill("Consider these lines together.")
    draft.get_by_role("button", name="Post comment", exact=True).click()
    ranged = file.locator('[data-review-thread="new-thread-2"]')
    expect(ranged.get_by_text("Consider these lines together.", exact=True)).to_be_visible()
    assert (
        ranged.evaluate("element => element.closest('[slot]').getAttribute('slot')")
        == "annotation-deletions-2"
    )
    panel.get_by_role("button", name="Refresh", exact=True).click()
    expect(created.locator("li")).to_have_count(1)
    expect(ranged.locator("li")).to_have_count(1)
    assert new_posts == [
        {
            "pr_url": pr_url,
            "body": body,
            "anchor": {
                "path": "src/app/main.py",
                "side": side,
                "line": 2,
                "start_line": start,
                "head_sha": "head",
                "base_sha": "base",
            },
        }
        for body, side, start in [
            ("**Separate conversation.**", "RIGHT", None),
            ("Consider these lines together.", "LEFT", 1),
        ]
    ]
    ranged.scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / f"inline-{entry}-range-posted.png"))


# Two additions match "added": the first on line 2, the second on line 4.
_FIND_PATCH = (
    "diff --git a/src/app/main.py b/src/app/main.py\n"
    "index e69de29..4b825dc 100644\n"
    "--- a/src/app/main.py\n"
    "+++ b/src/app/main.py\n"
    "@@ -1,2 +1,4 @@\n"
    " line1\n"
    "+added line\n"
    " line2\n"
    "+added again\n"
)


@pytest.mark.parametrize("entry", ["rail", "composer-mobile"])
def test_changes_find_reveals_collapsed_and_filtered(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path, entry: str
) -> None:
    """Find reveals a collapsed file's match, highlights it, and steps through matches.

    Exercises find reveal, navigation, and highlighting through both panel entry points.
    Filter-dependent hints are covered by component tests.
    """
    base_url, session_id = seeded_session
    mobile = entry == "composer-mobile"
    page.emulate_media(color_scheme="dark" if mobile else "light")
    _stub_github(page, pr_count=2)
    page.route(
        re.compile(r"/resources/github/diff(?:\?|$)"),
        lambda route: route.fulfill(json={**_PR_DIFF, "patch": _FIND_PATCH}),
    )
    page.set_viewport_size({"width": 390 if mobile else 1440, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    panel = (
        page.get_by_test_id("github-panel-drawer")
        if mobile
        else page.get_by_role("complementary", name="Workspace")
    )
    if mobile:
        open_session_pull_requests(page, mobile=mobile)
    else:
        open_right_rail(page)
        panel.get_by_role("tab", name="Pull Requests").click()
    panel.get_by_role(
        "button", name="example.com/acme/app #4242 — Add the GitHub tab", exact=True
    ).click()
    tabs = panel.get_by_role("tablist", name="Pull request", exact=True)
    tabs.get_by_role("tab", name="Changes", exact=False).click()

    file = panel.locator('[data-github-file="src/app/main.py"]')
    file.get_by_role("button", name=re.compile(r"main\.py"), expanded=True).click()
    expect(file.locator("diffs-container")).to_have_count(0)

    panel.get_by_role("button", name="Find in changes", exact=True).click()
    find = panel.get_by_role("textbox", name="Find in changes", exact=True)
    expect(find).to_be_focused()
    find.fill("added")
    expect(panel.get_by_text("1 of 2", exact=True)).to_be_visible()
    expect(file.locator("diffs-container")).to_have_count(1)

    def wait_for_highlight(line: str) -> None:
        # Painting runs in an effect after the counter updates, so poll for the new range.
        page.wait_for_function(
            """([line]) => [...(CSS.highlights.get("pr-find-active") ?? [])].some((range) =>
                range.startContainer.isConnected && range.toString() === "added"
                && range.startContainer.parentElement.closest("[data-line]")
                    ?.getAttribute("data-line") === line)""",
            arg=[line],
        )

    def highlighted() -> list[dict[str, str | None]]:
        return page.evaluate(
            """() => [...(CSS.highlights.get("pr-find-active") ?? [])].map((range) => ({
                text: range.toString(),
                line: range.startContainer.parentElement.closest("[data-line]")
                    ?.getAttribute("data-line") ?? null,
            }))"""
        )

    first = file.locator('[data-line="2"][data-line-type="change-addition"]')
    expect(first).to_be_in_viewport()
    wait_for_highlight("2")
    assert highlighted() == [{"text": "added", "line": "2"}]
    page.screenshot(path=str(tmp_path / f"find-{entry}-first-match.png"))

    find.press("Enter")
    expect(panel.get_by_text("2 of 2", exact=True)).to_be_visible()
    wait_for_highlight("4")
    assert highlighted() == [{"text": "added", "line": "4"}]
    expect(file.locator('[data-line="4"][data-line-type="change-addition"]')).to_be_in_viewport()

    if not mobile:
        # The highlight survives a diff re-render in the other layout.
        panel.get_by_role("button", name="Switch to split view", exact=True).click()
        wait_for_highlight("4")
        assert highlighted() == [{"text": "added", "line": "4"}]
        panel.get_by_role("button", name="Switch to unified view", exact=True).click()

    find.press("Escape")
    expect(find).to_have_count(0)
    page.wait_for_function("() => !CSS.highlights.has('pr-find-active')")
    assert highlighted() == []

    # Ctrl/Cmd+F reopens the bar from inside the Changes tab.
    file.get_by_role("button", name=re.compile(r"main\.py"), expanded=True).focus()
    page.keyboard.press("ControlOrMeta+f")
    expect(panel.get_by_role("textbox", name="Find in changes", exact=True)).to_be_focused()
    page.screenshot(path=str(tmp_path / f"find-{entry}-reopened.png"))


# README.md's hunk, so its viewed mark has a patch to be checked against.
_README_PATCH = (
    "diff --git a/README.md b/README.md\n"
    "new file mode 100644\n"
    "index 0000000..4c2d3e1\n"
    "--- /dev/null\n"
    "+++ b/README.md\n"
    "@@ -0,0 +1,2 @@\n"
    "+# Demo\n"
    "+Adds the GitHub tab.\n"
)


@pytest.mark.parametrize("entry", ["rail", "composer-mobile"])
def test_changes_viewed_progress_persists_and_resets(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path, entry: str
) -> None:
    """Viewed marks collapse files and survive a reload. A new head keeps the mark on an
    unchanged file and flags a changed one as "Changed since viewed"."""
    base_url, session_id = seeded_session
    mobile = entry == "composer-mobile"
    page.set_viewport_size({"width": 390 if mobile else 1280, "height": 900})
    _stub_github(page)
    pr_url = _INFO["pr"]["url"]
    # The PR's head moves mid-test, so every resource answers for the current head.
    head = {"sha": "head1"}
    # head2 reworks main.py's hunk and leaves README.md's hunk as it was.
    patches = {
        "head1": _PR_DIFF["patch"] + _README_PATCH,
        "head2": _PR_DIFF["patch"].replace("+added line", "+reworked line") + _README_PATCH,
    }

    def info_at_head(route: Route) -> None:
        route.fulfill(
            json={
                **_INFO,
                "tracking_available": True,
                "selected_pr_url": pr_url,
                "prs": [
                    {
                        "url": pr_url,
                        "host": "example.com",
                        "repository": "acme/app",
                        "number": _PR_NUMBER,
                        "relationship": "created",
                    }
                ],
                "pr": {**_INFO["pr"], "head_sha": head["sha"], "base_sha": "base"},
            }
        )

    page.route(re.compile(r"/resources/github(?:\?|$)"), info_at_head)
    page.route(
        re.compile(r"/resources/github/changes"),
        lambda r: r.fulfill(json={**_CHANGES, "head_sha": head["sha"], "base_sha": "base"}),
    )
    page.route(
        re.compile(r"/resources/github/diff(?:\?|$)"),
        lambda r: r.fulfill(
            json={
                **_PR_DIFF,
                "patch": patches[head["sha"]],
                "head_sha": head["sha"],
                "base_sha": "base",
            }
        ),
    )

    def open_changes() -> Locator:
        if mobile:
            open_session_pull_requests(page, mobile=mobile)
            panel = page.get_by_test_id("github-panel-drawer")
        else:
            open_right_rail(page)
            panel = page.get_by_role("complementary", name="Workspace")
            panel.get_by_role("tab", name="Pull Requests").click()
        panel.get_by_role("tablist", name="Pull request", exact=True).get_by_role(
            "tab", name="Changes", exact=True
        ).click()
        return panel

    page.goto(f"{base_url}/c/{session_id}")
    panel = open_changes()
    expect(panel.get_by_text("0 / 2 files viewed", exact=True)).to_be_visible(timeout=30_000)
    panel.get_by_role("checkbox", name="Viewed README.md").click()
    panel.get_by_role("checkbox", name="Viewed src/app/main.py").click()
    expect(panel.get_by_role("checkbox", name="Viewed README.md")).to_be_checked()
    expect(panel.get_by_text("2 / 2 files viewed", exact=True)).to_be_visible()
    if not mobile:  # Mobile shows the count without the bar.
        expect(panel.get_by_role("progressbar", name="Files viewed")).to_have_attribute(
            "aria-valuenow", "100"
        )
    # Marking collapses the file's diff.
    expect(
        panel.get_by_role("button", name=re.compile(r"^README\.md"), expanded=False)
    ).to_be_visible()
    page.screenshot(path=str(tmp_path / f"viewed-{entry}-marked.png"), animations="disabled")

    # Marks are browser-local, so they survive a reload on the same head.
    page.reload()
    panel = open_changes()
    expect(panel.get_by_role("checkbox", name="Viewed README.md")).to_be_checked(timeout=30_000)
    expect(panel.get_by_text("2 / 2 files viewed", exact=True)).to_be_visible()

    # The new head changes main.py's hunk. README.md keeps its mark, and main.py loses its
    # mark and is flagged until it is marked again.
    head["sha"] = "head2"
    page.reload()
    panel = open_changes()
    expect(panel.get_by_text("1 / 2 files viewed", exact=True)).to_be_visible(timeout=30_000)
    expect(panel.get_by_role("checkbox", name="Viewed README.md")).to_be_checked()
    expect(panel.get_by_role("checkbox", name="Viewed src/app/main.py")).not_to_be_checked()
    expect(panel.get_by_text("Changed since viewed", exact=True)).to_have_count(1)
    expect(panel.get_by_text(re.compile("changed files lost their viewed marks"))).to_be_visible()
    page.screenshot(path=str(tmp_path / f"viewed-{entry}-reset.png"), animations="disabled")
    panel.get_by_role("button", name="Dismiss", exact=True).click()
    expect(panel.get_by_text(re.compile("changed files lost their viewed marks"))).to_have_count(0)

    # Marking the changed file on the new head clears its badge. Then hiding viewed files,
    # once every file is viewed, shows the all-viewed state.
    panel.get_by_role("checkbox", name="Viewed src/app/main.py").click()
    expect(panel.get_by_text("Changed since viewed", exact=True)).to_have_count(0)
    panel.get_by_role("checkbox", name="Hide viewed").click()
    expect(panel.get_by_text("All 2 files viewed", exact=True)).to_be_visible()
    if not mobile:
        expect(panel.get_by_role("progressbar", name="Files viewed")).to_have_attribute(
            "aria-valuenow", "100"
        )
    page.screenshot(path=str(tmp_path / f"viewed-{entry}-all-viewed.png"), animations="disabled")
    panel.get_by_role("button", name="Show viewed", exact=True).click()
    expect(panel.get_by_role("checkbox", name="Hide viewed")).not_to_be_checked()
    expect(panel.get_by_role("checkbox", name="Viewed README.md")).to_be_checked()


@pytest.mark.parametrize("entry", ["rail", "composer-mobile"])
def test_changes_filter_copy_open(page: Page, seeded_session: tuple[str, str], entry: str) -> None:
    """Filter changed files, copy a path, and open a file at the pinned head revision.

    The rail covers the desktop sidebar and icon copy. The mobile composer covers the
    collapsed sidebar's inline filter and every file action in the overflow menu.
    """
    base_url, session_id = seeded_session
    is_mobile = entry == "composer-mobile"
    pr_url = _INFO["pr"]["url"]
    _stub_github(page)
    # The list reports the revisions it was fetched for, which Open links use.
    page.route(
        re.compile(r"/resources/github/changes"),
        lambda route: route.fulfill(json={**_CHANGES, "head_sha": "head", "base_sha": "base"}),
    )
    # Supply fork metadata and revisions for the Open-link assertion.
    page.route(
        re.compile(r"/resources/github(?:\?|$)"),
        lambda route: route.fulfill(
            json={
                **_INFO,
                "tracking_available": True,
                "selected_pr_url": pr_url,
                "prs": [
                    {
                        "url": pr_url,
                        "host": "example.com",
                        "repository": "acme/app",
                        "number": _PR_NUMBER,
                        "relationship": "created",
                    }
                ],
                "pr": {
                    **_INFO["pr"],
                    "head_sha": "head",
                    "base_sha": "base",
                    "head_repository_url": "https://example.com/fork/app",
                },
            }
        ),
    )
    if is_mobile:
        workspace = "/workspace/demo-app"

        def session_details(route: Route) -> None:
            response = fetch_with_retry(route)
            snapshot = response.json()
            snapshot.update(
                workspace=workspace, git_branch="feature/github-tab", host_id="composer-pr-host"
            )
            route.fulfill(response=response, json=snapshot)

        page.route(re.compile(rf"/v1/sessions/{session_id}(?:\?.*)?$"), session_details)
        page.route(
            "**/v1/hosts/composer-pr-host/worktrees?*",
            lambda route: route.fulfill(
                json={
                    "data": [
                        {
                            "path": workspace,
                            "branch": "feature/github-tab",
                            "is_main": False,
                            "detached": False,
                        }
                    ]
                }
            ),
        )
        page.set_viewport_size({"width": 390, "height": 844})
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base_url)
    page.goto(f"{base_url}/c/{session_id}")
    if is_mobile:
        open_session_pull_requests(page, mobile=True)
        panel = page.get_by_test_id("github-panel-drawer")
        expect(panel).to_have_attribute("data-state", "open")
    else:
        open_right_rail(page)
        panel = page.get_by_role("complementary", name="Workspace")
        panel.get_by_role("tab", name="Pull Requests").click()
    panel.get_by_role("tablist", name="Pull request").get_by_role("tab", name="Changes").click()

    files = panel.locator("[data-github-file]")
    expect(files).to_have_count(2, timeout=30_000)
    if is_mobile:
        # The sidebar starts collapsed on mobile, so the filter opens above the list.
        panel.get_by_role("button", name="Show file filter").click()
    filter_box = panel.get_by_role("searchbox", name="Filter changed files")
    filter_box.fill("readme")
    expect(files).to_have_attribute("data-github-file", "README.md")
    filter_box.fill("no-such-file")
    expect(panel.get_by_text('No changed files match "no-such-file"')).to_be_visible()
    panel.get_by_role("button", name="Clear filter").click()
    expect(files).to_have_count(2)
    filter_box.fill("readme")
    filter_box.press("Escape")
    expect(filter_box).to_have_value("")
    expect(files).to_have_count(2)

    if is_mobile:
        panel.get_by_role("button", name="More actions for src/app/main.py").click()
        page.get_by_role("menuitem", name="Copy path", exact=True).click()
    else:
        panel.get_by_role("button", name="Copy path src/app/main.py").click()
    expect(page.get_by_text("Copied path", exact=True)).to_be_visible()
    assert page.evaluate("navigator.clipboard.readText()") == "src/app/main.py"

    panel.get_by_role("button", name="More actions for README.md").click()
    expect(page.get_by_role("menuitem", name="Open in GitHub")).to_have_attribute(
        "href", "https://example.com/fork/app/blob/head/README.md"
    )
