"""Multiple repositories, stable selection, and durable link/unlink UI flow."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.github.test_github_tab import _INFO


def test_session_pr_selection_and_unlink(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    base_url, session_id = seeded_session
    one = "https://github.com/example/one/pull/42"
    two = "https://github.com/example/two/pull/42"
    urls = [one, two]
    requested: list[tuple[str, str]] = []
    pending_info: list[Route] = []
    hold_second_pr = True

    def info(url: str | None) -> dict:
        return {
            **_INFO,
            "repo": {"name_with_owner": "example/one" if url == one else "example/two"},
            "tracking_available": True,
            "selected_pr_url": url,
            "prs": [
                {
                    "url": value,
                    "host": "github.com",
                    "repository": value.split("github.com/")[1].split("/pull/")[0],
                    "number": 42,
                    "relationship": "created",
                }
                for value in urls
            ],
            "pr": {
                **_INFO["pr"],
                "number": 42,
                "url": url,
                "title": "First repository" if url == one else "Second repository",
                "head_sha": "head",
                "base_sha": "base",
            }
            if url
            else None,
        }

    def respond(route: Route) -> None:
        parsed = urlsplit(route.request.url)
        url = parse_qs(parsed.query).get("pr_url", [urls[0] if urls else None])[0]
        if parsed.path.endswith("/prs"):
            body = route.request.post_data_json
            if body["action"] == "remove":
                urls.remove(body["url"])
                url = urls[0] if urls else None
            else:
                urls.append(body["url"])
                url = body["url"]
            route.fulfill(json=info(url))
        elif parsed.path.endswith("/changes"):
            requested.append(("changes", url))
            filename = "one.py" if url == one else "two.py"
            route.fulfill(
                json={
                    "object": "list",
                    "data": [
                        {
                            "path": filename,
                            "name": filename,
                            "status": "modified",
                            "lines_added": 1,
                            "lines_removed": 1,
                        }
                    ],
                    "has_more": False,
                }
            )
        elif parsed.path.endswith("/diff"):
            requested.append(("diff", url))
            filename = "one.py" if url == one else "two.py"
            route.fulfill(
                json={
                    "object": "session.github.pr_diff",
                    "patch": (
                        f"diff --git a/{filename} b/{filename}\n--- a/{filename}\n"
                        f"+++ b/{filename}\n@@ -1 +1 @@\n-old\n+new\n"
                    ),
                }
            )
        else:
            assert url is None or url in urls, "Removed PR was selected again"
            if hold_second_pr and url == two:
                pending_info.append(route)
            else:
                route.fulfill(json=info(url))

    page.route(re.compile(r"/resources/github(?:/|\?|$)"), respond)
    page.goto(f"{base_url}/c/{session_id}")
    indicator = page.get_by_test_id("composer-pr-link")
    expect(indicator).to_have_accessible_name("2 PRs", timeout=30_000)
    indicator.click()
    rail = page.get_by_role("complementary", name="Workspace")
    expect(rail.get_by_role("tab", name="GitHub")).to_have_attribute("aria-selected", "true")
    picker = rail.get_by_role("combobox", name="Session pull request")
    expect(picker).to_have_text("example/one #42")
    original_picker = picker.element_handle()
    assert original_picker
    link_action = rail.get_by_role("button", name="Link a PR", exact=True)
    unlink_action = rail.get_by_role("button", name="Unlink PR", exact=True)
    expect(link_action).to_have_text("")
    expect(unlink_action).to_have_text("")
    link_action.hover()
    expect(page.get_by_role("tooltip", name="Link a PR", exact=True)).to_be_visible()
    link_action.click()
    expect(rail.get_by_role("textbox", name="Pull request URL")).to_be_visible()
    rail.get_by_role("textbox", name="Pull request URL").focus()
    page.screenshot(path=str(tmp_path / "session-pr-link-input.png"), animations="disabled")
    rail.get_by_role("button", name="Cancel", exact=True).click()
    expect(rail.get_by_role("textbox", name="Pull request URL")).to_have_count(0)
    expect(picker).to_have_text("example/one #42")
    link_action.click()
    rail.get_by_role("textbox", name="Pull request URL").press("Escape")
    expect(rail.get_by_role("textbox", name="Pull request URL")).to_have_count(0)
    picker.hover()
    expect(page.get_by_role("tooltip")).to_have_count(0)
    unlink_action.hover()
    expect(page.get_by_role("tooltip", name="Unlink PR", exact=True)).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_role("tooltip")).to_have_count(0)
    page.screenshot(path=str(tmp_path / "session-pr-count.png"), animations="disabled")
    picker.click()
    expect(page.get_by_role("option", name="example/one #42")).to_have_attribute(
        "aria-selected", "true"
    )
    expect(page.get_by_role("option", name="example/two #42")).to_be_visible()
    page.screenshot(path=str(tmp_path / "session-pr-popover.png"), animations="disabled")
    page.keyboard.press("Escape")
    expect(page.get_by_role("listbox")).to_have_count(0)
    expect(picker).to_be_focused()
    expect(picker).to_have_text("example/one #42")
    picker.press("ArrowDown")
    expect(page.get_by_role("listbox")).to_be_visible()
    expect(page.get_by_role("option", name="example/one #42")).to_be_focused()
    page.keyboard.press("End")
    expect(page.get_by_role("option", name="example/two #42")).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.get_by_role("listbox")).to_have_count(0)
    expect(picker).to_have_text("example/two #42")
    expect(rail.get_by_text("Loading GitHub…", exact=True)).to_be_visible()
    expect(rail.get_by_text("First repository", exact=True)).to_have_count(0)
    assert original_picker.evaluate("element => element.isConnected")
    expect(rail.get_by_role("button", name="Link a PR", exact=True)).to_be_enabled()
    expect(unlink_action).to_be_enabled()
    picker.click()
    expect(page.get_by_role("option", name="example/one #42")).to_be_visible()
    page.keyboard.press("Escape")
    expect(picker).to_be_focused()
    page.screenshot(path=str(tmp_path / "session-pr-switch-loading.png"), animations="disabled")
    assert pending_info
    hold_second_pr = False
    for route in pending_info:
        route.fulfill(json=info(two))
    expect(rail.get_by_text("Second repository", exact=True)).to_be_visible()

    rail.get_by_role("tablist", name="Pull request").get_by_role(
        "tab", name="Changes", exact=True
    ).click()
    expect(rail.get_by_text("two.py", exact=True).first).to_be_visible()
    assert ("changes", two) in requested
    assert ("diff", two) in requested
    unlink_action.click()
    expect(picker).to_have_text("example/one #42")
    expect(indicator).to_have_accessible_name("#42")
    unlink_action.click()
    expect(picker).to_have_count(0)
    expect(indicator).to_have_count(0)
    expect(unlink_action).to_have_count(0)
    description = rail.get_by_text(
        "Pull requests created in this session appear here. You can also link an existing PR."
    )
    expect(description).to_be_visible()
    link = rail.get_by_role("button", name="Link a PR", exact=True)
    expect(link).to_be_visible()
    description_box = description.bounding_box()
    link_box = link.bounding_box()
    assert description_box and link_box
    assert link_box["y"] >= description_box["y"] + description_box["height"]
    page.screenshot(path=str(tmp_path / "session-pr-empty-state.png"), animations="disabled")
    link.click()
    rail.get_by_role("textbox", name="Pull request URL").fill(two)
    page.screenshot(path=str(tmp_path / "session-pr-empty-link-input.png"), animations="disabled")
    rail.get_by_role("button", name="Cancel", exact=True).click()
    expect(rail.get_by_role("textbox", name="Pull request URL")).to_have_count(0)
    expect(indicator).to_have_count(0)
    link.click()
    expect(rail.get_by_role("textbox", name="Pull request URL")).to_have_value(two)
    rail.get_by_role("button", name="Link", exact=True).click()
    expect(picker).to_have_text("example/two #42")
    expect(indicator).to_have_accessible_name("#42")
    expect(rail.get_by_text("Second repository", exact=True)).to_be_visible()


def test_session_pr_account_fallback(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    base_url, session_id = seeded_session
    url = "https://github.com/example/one/pull/42"
    info = {
        **_INFO,
        "repo": None,
        "pr": None,
        "tracking_available": True,
        "selected_pr_url": url,
        "prs": [
            {
                "url": url,
                "host": "github.com",
                "repository": "example/one",
                "number": 42,
                "relationship": "created",
            }
        ],
        "accounts": [
            {
                "login": login,
                "active": login == "personal",
                "state": "success",
                "host": "github.com",
            }
            for login in ("personal", "work")
        ],
        "selected_account": "personal",
    }
    page.route(re.compile(r"/resources/github(?:\?|$)"), lambda route: route.fulfill(json=info))
    page.goto(f"{base_url}/c/{session_id}")
    page.get_by_test_id("composer-pr-link").click()
    rail = page.get_by_role("complementary", name="Workspace")
    expect(rail.get_by_text("Can’t reach the upstream repo", exact=True)).to_be_visible()
    account = rail.get_by_role("combobox", name="GitHub account")
    alternative = rail.get_by_text("or", exact=True)
    link = rail.get_by_role("link", name="Open the PR on GitHub", exact=True)
    expect(account).to_have_text("personal (active)")
    expect(link).to_have_attribute("href", url)
    expect(link).to_have_attribute("target", "_blank")
    account_box, alternative_box, link_box = (
        element.bounding_box() for element in (account, alternative, link)
    )
    assert account_box and alternative_box and link_box
    assert alternative_box["y"] >= account_box["y"] + account_box["height"]
    assert link_box["y"] >= alternative_box["y"] + alternative_box["height"]
    page.screenshot(path=str(tmp_path / "session-pr-account-fallback.png"), animations="disabled")
