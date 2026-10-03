"""GitLab browser proof uses the real panel with deterministic resource responses."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import open_right_rail

HOST = "git.example.test:8443"
URL = f"https://{HOST}/team/sub/project/-/merge_requests/7"
SECOND = f"https://{HOST}/team/sub/other/-/merge_requests/12"
DISPLAY = {
    "id": "gitlab",
    "display_name": "GitLab",
    "request_name": "merge request",
    "number_prefix": "!",
}
PATCH = (
    "diff --git a/widget.py b/widget.py\n--- a/widget.py\n+++ b/widget.py\n"
    "@@ -2 +2 @@\n-old_widget\n+new_widget\n"
)


def stub(page: Page, *, partial: bool = False) -> list[tuple[str, str | None]]:
    urls = [URL]
    requested: list[tuple[str, str | None]] = []

    def info(url: str | None) -> dict:
        return {
            "object": "session.github.info",
            "available": True,
            "provider": "gitlab",
            "provider_display": DISPLAY,
            "branch": "feature/widget",
            "base_ref": "main",
            "tracking_available": True,
            "selected_pr_url": url,
            "repo": {"name_with_owner": "team/sub/project"},
            "auth": {
                "authenticated": True,
                "hint": None,
                "cli": {"name": "glab", "available": True},
                "accounts": None,
                "selected_account": None,
            },
            "capabilities": {
                "account_switching": False,
                "base_remote_selection": False,
                "line_counts": True,
                "linked_pr_diff": True,
            },
            "warnings": ["GitLab comments and checks are incomplete. Refresh to retry."]
            if partial
            else [],
            "prs": [
                {
                    "provider": "gitlab",
                    "provider_display": DISPLAY,
                    "host": HOST,
                    "repository": "team/sub/project" if value == URL else "team/sub/other",
                    "number": 7 if value == URL else 12,
                    "url": value,
                    "title": "Add the widget" if value == URL else "Fix another project",
                    "relationship": "attached",
                }
                for value in urls
            ],
            "pr": {
                "number": 7 if url == URL else 12,
                "url": url,
                "title": "Add the widget" if url == URL else "Fix another project",
                "state": "OPEN",
                "is_draft": False,
                "author": "developer",
                "author_id": "3",
                "base_ref": "main",
                "head_ref": "feature/widget",
                "head_sha": "a" * 40,
                "base_sha": "b" * 40,
                "body": "A widget from a nested GitLab project.",
                "comments_partial": partial,
                "comments": [
                    {
                        "author": "reviewer",
                        "author_id": "4",
                        "body": "The fork changes look good.",
                        "created_at": "2026-10-01T00:00:00Z",
                        "url": URL + "#note_11",
                    }
                ],
                "checks": {
                    "passing": 1,
                    "failing": 0,
                    "pending": 1,
                    "total": 2,
                    "partial": partial,
                    "runs": [
                        {
                            "name": "unit tests",
                            "bucket": "passing",
                            "url": f"https://{HOST}/team/sub/project/-/jobs/55",
                        },
                        {
                            "name": "downstream build",
                            "bucket": "pending",
                            "url": f"https://{HOST}/team/sub/project/-/jobs/56",
                        },
                    ],
                },
            }
            if url
            else None,
        }

    def respond(route: Route) -> None:
        parsed = urlsplit(route.request.url)
        selected = parse_qs(parsed.query).get("pr_url", [urls[0] if urls else None])[0]
        requested.append((parsed.path.rsplit("/", 1)[-1], selected))
        if parsed.path.endswith("/prs"):
            body = route.request.post_data_json
            if body["action"] == "remove":
                urls.remove(body["url"])
                selected = urls[0] if urls else None
            else:
                if body["url"] not in urls:
                    urls.append(body["url"])
                selected = body["url"]
            route.fulfill(json=info(selected))
        elif parsed.path.endswith("/changes"):
            route.fulfill(
                json={
                    "object": "list",
                    "data": [
                        {
                            "object": "session.github.changed_file",
                            "path": "widget.py",
                            "name": "widget.py",
                            "status": "modified",
                            "lines_added": 1,
                            "lines_removed": 1,
                        }
                    ],
                    "has_more": partial,
                    "warning": "GitLab returned incomplete file changes." if partial else None,
                }
            )
        elif parsed.path.endswith("/diff"):
            route.fulfill(
                json={
                    "object": "session.github.pr_diff",
                    "patch": "" if partial else PATCH,
                    **(
                        {
                            "unavailable_reason": "incomplete_diff",
                            "message": "View the merge request on GitLab for all changes.",
                        }
                        if partial
                        else {}
                    ),
                }
            )
        elif "/resources/github/diff/" in parsed.path:
            query = parse_qs(parsed.query)
            assert query["head_sha"] == ["a" * 40]
            assert query["base_sha"] == ["b" * 40]
            assert query["pr_url"] == [selected]
            route.fulfill(
                json={
                    "object": "session.github.file_diff",
                    "path": "widget.py",
                    "before": "unchanged_header\nold_widget\n",
                    "after": "unchanged_header\nnew_widget\n",
                }
            )
        else:
            route.fulfill(json=info(selected))

    page.route(re.compile(r"/resources/github(?:/|\?|$)"), respond)
    return requested


@pytest.mark.parametrize("entry", ["rail", "composer-desktop", "composer-mobile"])
def test_gitlab_panel_summary_and_diff(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path, entry: str
) -> None:
    base_url, session_id = seeded_session
    requested = stub(page)
    mobile = entry == "composer-mobile"
    page.set_viewport_size({"width": 390 if mobile else 1280, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    if entry == "rail":
        open_right_rail(page)
        panel = page.get_by_role("complementary", name="Workspace")
        panel.get_by_role("tab", name="Pull Requests").click()
    else:
        indicator = page.get_by_test_id("composer-pr-link")
        expect(indicator).to_have_accessible_name("!7", timeout=30_000)
        indicator.click()
        panel = (
            page.get_by_test_id("github-panel-drawer")
            if mobile
            else page.get_by_role("complementary", name="Workspace")
        )
    expect(panel.get_by_text("Add the widget", exact=True)).to_be_visible(timeout=30_000)
    expect(panel.get_by_role("heading", name="GitLab", exact=True)).to_be_visible()
    expect(panel.get_by_text("!7", exact=True)).to_be_visible()
    expect(panel.get_by_text("A widget from a nested GitLab project.", exact=True)).to_be_visible()
    expect(panel.get_by_text("The fork changes look good.", exact=True)).to_be_visible()
    expect(panel.get_by_text(re.compile(r"1\s*passed"))).to_be_visible()
    expect(panel.get_by_text(re.compile(r"1\s*pending"))).to_be_visible()
    expect(panel.get_by_role("combobox", name="GitHub account")).to_have_count(0)
    page.screenshot(path=str(tmp_path / f"gitlab-{entry}-summary.png"), animations="disabled")
    panel.get_by_role("tablist", name="Pull request").get_by_role("tab", name="Changes").click()
    expect(panel.get_by_text("widget.py", exact=True).first).to_be_visible()
    expect(panel.get_by_text("new_widget", exact=True)).to_be_visible()
    assert ("changes", URL) in requested and ("diff", URL) in requested
    panel.locator("[data-expand-button]").first.click()
    expect(panel.get_by_text("unchanged_header", exact=True)).to_be_visible()
    assert ("widget.py", URL) in requested
    page.screenshot(path=str(tmp_path / f"gitlab-{entry}-diff.png"), animations="disabled")


def test_gitlab_link_select_unlink(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    base_url, session_id = seeded_session
    stub(page)
    page.goto(f"{base_url}/c/{session_id}")
    page.get_by_test_id("composer-pr-link").click()
    panel = page.get_by_role("complementary", name="Workspace")
    panel.get_by_role("button", name="Link a PR", exact=True).click()
    panel.get_by_role("textbox", name="Pull request URL").fill(SECOND)
    panel.get_by_role("button", name="Link", exact=True).click()
    picker = panel.get_by_role("combobox", name="Session pull request")
    expect(picker).to_contain_text("!12")
    expect(panel.get_by_text("Fix another project", exact=True)).to_be_visible()
    picker.click()
    page.get_by_role("option").filter(has_text="team/sub/project !7").click()
    expect(panel.get_by_text("Add the widget", exact=True)).to_be_visible()
    picker.click()
    page.get_by_role("option").filter(has_text="team/sub/other !12").click()
    panel.get_by_role("button", name="Unlink PR", exact=True).click()
    expect(picker).to_contain_text("!7")
    expect(page.get_by_test_id("composer-pr-link")).to_have_accessible_name("!7")
    page.screenshot(path=str(tmp_path / "gitlab-selection.png"), animations="disabled")


def test_gitlab_partial_data_is_visible(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    base_url, session_id = seeded_session
    stub(page, partial=True)
    page.goto(f"{base_url}/c/{session_id}")
    page.get_by_test_id("composer-pr-link").click()
    panel = page.get_by_role("complementary", name="Workspace")
    expect(
        panel.get_by_text(
            "GitLab comments and checks are incomplete. Refresh to retry.", exact=True
        )
    ).to_be_visible()
    expect(panel.get_by_text("Some comments are unavailable.", exact=True)).to_be_visible()
    panel.get_by_role("tablist", name="Pull request").get_by_role("tab", name="Changes").click()
    expect(
        panel.get_by_text("GitLab returned incomplete file changes.", exact=True)
    ).to_be_visible()
    expect(
        panel.get_by_text("View the merge request on GitLab for all changes.", exact=True)
    ).to_be_visible()
    page.screenshot(path=str(tmp_path / "gitlab-partial.png"), animations="disabled")


def test_gitlab_canvas_link_uses_merge_request_identity(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    from tests.e2e_ui.sessions.test_canvas_page import _serve_list, _session, _stub_server_info

    base_url, session_id = seeded_session
    stub(page)
    _stub_server_info(page, canvas=True)
    page.route(
        "**/v1/sessions?*",
        _serve_list(
            [_session(session_id, "GitLab canvas session", 1, git_branch="feature/widget")]
        ),
    )
    page.route("**/v1/sessions/projects", lambda route: route.fulfill(json=[]))
    page.goto(f"{base_url}/canvas")
    card = page.get_by_test_id("session-card")
    expect(card).to_have_count(1)
    link = card.get_by_role("link", name="Open merge request !7")
    expect(link).to_be_visible(timeout=30_000)
    expect(link).to_have_attribute("href", URL)
    page.screenshot(path=str(tmp_path / "gitlab-canvas.png"), animations="disabled")
