"""Azure browser journeys use the shared UI with mocked provider responses.

No test contacts an Azure organization, invokes ``az``, or sends a message.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import open_right_rail
from tests.e2e_ui.github.test_github_tab import _INFO, _stub_github

_PR_URL = "https://dev.azure.com/contoso/web/_git/app/pullrequest/7"
_SECOND_URL = "https://dev.azure.com/contoso/web/_git/other/pullrequest/12"
_DISPLAY = {
    "id": "azure_devops",
    "display_name": "Azure DevOps",
    "request_name": "pull request",
    "number_prefix": "!",
}
_REFERENCE = {
    "provider": "azure_devops",
    "provider_display": _DISPLAY,
    "host": "dev.azure.com",
    "repository": "contoso/web/app",
    "number": 7,
    "url": _PR_URL,
    "title": "Add the widget",
    "relationship": "attached",
}

# The GitHub payload minus its legacy fields, so ``auth`` alone drives the panel.
_ADO_INFO = {
    **{key: value for key, value in _INFO.items() if key not in ("gh_available", "authenticated")},
    "branch": "feature/widget",
    "provider": "azure_devops",
    "provider_display": _DISPLAY,
    "tracking_available": True,
    "selected_pr_url": _PR_URL,
    "prs": [_REFERENCE],
    "auth": {
        "authenticated": True,
        "hint": None,
        "cli": {"name": "az", "available": True},
        "accounts": None,
        "selected_account": None,
    },
    "capabilities": {
        "account_switching": False,
        "base_remote_selection": False,
        "line_counts": False,
        "linked_pr_diff": False,
    },
    "repo": {"name_with_owner": "contoso/web/app"},
    "pr": {
        "number": 7,
        "title": "Add the widget",
        "state": "OPEN",
        "url": _PR_URL,
        "is_draft": False,
        "author": "dev",
        "base_ref": "main",
        "head_ref": "feature/widget",
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "checks": {"passing": 0, "failing": 0, "pending": 0, "total": 0, "runs": []},
        "body": "Adds the widget.",
        "comments": [],
    },
}

# What the host sends when the workspace remote belongs to no supported provider.
_UNSUPPORTED_REMOTE = {
    "object": "session.github.info",
    "available": False,
    "reason": "unsupported_remote",
    "remote_host": "forge.example.test",
    "provider": None,
}


def _stub_azure(page: Page, info: dict | None = None) -> None:
    _stub_github(page)
    page.route(
        re.compile(r"/resources/github(?:\?|$)"),
        lambda route: route.fulfill(json=info if info is not None else _ADO_INFO),
    )
    page.route(
        re.compile(r"/resources/github/changes"),
        lambda route: route.fulfill(
            json={
                "object": "list",
                "has_more": False,
                "data": [{"path": "src/app/main.py", "name": "main.py", "status": "modified"}],
            }
        ),
    )
    page.route(
        re.compile(r"/resources/github/diff(?:\?|$)"),
        lambda route: route.fulfill(
            json={
                "object": "session.github.pr_diff",
                "patch": "diff --git a/src/app/main.py b/src/app/main.py\n"
                "--- a/src/app/main.py\n+++ b/src/app/main.py\n"
                "@@ -2 +2 @@\n-old_line\n+new_line\n",
            }
        ),
    )

    def contents(route: Route) -> None:
        query = parse_qs(urlsplit(route.request.url).query)
        assert query["pr_url"] == [_PR_URL]
        assert query["head_sha"] == ["a" * 40]
        assert query["base_sha"] == ["b" * 40]
        route.fulfill(
            json={
                "object": "session.github.file_diff",
                "path": "src/app/main.py",
                "before": "unchanged_header\nold_line\n",
                "after": "unchanged_header\nnew_line\n",
            }
        )

    page.route(re.compile(r"/resources/github/diff/"), contents)


@pytest.mark.parametrize("entry", ["rail", "composer-desktop", "composer-mobile"])
def test_azure_devops_tab_shows_provider_repo_and_pull_request(
    page: Page,
    seeded_session: tuple[str, str],
    entry: str,
    tmp_path: Path,
) -> None:
    """Rail and composer entries show Azure details and pinned expanded context."""
    base_url, session_id = seeded_session
    info = _ADO_INFO
    if entry == "rail":
        info = {**info, "auth": {**info["auth"], "cli": {"name": "az", "available": False}}}
    _stub_azure(page, info)
    mobile = entry == "composer-mobile"
    page.set_viewport_size({"width": 390 if mobile else 1440, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")

    link = page.get_by_test_id("composer-pr-link")
    expect(link).to_have_text("!7", timeout=30_000)
    if entry == "rail":
        open_right_rail(page)
        page.get_by_role("tab", name="Pull Requests", exact=True).click()
    else:
        link.click()
    rail = (
        page.get_by_test_id("github-panel-drawer")
        if mobile
        else page.get_by_role("complementary", name="Workspace")
    )

    expect(rail.get_by_role("heading", name="Azure DevOps")).to_be_visible(timeout=30_000)
    expect(rail.get_by_text("contoso/web/app", exact=False).first).to_be_visible()
    expect(rail.get_by_text("Add the widget", exact=True)).to_be_visible()
    expect(rail.get_by_text("!7", exact=True)).to_be_visible()
    expect(rail.get_by_label("Pull request status: Open")).to_be_visible()
    expect(rail.get_by_role("heading", name="GitHub")).to_have_count(0)
    expect(rail.get_by_role("combobox", name="GitHub account")).to_have_count(0)
    expect(rail.get_by_text("Adds the widget.", exact=True)).to_be_visible()
    page.screenshot(path=tmp_path / "azure-summary.png", animations="disabled")
    rail.get_by_role("tablist", name="Pull request").get_by_role("tab", name="Changes").click()
    expect(rail.get_by_role("button", name="src/app", exact=True)).to_be_visible()
    expect(rail.get_by_text("new_line", exact=True)).to_be_visible()
    rail.locator("[data-expand-button]").first.click()
    expect(rail.get_by_text("unchanged_header", exact=True)).to_be_visible()
    page.screenshot(path=tmp_path / "azure-diff.png", animations="disabled")


def test_azure_partial_results_keep_loaded_data_and_explain_unavailable_diff(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    base_url, session_id = seeded_session
    info = {
        **_ADO_INFO,
        "warnings": ["Some pull request details could not be loaded from Azure DevOps."],
        "pr": {
            **_ADO_INFO["pr"],
            "checks": {
                "passing": 1,
                "failing": 0,
                "pending": 0,
                "total": 1,
                "runs": [],
                "partial": True,
            },
            "comments_partial": True,
        },
    }
    _stub_azure(page, info)
    page.route(
        re.compile(r"/resources/github/changes"),
        lambda route: route.fulfill(
            json={
                "object": "list",
                "data": [],
                "has_more": True,
                "warning": "Azure DevOps could not load every changed file.",
            }
        ),
    )
    page.route(
        re.compile(r"/resources/github/diff(?:\?|$)"),
        lambda route: route.fulfill(
            json={
                "object": "session.github.pr_diff",
                "patch": "",
                "unavailable_reason": "commits_unavailable",
                "message": "Commits are unavailable locally. Refresh after the background fetch.",
            }
        ),
    )
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name="Pull Requests").click()

    expect(rail.get_by_text("Add the widget", exact=True)).to_be_visible()
    expect(rail.get_by_text(re.compile(r"1\s*passed"))).to_be_visible()
    expect(rail.get_by_text("Some comments are unavailable.")).to_be_visible()
    expect(rail.get_by_text("No comments yet.")).to_have_count(0)
    page.screenshot(path=tmp_path / "azure-partial-summary.png", animations="disabled")
    rail.get_by_role("tablist", name="Pull request").get_by_role("tab", name="Changes").click()
    expect(rail.get_by_text("Azure DevOps could not load every changed file.")).to_be_visible()
    expect(
        rail.get_by_text("Commits are unavailable locally. Refresh after the background fetch.")
    ).to_be_visible()
    expect(rail.get_by_text("No changes vs base.")).to_have_count(0)
    page.screenshot(path=tmp_path / "azure-unavailable-diff.png", animations="disabled")


def test_unsupported_remote_shows_empty_state_without_a_provider_name(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A remote no provider serves gets "No supported remote" and a neutral header."""
    base_url, session_id = seeded_session
    page.route(
        re.compile(r"/resources/github(?:\?|$)"), lambda r: r.fulfill(json=_UNSUPPORTED_REMOTE)
    )
    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name="Pull Requests").click()

    expect(rail.get_by_text("No supported remote")).to_be_visible(timeout=30_000)
    expect(rail.get_by_text("forge.example.test", exact=True)).to_be_visible()
    # A GitHub Enterprise host the user never signed in to lands here too.
    expect(
        rail.get_by_text("gh auth login --hostname forge.example.test", exact=True)
    ).to_be_visible()
    expect(rail.get_by_role("heading", name="Pull Requests")).to_be_visible()
    expect(rail.get_by_role("heading", name="GitHub")).to_have_count(0)


def test_azure_link_select_unlink_explains_outside_workspace_diff(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    base_url, session_id = seeded_session
    _stub_azure(page)
    references = [dict(_REFERENCE)]

    def respond(route: Route) -> None:
        parsed = urlsplit(route.request.url)
        selected = parse_qs(parsed.query).get("pr_url", [references[0]["url"]])[0]
        if parsed.path.endswith("/prs"):
            body = route.request.post_data_json
            if body["action"] == "remove":
                references[:] = [ref for ref in references if ref["url"] != body["url"]]
                selected = references[0]["url"]
            else:
                references.append(
                    {
                        **_REFERENCE,
                        "url": _SECOND_URL,
                        "repository": "contoso/web/other",
                        "number": 12,
                        "title": "Another repository",
                    }
                )
                selected = _SECOND_URL
        reference = next((ref for ref in references if ref["url"] == selected), references[0])
        selected = reference["url"]
        route.fulfill(
            json={
                **_ADO_INFO,
                "prs": references,
                "selected_pr_url": selected,
                "repo": {"name_with_owner": reference["repository"]},
                "pr": {
                    **_ADO_INFO["pr"],
                    **{key: reference[key] for key in ("url", "number", "title")},
                },
            }
        )

    page.route(re.compile(r"/resources/github(?:/prs)?(?:\?|$)"), respond)

    def diff(route: Route) -> None:
        if parse_qs(urlsplit(route.request.url).query).get("pr_url") == [_SECOND_URL]:
            route.fulfill(
                json={
                    "object": "session.github.pr_diff",
                    "patch": "",
                    "unavailable_reason": "pr_outside_workspace",
                }
            )
        else:
            route.fallback()

    page.route(re.compile(r"/resources/github/diff(?:\?|$)"), diff)
    page.goto(f"{base_url}/c/{session_id}")
    page.get_by_test_id("composer-pr-link").click()
    panel = page.get_by_role("complementary", name="Workspace")
    panel.get_by_role("button", name="Link a PR", exact=True).click()
    panel.get_by_role("textbox", name="Pull request URL").fill(_SECOND_URL)
    panel.get_by_role("button", name="Link", exact=True).click()
    picker = panel.get_by_role("combobox", name="Session pull request")
    expect(picker).to_contain_text("!12")
    expect(panel.get_by_text("Another repository", exact=True)).to_be_visible()
    panel.get_by_role("tablist", name="Pull request").get_by_role("tab", name="Changes").click()
    expect(
        panel.get_by_text(
            "Diff unavailable for a PR outside this workspace's repository", exact=True
        )
    ).to_be_visible()
    page.screenshot(path=tmp_path / "azure-outside-workspace.png", animations="disabled")
    picker.click()
    page.get_by_role("option").filter(has_text="contoso/web/app !7").click()
    expect(panel.get_by_text("Add the widget", exact=True)).to_be_visible()
    picker.click()
    page.get_by_role("option").filter(has_text="contoso/web/other !12").click()
    panel.get_by_role("button", name="Unlink PR", exact=True).click()
    expect(picker).to_contain_text("!7")
    expect(page.get_by_test_id("composer-pr-link")).to_have_text("!7")
    page.screenshot(path=tmp_path / "azure-selection.png", animations="disabled")


def test_azure_canvas_link_uses_provider_identity(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    from tests.e2e_ui.sessions.test_canvas_page import _serve_list, _session, _stub_server_info

    base_url, session_id = seeded_session
    _stub_azure(page)
    _stub_server_info(page, canvas=True)
    # Keep live session updates from replacing the mocked Canvas branch.
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)
    page.route(
        "**/v1/sessions?*",
        _serve_list([_session(session_id, "Azure session", 1, git_branch="feature/widget")]),
    )
    page.route("**/v1/sessions/projects", lambda route: route.fulfill(json=[]))
    page.goto(f"{base_url}/canvas")
    card = page.get_by_test_id("session-card")
    expect(card).to_have_count(1)
    link = card.get_by_role("link", name="Open pull request !7", exact=True)
    expect(link).to_be_visible(timeout=30_000)
    expect(link).to_have_attribute("href", _PR_URL)
    page.screenshot(path=tmp_path / "azure-canvas.png", animations="disabled")
